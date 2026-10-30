#!/usr/bin/env python3
"""Bounded offline ASR worker. Success stdout is exactly {text, language}."""
import argparse
import contextlib
import json
import math
import os
from pathlib import Path
import resource
import socket
import stat
import subprocess
import sys
import tempfile
import wave
import xml.etree.ElementTree as ET
from transcript_quality import transcript_problem

MAX_BYTES = 8 * 1024 * 1024
MAX_SECONDS = 90
SAMPLE_RATE = 16000
MODEL_REVISION = "62103fc276a35fdc76e318f314d8ff47987fba89"
MODEL_REPO = "mlx-community/whisper-large-v3-turbo-8bit"


class SpeechError(Exception):
    def __init__(self, code, message):
        self.code, self.message = code, message


def fail(code, message):
    raise SpeechError(code, message)


class SafeParser(argparse.ArgumentParser):
    def error(self, message):
        fail("invalid_arguments", "语音处理参数不完整")


def inspect_input(path):
    try:
        info = path.lstat()
    except OSError:
        fail("invalid_audio", "无法读取这份录音，请重新录制")
    if not stat.S_ISREG(info.st_mode) or info.st_size == 0:
        fail("invalid_audio", "录音不是有效的普通音频文件")
    if info.st_size > MAX_BYTES:
        fail("audio_too_large", "录音超过 8 MiB，请缩短后重试")
    try:
        result = subprocess.run(["/usr/bin/afinfo", "-x", str(path)], capture_output=True, timeout=12)
        if result.returncode != 0 or len(result.stdout) > 128 * 1024:
            fail("invalid_audio", "无法识别录音格式，请使用系统录音动作")
        root = ET.fromstring(result.stdout)
        tracks = root.findall(".//{*}track")
        if len(tracks) != 1:
            fail("invalid_audio", "请提交只有一个音轨的录音")
        duration = float(tracks[0].find("{*}duration").text)
        if not math.isfinite(duration) or duration <= 0:
            fail("invalid_audio", "录音时长无效，请重新录制")
        if duration > MAX_SECONDS:
            fail("audio_too_long", "录音超过 90 秒，请分成较短的任务")
    except SpeechError:
        raise
    except (OSError, subprocess.TimeoutExpired, ET.ParseError, TypeError, ValueError, AttributeError):
        fail("invalid_audio", "无法读取录音信息，请重新录制")


def validate_model(model):
    if not model.is_dir() or model.is_symlink():
        fail("asr_unavailable", "Mac 的本地语音模型尚未配置完成")
    try:
        manifest = json.loads((model / "personalos-model-manifest.json").read_text())
        if manifest.get("repo") != MODEL_REPO or manifest.get("revision") != MODEL_REVISION or not manifest.get("data_files_only"):
            fail("asr_unavailable", "Mac 的语音模型版本未通过配置检查")
        for name in ("config.json", "weights.safetensors"):
            file = model / name
            if file.is_symlink() or not file.is_file() or file.stat().st_size != manifest["files"][name]["size"]:
                fail("asr_unavailable", "Mac 的本地语音模型不完整，请重新配置")
    except SpeechError:
        raise
    except (OSError, ValueError, KeyError, TypeError):
        fail("asr_unavailable", "Mac 的语音模型清单无法读取")


@contextlib.contextmanager
def quiet_backend():
    """Do not emit transcripts, file paths, progress bars, or library diagnostics."""
    sys.stdout.flush()
    sys.stderr.flush()
    saved = (os.dup(1), os.dup(2))
    try:
        with open(os.devnull, "wb") as sink:
            os.dup2(sink.fileno(), 1)
            os.dup2(sink.fileno(), 2)
        yield
    finally:
        sys.stdout.flush()
        sys.stderr.flush()
        for target, original in zip((1, 2), saved):
            os.dup2(original, target)
            os.close(original)


def forbid_network(*args, **kwargs):
    raise OSError("Offline speech worker has no network access")


def bound_decoder_output():
    # Metadata may be incorrect; the decoder cannot fill disk before the duration
    # is checked again from the actual 16 kHz PCM sample count.
    limit = SAMPLE_RATE * MAX_SECONDS * 2 + 65536
    resource.setrlimit(resource.RLIMIT_FSIZE, (limit, limit))


def validate_result(result):
    """Reject the whole transcript if any spoken segment is unreliable."""
    if not isinstance(result, dict):
        fail("no_speech", "没有识别到清晰任务，请重新说一遍")
    text, language, segments = result.get("text"), result.get("language"), result.get("segments")
    if not isinstance(text, str) or not isinstance(language, str) or not isinstance(segments, list):
        fail("no_speech", "没有识别到清晰任务，请重新说一遍")
    text, language = text.strip(), language.strip()
    spoken = 0
    for segment in segments:
        if not isinstance(segment, dict) or not isinstance(segment.get("text"), str):
            fail("no_speech", "没有识别到清晰任务，请重新说一遍")
        if not segment["text"].strip():
            continue
        spoken += 1
        probability, logprob = segment.get("no_speech_prob"), segment.get("avg_logprob")
        for value in (probability, logprob):
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                fail("no_speech", "没有识别到清晰任务，请重新说一遍")
            try:
                finite = math.isfinite(value)
            except OverflowError:
                finite = False
            if not finite:
                fail("no_speech", "没有识别到清晰任务，请重新说一遍")
        if not 0 <= probability < 0.6 or not -1.2 <= logprob <= 0:
            # Never retain only the trusted parts: dropping a date, negation or
            # command fragment can change the meaning of the user's request.
            fail("no_speech", "没有识别到清晰任务，请重新说一遍")
    if not any(character.isalnum() for character in text) or not spoken or not language:
        fail("no_speech", "没有识别到清晰任务，请重新说一遍")
    if len(text) > 4000:
        fail("transcript_too_long", "识别内容过长，请分成几条任务")
    if transcript_problem(text):
        fail("no_speech", "识别结果出现异常重复，未执行任务，请重新说一遍")
    return {"text": text, "language": language}


def simplify_transcript(text):
    """Offline script conversion only: no guessed words, dates, or time values."""
    try:
        from opencc import OpenCC
        # t2s uses packaged dictionaries, without region-specific vocabulary
        # rewriting. English, digits and punctuation remain unchanged.
        return OpenCC("t2s").convert(text)
    except (ImportError, ModuleNotFoundError):
        fail("asr_unavailable", "Mac 的本地简体转换组件尚未安装完成")
    except Exception:
        fail("asr_failed", "本地简体转换未完成，请稍后重试")


def transcribe(path, model):
    inspect_input(path)
    validate_model(model)
    # Model download belongs exclusively to setup_local_speech.py, never a request.
    os.environ.update(HF_HUB_OFFLINE="1", TRANSFORMERS_OFFLINE="1", HF_HUB_DISABLE_TELEMETRY="1",
                      DO_NOT_TRACK="1", TOKENIZERS_PARALLELISM="false")
    socket.create_connection = forbid_network
    socket.socket.connect = forbid_network
    socket.socket.connect_ex = forbid_network
    # Keep decoded audio inside the caller's private per-job directory, so its
    # parent can remove it even if a timeout kills this worker before finally runs.
    with tempfile.TemporaryDirectory(prefix="personalos-asr-", dir=str(path.parent)) as temporary:
        pcm = Path(temporary) / "speech.wav"
        try:
            result = subprocess.run(["/usr/bin/afconvert", str(path), str(pcm), "-f", "WAVE", "-d", "LEI16@16000", "-c", "1"],
                                    capture_output=True, timeout=20, preexec_fn=bound_decoder_output)
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
        except SpeechError:
            raise
        except (OSError, subprocess.TimeoutExpired, wave.Error, EOFError):
            fail("invalid_audio", "录音转换失败，请重新录制")
        with quiet_backend():
            try:
                import numpy as np
                samples = np.frombuffer(raw, dtype="<i2").astype(np.float32) / 32768.0
                if not np.isfinite(samples).all() or float(np.sqrt(np.mean(samples * samples))) < 0.0005:
                    fail("no_speech", "录音中没有清晰语音，请靠近麦克风重试")
                import mlx_whisper
                result = mlx_whisper.transcribe(samples, path_or_hf_repo=str(model), language=None,
                                               task="transcribe", verbose=None, temperature=0.0,
                                               condition_on_previous_text=False, word_timestamps=False)
            except SpeechError:
                raise
            except (ImportError, ModuleNotFoundError):
                fail("asr_unavailable", "Mac 的本地语音组件尚未安装完成")
            except Exception:
                fail("asr_failed", "Mac 本地语音识别未完成，请稍后重试")
        validated = validate_result(result)
        with quiet_backend():
            simplified = simplify_transcript(validated["text"])
        # Retain the repetition/length gate both before and after script conversion.
        return validate_result({**result, "text": simplified})


def main():
    parser = SafeParser(description=__doc__)
    parser.add_argument("--input", required=True)
    parser.add_argument("--model", required=True)
    try:
        args = parser.parse_args()
        # Absolute names prevent a user-supplied filename from becoming a tool flag.
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
