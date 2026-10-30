"""No network/model installation: pinned upgrade checks use tiny temporary fixtures."""
import contextlib
import hashlib
import importlib.util
import io
import json
import pathlib
import tempfile
import unittest
from unittest import mock

spec=importlib.util.spec_from_file_location('isolated_speech_setup',pathlib.Path(__file__).resolve().parents[1]/'setup_local_speech.py')
setup=importlib.util.module_from_spec(spec); spec.loader.exec_module(setup)


def blob_id(data):
    return hashlib.sha1(b'blob '+str(len(data)).encode()+b'\0'+data).hexdigest()


class SpeechSetupTests(unittest.TestCase):
    def setUp(self):
        self.temporary=tempfile.TemporaryDirectory(); self.model=pathlib.Path(self.temporary.name)/'model'; self.model.mkdir()
        self.old_config=b'{"quantization":{"bits":4}}'; self.new_config=b'{"quantization":{"bits":8}}'
        self.weights=b'isolated eight-bit weight fixture'
        self.assertEqual(len(self.old_config),len(self.new_config))
        self.metadata={'sha':setup.REVISION,'siblings':[
            {'rfilename':'config.json','size':len(self.new_config),'blobId':blob_id(self.new_config)},
            {'rfilename':'model.safetensors','size':len(self.weights),'lfs':{'sha256':hashlib.sha256(self.weights).hexdigest()}}]}
        (self.model/'weights.safetensors').write_bytes(self.weights)
        self.stack=contextlib.ExitStack()
        self.stack.enter_context(mock.patch.object(setup,'MODEL',self.model))
        self.stack.enter_context(mock.patch.object(setup.subprocess,'run',side_effect=AssertionError('No package installs or external programs in this test')))
        self.network=self.stack.enter_context(mock.patch.object(setup.urllib.request,'urlopen',side_effect=lambda *a,**kw:io.BytesIO(json.dumps(self.metadata).encode())))
        self.fetch=self.stack.enter_context(mock.patch.object(setup,'download_data',side_effect=self.fake_download))
        self.stack.enter_context(contextlib.redirect_stdout(io.StringIO()))

    def tearDown(self): self.stack.close(); self.temporary.cleanup()

    def fake_download(self,url,path,expected_size):
        self.assertIn('/resolve/'+setup.REVISION+'/',url)
        data=self.new_config if url.endswith('/config.json') else self.weights
        self.assertEqual(len(data),expected_size); path.write_bytes(data)

    def test_same_size_four_bit_config_is_replaced_before_eight_bit_manifest(self):
        (self.model/'config.json').write_bytes(self.old_config)
        setup.setup_model()
        self.assertEqual((self.model/'config.json').read_bytes(),self.new_config)
        self.fetch.assert_called_once(); self.assertTrue(self.fetch.call_args.args[0].endswith('/config.json'))
        manifest=json.loads((self.model/'personalos-model-manifest.json').read_text())
        self.assertEqual(manifest['repo'],setup.REPO); self.assertEqual(manifest['revision'],setup.REVISION)
        self.assertEqual(manifest['files']['config.json']['sha256'],hashlib.sha256(self.new_config).hexdigest())
        self.assertEqual(setup.git_blob_digest(self.model/'config.json'),blob_id(self.new_config))

    def test_matching_official_blob_and_lfs_hash_are_reused_without_fetch(self):
        (self.model/'config.json').write_bytes(self.new_config)
        setup.setup_model(); self.fetch.assert_not_called()
        self.assertTrue((self.model/'personalos-model-manifest.json').is_file())

    def test_bad_same_size_download_is_rejected_and_partial_removed(self):
        (self.model/'config.json').write_bytes(self.old_config)
        self.fetch.side_effect=lambda url,path,size:path.write_bytes(self.old_config)
        with self.assertRaisesRegex(RuntimeError,'Git blob validation failed'): setup.setup_model()
        self.assertEqual((self.model/'config.json').read_bytes(),self.old_config)
        self.assertFalse((self.model/'config.json.partial').exists())
        self.assertFalse((self.model/'personalos-model-manifest.json').exists())

    def test_missing_official_blob_id_cannot_accept_existing_or_downloaded_config(self):
        (self.model/'config.json').write_bytes(self.new_config)
        self.metadata['siblings'][0].pop('blobId')
        with self.assertRaisesRegex(RuntimeError,'Git blob validation failed'): setup.setup_model()
        self.fetch.assert_called_once(); self.assertFalse((self.model/'personalos-model-manifest.json').exists())


if __name__=='__main__': unittest.main(verbosity=2)
