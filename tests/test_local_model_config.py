"""Local-only model selection and bounded residency configuration."""
import json
import os
import pathlib
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / 'server'))
from local_model_config import settings


class LocalModelSettingsTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.home = pathlib.Path(self.tmp.name)
        self.config = self.home / 'local-model.json'

    def write(self, value):
        self.config.write_text(json.dumps(value), encoding='utf-8')

    def test_absent_uses_local_defaults_and_integer_seconds(self):
        with mock.patch('urllib.request.urlopen', side_effect=AssertionError('No network')):
            value = settings(self.home)
        self.assertEqual(value, {'model': 'qwen3.5:9b', 'num_ctx': 32768, 'keep_alive': 60})
        self.assertIs(type(value['keep_alive']), int)
        self.assertFalse(self.config.exists())

    def test_optional_fields_and_explicit_context_residency_limits(self):
        self.write({'model': 'qwen3.8:27b-q4_K_M', 'num_ctx': 8192, 'keep_alive': 0})
        self.assertEqual(settings(self.home), {'model': 'qwen3.8:27b-q4_K_M', 'num_ctx': 8192, 'keep_alive': 0})
        self.write({'keep_alive': 300})
        self.assertEqual(settings(self.home)['keep_alive'], 300)
        self.write({})
        self.assertEqual(settings(self.home, 'qwen3.5:9b-q4_K_M')['model'], 'qwen3.5:9b-q4_K_M')

    def test_cloud_url_namespace_and_path_tags_rejected(self):
        for name in ('gpt-oss:120b-cloud', 'qwen3:cloud', 'CLOUD-model:latest',
                     'https://ollama.com/library/model', 'http:model', 'file:model',
                     '/tmp/model.gguf', '../model', 'model/../../x', 'user/model',
                     'model\\file', 'model:latest\n', ' model:latest', 'model:tag:other',
                     '-model', '.model', 'model..name', '', None, 123):
            with self.subTest(model=name):
                self.write({'model': name})
                with self.assertRaises(ValueError):
                    settings(self.home)

    def test_invalid_default_also_fails_closed(self):
        with self.assertRaises(ValueError):
            settings(self.home, 'model:cloud')

    def test_invalid_numeric_values_and_unknown_fields_rejected(self):
        for value in ({'num_ctx': 8191}, {'num_ctx': 32769}, {'num_ctx': True},
                      {'num_ctx': '8192'}, {'num_ctx': 8192.0}, {'keep_alive': -1},
                      {'keep_alive': 301}, {'keep_alive': False}, {'keep_alive': '60s'},
                      {'keep_alive': 60.0}, {'endpoint': 'https://example.com'}, [], None):
            with self.subTest(value=value):
                self.write(value)
                with self.assertRaises(ValueError):
                    settings(self.home)

    def test_malformed_duplicate_oversized_and_non_utf8_rejected(self):
        for raw in (b'{', b'{"model":"test-local:latest","model":"other:latest"}',
                    b'{"keep_alive":60,"keep_alive":0}', b' ' * 4097, b'\xff'):
            with self.subTest(raw=raw[:30]):
                self.config.write_bytes(raw)
                with self.assertRaises(ValueError):
                    settings(self.home)

    def test_symlink_and_non_regular_files_rejected(self):
        target = self.home / 'outside.json'
        target.write_text('{}')
        self.config.symlink_to(target)
        with self.assertRaises(ValueError):
            settings(self.home)
        self.config.unlink()
        self.config.mkdir()
        with self.assertRaises(ValueError):
            settings(self.home)
        self.config.rmdir()
        os.mkfifo(self.config)
        with self.assertRaises(ValueError):
            settings(self.home)

    def test_config_scope_and_no_shared_alias_mutation(self):
        second_home = self.home / 'second'
        second_home.mkdir()
        self.write({'model': 'qwen3.8:27b-q4_K_M'})
        before = self.config.read_bytes()
        self.assertEqual(settings(second_home)['model'], 'qwen3.5:9b')
        self.assertEqual(settings(self.home)['model'], 'qwen3.8:27b-q4_K_M')
        self.assertEqual(self.config.read_bytes(), before)


if __name__ == '__main__':
    unittest.main(verbosity=2)
