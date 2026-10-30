import contextlib
import hashlib
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest import mock
import wave

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "server"))
import transcribe_qwen as worker


class QwenModelValidationTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.model = Path(self.temporary.name) / "model"
        self.model.mkdir()
        config = {"model_type": "qwen3_asr", "architectures": ["Qwen3ASRForConditionalGeneration"],
                  "quantization": {"bits": 8, "group_size": 64, "mode": "affine"}}
        values = {"config.json": config, "tokenizer_config.json": {"tokenizer_class": "Qwen2Tokenizer"},
                  "model.safetensors.index.json": {"weight_map": {"weight": "model.safetensors"}}}
        for name in worker.DATA_FILES:
            data = b"fixture" if not name.endswith(".json") else json.dumps(values.get(name, {})).encode()
            (self.model / name).write_bytes(data)
        self.manifest = {"repo": worker.MODEL_REPO, "revision": worker.MODEL_REVISION,
                         "data_files_only": True, "files": {}}
        for name in worker.DATA_FILES:
            self.record_hash(name)
        self.write_manifest()
        for key, value in (("WEIGHTS_BYTES", 7), ("WEIGHTS_SHA256", hashlib.sha256(b"fixture").hexdigest())):
            patcher = mock.patch.object(worker, key, value)
            patcher.start()
            self.addCleanup(patcher.stop)

    def record_hash(self, name):
        data = (self.model / name).read_bytes()
        self.manifest["files"][name] = {"size": len(data), "sha256": hashlib.sha256(data).hexdigest()}

    def write_manifest(self):
        (self.model / "personalos-model-manifest.json").write_text(json.dumps(self.manifest))

    def change_json(self, name, value):
        (self.model / name).write_text(json.dumps(value))
        self.record_hash(name)
        self.write_manifest()

    def assert_invalid(self):
        with self.assertRaises(worker.SpeechError) as error:
            worker.validate_model(self.model)
        self.assertEqual(error.exception.code, "asr_unavailable")

    def test_accepts_complete_pinned_plain_data(self):
        worker.validate_model(self.model)

    def test_rejects_wrong_repo_revision_and_flag_types(self):
        for key, value in (("repo", "elsewhere/model"), ("revision", "main"), ("data_files_only", 1)):
            old = self.manifest[key]
            self.manifest[key] = value
            self.write_manifest()
            with self.subTest(key=key): self.assert_invalid()
            self.manifest[key] = old

    def test_rejects_model_and_manifest_symlinks(self):
        link = self.model.parent / "link"
        link.symlink_to(self.model, target_is_directory=True)
        with self.assertRaises(worker.SpeechError): worker.validate_model(link)
        manifest = self.model / "personalos-model-manifest.json"
        target = self.model.parent / "manifest"
        manifest.rename(target)
        manifest.symlink_to(target)
        self.assert_invalid()

    def test_rejects_symlinked_data_even_if_hash_matches(self):
        path = self.model / "merges.txt"
        target = self.model.parent / "merges"
        path.rename(target)
        path.symlink_to(target)
        self.assert_invalid()

    def test_rejects_extra_scripts_and_incomplete_downloads(self):
        for name in ("custom.py", "model.safetensors.partial", "unlisted.json"):
            path = self.model / name
            path.write_text("not executable")
            with self.subTest(name=name): self.assert_invalid()
            path.unlink()

    def test_rejects_missing_data_and_manifest_path_traversal(self):
        original = dict(self.manifest["files"])
        self.manifest["files"].pop("vocab.json")
        self.write_manifest()
        self.assert_invalid()
        self.manifest["files"] = original
        self.manifest["files"]["../other.json"] = {"size": 1, "sha256": "0" * 64}
        self.write_manifest()
        self.assert_invalid()

    def test_rejects_small_file_tampering_size_and_digest(self):
        path = self.model / "merges.txt"
        path.write_bytes(b"changed")
        self.assert_invalid()
        path.write_bytes(b"longer fixture")
        self.assert_invalid()

    def test_rejects_weight_metadata_other_than_pinned_version(self):
        for key, value in (("size", True), ("size", 8), ("sha256", "0" * 64)):
            old = self.manifest["files"]["model.safetensors"][key]
            self.manifest["files"]["model.safetensors"][key] = value
            self.write_manifest()
            with self.subTest(key=key, value=value): self.assert_invalid()
            self.manifest["files"]["model.safetensors"][key] = old

    def test_rejects_custom_loader_configuration_at_any_depth(self):
        for extra in ({"auto_map": {}}, {"nested": [{"auto_map": {"AutoTokenizer": "evil.Loader"}}]},
                      {"nested": {"trust_remote_code": True}}):
            self.change_json("tokenizer_config.json", {"tokenizer_class": "Qwen2Tokenizer", **extra})
            with self.subTest(extra=extra): self.assert_invalid()

    def test_rejects_other_tokenizers_and_external_weight_index(self):
        self.change_json("tokenizer_config.json", {"tokenizer_class": "CustomTokenizer"})
        self.assert_invalid()
        self.change_json("tokenizer_config.json", {"tokenizer_class": "Qwen2Tokenizer"})
        self.change_json("model.safetensors.index.json", {"weight_map": {"weight": "../external.safetensors"}})
        self.assert_invalid()


class QwenResultTests(unittest.TestCase):
    def test_rejects_punctuation_only_noise_transcripts(self):
        for text in ('.','。。。','… !?','♪♫','   '):
            with self.subTest(text=text), self.assertRaises(worker.SpeechError) as error:
                worker.validate_result({'text':text,'language':['Chinese'],'generation_tokens':3})
            self.assertEqual(error.exception.code,'no_speech')

    def result(self, **changes):
        return {"text": "明天下午三点到四点，安排项目讨论。", "language": ["Chinese"],
                "generation_tokens": 30, **changes}

    def test_preserves_dates_and_returns_fixed_language_codes(self):
        for label, code in (("Chinese", "zh"), ("English", "en"), ("Cantonese", "yue"), ("Japanese", "ja")):
            value = self.result(language=[label])
            with self.subTest(language=label):
                self.assertEqual(worker.validate_result(SimpleNamespace(**value)), {"text": value["text"], "language": code})
        self.assertEqual(worker.validate_result(self.result(language=["Chinese", "English"]))["language"], "mul")

    def test_rejects_budget_exhaustion_and_invalid_token_counts(self):
        for tokens in (None, True, 0, -1, 1.5, "30", 1024, 1025):
            with self.subTest(tokens=tokens), self.assertRaises(worker.SpeechError):
                worker.validate_result(self.result(generation_tokens=tokens))

    def test_rejects_empty_unknown_language_and_malformed_outputs(self):
        cases = [None, [], {}, self.result(text="  "), self.result(text=None), self.result(language="Chinese"),
                 self.result(language=[]), self.result(language=[None]), self.result(language=["unknown"])]
        for result in cases:
            with self.subTest(result=result), self.assertRaises(worker.SpeechError): worker.validate_result(result)

    def test_rejects_repetition_without_fabricated_model_confidence(self):
        for text in ("安排明天会议" + "测试" * 20, "明天" + "热" * 100, "a" * 4001):
            with self.subTest(text=text[:20]), self.assertRaises(worker.SpeechError): worker.validate_result(self.result(text=text))
        self.assertEqual(worker.validate_result(self.result())["language"], "zh")

    def test_tokenizer_loader_forces_local_builtin_and_restores_on_failure(self):
        class Tokenizer:
            @classmethod
            def from_pretrained(cls, path, **kwargs): return path, kwargs
        original = Tokenizer.__dict__["from_pretrained"]
        path = Path("/local/model")
        with worker.local_tokenizer_only(Tokenizer, path):
            loaded, kwargs = Tokenizer.from_pretrained(str(path), trust_remote_code=True, local_files_only=False)
            self.assertEqual(loaded, str(path))
            self.assertEqual(kwargs, {"trust_remote_code": False, "local_files_only": True})
            with self.assertRaises(worker.SpeechError): Tokenizer.from_pretrained("remote/model")
        self.assertIs(Tokenizer.__dict__["from_pretrained"], original)
        with self.assertRaises(RuntimeError):
            with worker.local_tokenizer_only(Tokenizer, path): raise RuntimeError("fixture")
        self.assertIs(Tokenizer.__dict__["from_pretrained"], original)

    def test_offline_guard_blocks_dns_tcp_udp_and_sets_offline_flags(self):
        with contextlib.ExitStack() as stack:
            stack.enter_context(mock.patch.dict(os.environ))
            for owner, name in ((worker.socket, "create_connection"), (worker.socket, "getaddrinfo"),
                                (worker.socket.socket, "connect"), (worker.socket.socket, "connect_ex"),
                                (worker.socket.socket, "sendto")):
                stack.enter_context(mock.patch.object(owner, name))
            worker.enforce_offline()
            for call in (worker.socket.create_connection, worker.socket.getaddrinfo,
                         worker.socket.socket.connect, worker.socket.socket.connect_ex, worker.socket.socket.sendto):
                with self.assertRaises(OSError): call()
            self.assertEqual(os.environ["HF_HUB_OFFLINE"], "1")
            self.assertEqual(os.environ["TRANSFORMERS_OFFLINE"], "1")

    def test_decode_rechecks_duration_format_short_recording_and_cleanup(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "input.audio"
            path.write_bytes(b"fixture")
            for seconds, channels, expected in ((1, 1, None), (0.1, 1, "no_speech"),
                                                (91, 1, "audio_too_long"), (1, 2, "invalid_audio")):
                def convert(args, **kwargs):
                    self.assertIs(kwargs["preexec_fn"], worker.bound_decoder_output)
                    with wave.open(args[2], "wb") as audio:
                        audio.setnchannels(channels); audio.setsampwidth(2); audio.setframerate(16000)
                        audio.writeframes(b"\x01\x00" * int(seconds * 16000) * channels)
                    return SimpleNamespace(returncode=0)
                with self.subTest(seconds=seconds, channels=channels), mock.patch.object(worker.subprocess, "run", side_effect=convert):
                    if expected:
                        with self.assertRaises(worker.SpeechError) as error: worker.decode_audio(path)
                        self.assertEqual(error.exception.code, expected)
                    else:
                        self.assertEqual(len(worker.decode_audio(path)), 32000)
                self.assertEqual(list(Path(directory).iterdir()), [path])

    def test_cli_invalid_arguments_emit_only_sanitized_error(self):
        stderr = io.StringIO()
        with mock.patch.object(sys, "argv", ["worker", "--unknown", "/private/sensitive.wav"]), contextlib.redirect_stderr(stderr):
            self.assertEqual(worker.main(), 2)
        result = json.loads(stderr.getvalue())
        self.assertEqual(result["code"], "invalid_arguments")
        self.assertNotIn("sensitive", stderr.getvalue())


if __name__ == "__main__":
    unittest.main(verbosity=2)
