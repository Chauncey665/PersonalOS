#!/usr/bin/env python3
"""Bounded offline Qwen3-ASR candidate. Success stdout is {text, language}."""
import contextlib
import hashlib
import json
import os
from pathlib import Path
import socket
import stat
import subprocess
import sys
import tempfile
import wave

from transcribe_local import (
    MAX_BYTES, MAX_SECONDS, SAMPLE_RATE, SafeParser, SpeechError,
    bound_decoder_output, fail, forbid_network, inspect_input, quiet_backend,
    simplify_transcript,
)
from transcript_quality import transcript_problem

MODEL_REPO = "mlx-community/Qwen3-ASR-1.7B-8bit"
MODEL_REVISION = "a8379a2e2f9e313c9292cdf1af4055ab56d50d55"
WEIGHTS_BYTES = 2463307541
WEIGHTS_SHA256 = "bf304b009cc7eca79283056f787b44c952d24ac22cec787b39732bba3c23c13c"
MAX_TOKENS = 1024
DATA_FILES = frozenset({
    "chat_template.json", "config.json", "generation_config.json", "merges.txt",
    "model.safetensors", "model.safetensors.index.json", "preprocessor_config.json",
    "tokenizer_config.json", "vocab.json",
})
LANGUAGE_CODES = {
    "chinese": "zh", "english": "en", "cantonese": "yue", "arabic": "ar",
    "german": "de", "french": "fr", "spanish": "es", "portuguese": "pt",
    "indonesian": "id", "italian": "it", "korean": "ko", "russian": "ru",
    "thai": "th", "vietnamese": "vi", "japanese": "ja", "turkish": "tr",
    "hindi": "hi", "malay": "ms", "dutch": "nl", "swedish": "sv",
    "danish": "da", "finnish": "fi", "polish": "pl", "czech": "cs",
    "filipino": "fil", "persian": "fa", "greek": "el", "romanian": "ro",
    "hungarian": "hu", "macedonian": "mk",
}


def _plain_file(path, maximum=None):
    info = path.lstat()
    if not stat.S_ISREG(info.st_mode) or info.st_size <= 0:
        fail("asr_unavailable", "Mac 的本地语音模型不完整，请重新配置")
    if maximum is not None and info.st_size > maximum:
        fail("asr_unavailable", "Mac 的语音模型配置超出大小限制")
    return info


def _reject_custom_code(value):
    if isinstance(value, dict):
        if "auto_map" in value or value.get("trust_remote_code") is True:
            fail("asr_unavailable", "Mac 的语音模型包含不支持的自定义加载配置")
        for child in value.values():
            _reject_custom_code(child)
    elif isinstance(value, list):
        for child in value:
            _reject_custom_code(child)


def validate_model(model):
    """Check the pinned local data set without importing any model-directory code.

    Setup must hash the full 2.46 GB weight file before writing the manifest.
    Requests check its pinned size/hash metadata and rehash the small data files;
    this deliberately avoids rereading all weights solely for every validation.
    """
    try:
        if model.is_symlink() or not model.is_dir():
            fail("asr_unavailable", "Mac 的本地语音模型尚未配置完成")
        manifest_path = model / "personalos-model-manifest.json"
        _plain_file(manifest_path, 256 * 1024)
        manifest = json.loads(manifest_path.read_text())
        if (not isinstance(manifest, dict) or manifest.get("repo") != MODEL_REPO
                or manifest.get("revision") != MODEL_REVISION
                or manifest.get("data_files_only") is not True):
            fail("asr_unavailable", "Mac 的语音模型版本未通过配置检查")
        files = manifest.get("files")
        if not isinstance(files, dict) or set(files) != DATA_FILES:
            fail("asr_unavailable", "Mac 的语音模型文件清单不完整")
        if {item.name for item in model.iterdir()} != DATA_FILES | {manifest_path.name}:
            fail("asr_unavailable", "Mac 的语音模型目录包含未批准的文件")
        configs = {}
        for name in sorted(DATA_FILES):
            file = model / name
            info = _plain_file(file, None if name == "model.safetensors" else 8 * MAX_BYTES)
            entry = files[name]
            if (not isinstance(entry, dict) or type(entry.get("size")) is not int
                    or entry["size"] != info.st_size
                    or not isinstance(entry.get("sha256"), str)
                    or len(entry["sha256"]) != 64
                    or any(c not in "0123456789abcdef" for c in entry["sha256"])):
                fail("asr_unavailable", "Mac 的本地语音模型未通过完整性检查")
            if name == "model.safetensors":
                if info.st_size != WEIGHTS_BYTES or entry["sha256"] != WEIGHTS_SHA256:
                    fail("asr_unavailable", "Mac 的语音模型权重版本不正确")
                continue
            raw = file.read_bytes()
            if hashlib.sha256(raw).hexdigest() != entry["sha256"]:
                fail("asr_unavailable", "Mac 的本地语音模型未通过完整性检查")
            if name.endswith(".json") and name != "vocab.json":
                value = json.loads(raw)
                if not isinstance(value, dict):
                    fail("asr_unavailable", "Mac 的语音模型配置无效")
                _reject_custom_code(value)
                configs[name] = value
        config = configs["config.json"]
        quantization = config.get("quantization")
        if (config.get("model_type") != "qwen3_asr"
                or config.get("architectures") != ["Qwen3ASRForConditionalGeneration"]
                or not isinstance(quantization, dict) or quantization.get("bits") != 8
                or quantization.get("group_size") != 64
                or quantization.get("mode") != "affine"
                or configs["tokenizer_config.json"].get("tokenizer_class") != "Qwen2Tokenizer"):
            fail("asr_unavailable", "Mac 的语音模型类型未通过配置检查")
        mapping = configs["model.safetensors.index.json"].get("weight_map")
        if not isinstance(mapping, dict) or not mapping or set(mapping.values()) != {"model.safetensors"}:
            fail("asr_unavailable", "Mac 的语音模型权重索引无效")
    except SpeechError:
        raise
    except (OSError, ValueError, KeyError, TypeError, RecursionError):
        fail("asr_unavailable", "Mac 的语音模型清单无法读取")


@contextlib.contextmanager
def local_tokenizer_only(tokenizer_class, model):
    """Override mlx-audio's trust_remote_code=True during this isolated load."""
    original_descriptor = tokenizer_class.__dict__["from_pretrained"]
    original = tokenizer_class.from_pretrained

    def local_pretrained(*args, **kwargs):
        if not args or not isinstance(args[0], (str, Path)) or Path(args[0]) != model:
            fail("asr_unavailable", "语音组件尝试加载未批准的模型")
        kwargs["trust_remote_code"] = False
        kwargs["local_files_only"] = True
        return original(*args, **kwargs)

    tokenizer_class.from_pretrained = staticmethod(local_pretrained)
    try:
        yield
    finally:
        tokenizer_class.from_pretrained = original_descriptor


def validate_result(result):
    """Qwen supplies tokens/languages, not Whisper no-speech confidence."""
    getter = result.get if isinstance(result, dict) else lambda key: getattr(result, key, None)
    text, languages, tokens = getter("text"), getter("language"), getter("generation_tokens")
    if (not isinstance(text, str) or not any(character.isalnum() for character in text)
            or not isinstance(languages, list) or not languages):
        fail("no_speech", "没有识别到清晰任务，请重新说一遍")
    if type(tokens) is not int or tokens <= 0:
        fail("no_speech", "没有识别到完整任务，请重新说一遍")
    if tokens >= MAX_TOKENS:
        fail("no_speech", "识别达到长度上限，未执行任务，请缩短录音后重试")
    codes = []
    for language in languages:
        if not isinstance(language, str):
            fail("no_speech", "没有识别到清晰语言，请重新说一遍")
        code = LANGUAGE_CODES.get(language.strip().casefold())
        if code is None:
            fail("no_speech", "没有识别到清晰语言，请重新说一遍")
        codes.append(code)
    text = text.strip()
    if len(text) > 4000:
        fail("no_speech", "识别内容过长，未执行任务，请分成几条任务")
    if transcript_problem(text):
        fail("no_speech", "识别结果出现异常重复，未执行任务，请重新说一遍")
    return {"text": text, "language": codes[0] if len(set(codes)) == 1 else "mul"}


def enforce_offline():
    os.environ.update(HF_HUB_OFFLINE="1", TRANSFORMERS_OFFLINE="1", HF_HUB_DISABLE_TELEMETRY="1",
                      DO_NOT_TRACK="1", TOKENIZERS_PARALLELISM="false")
    socket.create_connection = forbid_network
    socket.getaddrinfo = forbid_network
    socket.socket.connect = forbid_network
    socket.socket.connect_ex = forbid_network
    socket.socket.sendto = forbid_network


def decode_audio(path):
    """Decode bounded mono PCM; keep temporary audio inside the private job."""
    with tempfile.TemporaryDirectory(prefix="personalos-qwen-", dir=str(path.parent)) as temporary:
        pcm = Path(temporary) / "speech.wav"
        try:
            result = subprocess.run(
                ["/usr/bin/afconvert", str(path), str(pcm), "-f", "WAVE", "-d", "LEI16@16000", "-c", "1"],
                capture_output=True, timeout=20, preexec_fn=bound_decoder_output,
            )
            if result.returncode != 0:
                fail("invalid_audio", "录音转换未完成，请重新录制")
            os.chmod(pcm, 0o600)
            with wave.open(str(pcm), "rb") as audio:
                count = audio.getnframes()
                if audio.getnchannels() != 1 or audio.getsampwidth() != 2 or audio.getframerate() != SAMPLE_RATE:
                    fail("invalid_audio", "录音转换后的格式不受支持")
                if count > SAMPLE_RATE * MAX_SECONDS:
                    fail("audio_too_long", "录音超过 90 秒，请分成较短的任务")
                if count < SAMPLE_RATE // 5:
                    fail("no_speech", "录音太短，没有听到完整任务，请重试")
                raw = audio.readframes(count)
                if len(raw) != count * 2:
                    fail("invalid_audio", "录音内容不完整，请重新录制")
                return raw
        except SpeechError:
            raise
        except (OSError, subprocess.TimeoutExpired, wave.Error, EOFError):
            fail("invalid_audio", "录音转换失败，请重新录制")


def transcribe(path, model):
    inspect_input(path)
    validate_model(model)
    enforce_offline()
    raw = decode_audio(path)
    with quiet_backend():
        try:
            import numpy as np
            samples = np.frombuffer(raw, dtype="<i2").astype(np.float32) / 32768.0
            if not np.isfinite(samples).all() or float(np.sqrt(np.mean(samples * samples))) < 0.0005:
                fail("no_speech", "录音中没有清晰语音，请靠近麦克风重试")
            from transformers import AutoTokenizer
            from mlx_audio.stt.utils import load
            with local_tokenizer_only(AutoTokenizer, model):
                recognizer = load(str(model))
            result = recognizer.generate(samples, language=None, temperature=0.0,
                                         max_tokens=MAX_TOKENS, verbose=False,
                                         system_prompt=None, hotwords=None)
            validated = validate_result(result)
            simplified = simplify_transcript(validated["text"])
            # Apply the same output guards again after script conversion.
            getter = result.get if isinstance(result, dict) else lambda key: getattr(result, key, None)
            final = validate_result({"text": simplified, "language": getter("language"),
                                     "generation_tokens": getter("generation_tokens")})
        except SpeechError:
            raise
        except (ImportError, ModuleNotFoundError):
            fail("asr_unavailable", "Mac 的本地语音组件尚未安装完成")
        except Exception:
            fail("asr_failed", "Mac 本地语音识别未完成，请稍后重试")
    return final


def main():
    parser = SafeParser(description=__doc__)
    parser.add_argument("--input", required=True)
    parser.add_argument("--model", required=True)
    try:
        args = parser.parse_args()
        result = transcribe(Path(os.path.abspath(args.input)), Path(os.path.abspath(args.model)))
        print(json.dumps(result, ensure_ascii=False))
        return 0
    except SpeechError as error:
        print(json.dumps({"error": error.message, "code": error.code}, ensure_ascii=False), file=sys.stderr)
        return 2
    except Exception:
        print(json.dumps({"error": "本地语音处理暂不可用，请重试", "code": "asr_failed"}, ensure_ascii=False), file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
