import contextlib
import importlib.util
import io
import json
import os
import pathlib
import subprocess
import tempfile
import unittest
from unittest.mock import patch

HERE = pathlib.Path(__file__).resolve().parent

def load(name, filename):
    spec = importlib.util.spec_from_file_location(name, HERE/filename)
    module = importlib.util.module_from_spec(spec); spec.loader.exec_module(module); return module

exporter = load('project_readonly_tests', 'project_readonly.py')
installer = load('project_install_tests', 'install_remote.py')


class ExporterTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.base = pathlib.Path(self.temp.name).resolve()
        self.root = self.base/'PROJECT'; self.root.mkdir()
    def tearDown(self): self.temp.cleanup()


    def test_real_files_only_and_exclusions(self):
        (self.root/'README.md').write_text('PROJECT reference')
        (self.root/'.hidden.txt').write_text('hidden data')
        for folder in ['node_modules', 'build', '.git', 'Pods', 'venv']:
            (self.root/folder).mkdir(); (self.root/folder/'skip.md').write_text('excluded data')
        outside = self.base/'outside'; outside.mkdir(); (outside/'secret.md').write_text('do not export')
        (self.root/'linked.md').symlink_to(outside/'secret.md')
        (self.root/'linked-directory').symlink_to(outside, target_is_directory=True)
        result = exporter.build_index(self.root)
        self.assertEqual([h['path'] for h in result['hits']], ['README.md'])
        self.assertNotIn('do not export', json.dumps(result))
        linked_root = self.base/'linked-root'; linked_root.symlink_to(self.root, target_is_directory=True)
        with self.assertRaises(ValueError): exporter.build_index(linked_root)
    def test_latest_hundred_and_snippet_cap(self):
        for i in range(105):
            path = self.root/(str(i)+'.md'); path.write_text('字'*700)
            os.utime(path, (1700000000+i,1700000000+i))
        result = exporter.build_index(self.root)
        self.assertEqual(len(result['hits']),100)
        self.assertEqual(result['hits'][0]['path'],'104.md')
        self.assertEqual(result['hits'][-1]['path'],'5.md')
        self.assertTrue(result['truncated'])
        self.assertTrue(all(len(h['snippet'])==500 for h in result['hits']))
        self.assertLessEqual(len(json.dumps(result,ensure_ascii=False).encode()),2_097_152)
    def test_file_size_scan_and_output_caps(self):
        (self.root/'oversized.md').write_bytes(b'x'*(1_048_576+1))
        self.assertEqual(exporter.build_index(self.root)['hits'],[])
        for i in range(5): (self.root/(str(i)+'.md')).write_text('A'*500)
        with patch.object(exporter,'MAX_FILES',3):
            limited = exporter.build_index(self.root)
        self.assertEqual(limited['scanned'],3); self.assertTrue(limited['truncated'])
        with patch.object(exporter,'MAX_OUTPUT_BYTES',1000): limited = exporter.build_index(self.root)
        self.assertLessEqual(len(json.dumps(limited,ensure_ascii=False,separators=(',',':')).encode()),1000)
        self.assertTrue(limited['truncated'])
    def test_descriptor_open_rejects_traversal_and_symlink(self):
        outside = self.base/'outside.md'; outside.write_text('private')
        (self.root/'escape.md').symlink_to(outside)
        rootfd = os.open(self.root,os.O_RDONLY|os.O_DIRECTORY)
        try:
            with self.assertRaises(ValueError): exporter._open_relative_file(rootfd,'../outside.md')
            with self.assertRaises(OSError): exporter._open_relative_file(rootfd,'escape.md')
        finally: os.close(rootfd)
    def test_installer_is_restricted_and_idempotent(self):
        home = self.base/'user'; home.mkdir(); project = home/'Desktop/PROJECT'; project.mkdir(parents=True)
        ssh = home/'.ssh'; ssh.mkdir(); auth = ssh/'authorized_keys'
        original = 'ssh-ed25519 AAAAunrelated unrelated-user-key\n'; auth.write_text(original)
        payload = {'public_key':'ssh-ed25519 '+('A'*44)+' personal-os-project-readonly','exporter':'# fixture exporter\n'}
        for _ in range(2):
            incoming = io.TextIOWrapper(io.BytesIO(json.dumps(payload).encode()))
            with patch.object(installer,'HOME',home), patch.object(installer,'ROOT',project), patch.object(installer,'DESTINATION',home/'.local/share/personal-os'), patch.object(installer.pathlib.Path,'home',return_value=home), patch.object(installer.sys,'stdin',incoming), contextlib.redirect_stdout(io.StringIO()):
                installer.main()
        data = auth.read_text()
        self.assertIn(original.strip(),data)
        self.assertEqual(data.count(' personal-os-project-readonly'),1)
        restricted = next(line for line in data.splitlines() if line.endswith(' personal-os-project-readonly'))
        self.assertTrue(restricted.startswith('restrict,command="/usr/bin/python3 '))
        self.assertIn('/project_readonly.py" ssh-ed25519 ',restricted)
        self.assertEqual(auth.stat().st_mode & 0o777,0o600)
        self.assertEqual(list(project.iterdir()),[])

if __name__ == '__main__': unittest.main(verbosity=2)
