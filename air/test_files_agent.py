"""Isolated file-agent/bootstrap checks. Never connects to Air or reads real keys."""
import contextlib
import importlib.util
import io
import json
import os
import pathlib
import shutil
import stat
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

HERE = pathlib.Path(__file__).resolve().parent
SERVER = HERE.parent/'server'
sys.path.insert(0, str(SERVER))
sys.path.insert(0, str(HERE))
import file_agent
import install_files_remote
from file_access import FileAccessError, LocalFileAccess


def request(**extra):
    value = {'version': 1, 'operation': 'list', 'path': '/'}
    value.update(extra)
    return json.dumps(value).encode()+b'\n'


class FileAgentTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix='personalos-file-agent-')
        self.root = pathlib.Path(self.temporary.name).resolve()
    def tearDown(self): self.temporary.cleanup()

    def test_strict_single_request(self):
        cases = [b'', b'{}\n{}\n', b'{\n"version":1}', b'x'*16385,
                 b'{"version":1,"version":1,"operation":"list","path":"/"}',
                 request(version=True), request(operation='shell'), request(command='id'),
                 request(path='relative'), request(max_bytes=262145), request(offset=True),
                 request(offset=-1), request(cursor='a'*4097), request(etag='x'*257),
                 request(query='nul\x00'), b'{"version":1,"operation":"list","path":"/","offset":NaN}']
        for raw in cases:
            with self.subTest(raw=raw[:90]):
                with self.assertRaises((ValueError, UnicodeError)): file_agent.parse_request(raw)
        self.assertEqual(file_agent.parse_request(request())['max_bytes'], 262144)

    def test_real_core_listing_symlink_and_secret_guards(self):
        (self.root/'visible.txt').write_text('visible text')
        (self.root/'.env').write_text('not to be listed')
        (self.root/'link').symlink_to(self.root/'visible.txt')
        access = LocalFileAccess(home=str(self.root))
        reply = file_agent.process_request(request(path=str(self.root)), access)
        self.assertTrue(reply['ok'])
        self.assertEqual([x['name'] for x in reply['result']['entries']], ['visible.txt'])
        for name in ('.env', 'link'):
            denied = file_agent.process_request(request(operation='read',path=str(self.root/name)), access)
            self.assertFalse(denied['ok']); self.assertEqual(denied['status'],403)
        (self.root/'private.txt').write_text('-----BEGIN PRIVATE KEY-----\nsynthetic fixture')
        denied = file_agent.process_request(request(operation='read',path=str(self.root/'private.txt')), access)
        self.assertEqual(denied['code'],'sensitive_content')

    def test_real_core_download_chunks_and_changed_file(self):
        target = self.root/'document.txt'; content=b'normal project text\n'*22000; target.write_bytes(content)
        first = file_agent.process_request(request(operation='download',path=str(target)))
        self.assertTrue(first['ok']); body=first['result']
        self.assertEqual(body['next_offset'],262144); self.assertFalse(body['eof'])
        second = file_agent.process_request(request(operation='download',path=str(target),offset=262144,etag=body['etag']))
        self.assertTrue(second['ok']); self.assertTrue(second['result']['eof'])
        target.write_bytes(b'changed')
        changed=file_agent.process_request(request(operation='download',path=str(target),offset=262144,etag=body['etag']))
        self.assertEqual(changed['code'],'file_changed')

    def test_only_json_and_original_command_ignored(self):
        agent=self.root/'file_agent.py'; shutil.copyfile(HERE/'file_agent.py',agent)
        shutil.copyfile(SERVER/'file_access.py',self.root/'file_access.py')
        marker=self.root/'retained.txt'; marker.write_text('keep')
        env=os.environ.copy(); env['SSH_ORIGINAL_COMMAND']='rm -rf '+str(marker)
        result=subprocess.run([sys.executable,str(agent),'ignored',';id'],input=request(path=str(self.root)),
                              stdout=subprocess.PIPE,stderr=subprocess.PIPE,env=env,timeout=5)
        self.assertEqual(result.returncode,0); self.assertEqual(result.stderr,b'')
        self.assertTrue(json.loads(result.stdout)['ok']); self.assertTrue(marker.exists())
        bad=subprocess.run([sys.executable,str(agent)],input=b'{}\n{}\n',stdout=subprocess.PIPE,stderr=subprocess.PIPE,timeout=5)
        self.assertEqual(bad.stderr,b''); self.assertFalse(json.loads(bad.stdout)['ok'])

    def test_errors_are_bounded_json(self):
        huge=file_agent.encode_response({'version':1,'ok':True,'result':'x'*2_097_152})
        self.assertLess(len(huge),2048); self.assertEqual(json.loads(huge)['status'],413)
        class Failure:
            def handle(self,*args,**kwargs): raise RuntimeError('private traceback details')
        encoded=file_agent.encode_response(file_agent.process_request(request(),Failure()))
        self.assertNotIn(b'private traceback',encoded); self.assertEqual(json.loads(encoded)['status'],500)

    def test_remote_installer_is_idempotent_and_preserves_project(self):
        home=self.root/'air'; home.mkdir()
        ssh=home/'.ssh'; ssh.mkdir()
        old='restrict,command="old" ssh-ed25519 OLDKEY personal-os-project-readonly'
        (ssh/'authorized_keys').write_text(old+'\nssh-ed25519 OTHER other-device\n')
        payload={'public_key':'ssh-ed25519 AAAATESTKEY personalos-files-readonly',
                 'agent':(HERE/'file_agent.py').read_text(),'core':(SERVER/'file_access.py').read_text()}
        destination=home/'.local/share/personal-os-files'
        for _ in range(2):
            stream=io.TextIOWrapper(io.BytesIO(json.dumps(payload).encode()))
            with mock.patch.object(install_files_remote,'HOME',home), mock.patch.object(install_files_remote,'DESTINATION',destination), \
                 mock.patch.object(pathlib.Path,'home',return_value=home), mock.patch.object(sys,'stdin',stream), \
                 contextlib.redirect_stdout(io.StringIO()) as output:
                install_files_remote.main()
            self.assertTrue(json.loads(output.getvalue())['installed'])
        lines=(ssh/'authorized_keys').read_text().splitlines()
        self.assertIn(old,lines); self.assertEqual(len(lines),3)
        self.assertEqual(sum(' personalos-files-readonly' in x for x in lines),1)
        restricted=next(x for x in lines if x.endswith(' personalos-files-readonly'))
        self.assertTrue(restricted.startswith('restrict,command="/usr/bin/python3 '+str(destination/'file_agent.py')+'"'))
        for path in [ssh/'authorized_keys',destination/'file_agent.py',destination/'file_access.py']:
            self.assertEqual(stat.S_IMODE(path.stat().st_mode),0o600)
        self.assertEqual(stat.S_IMODE(destination.stat().st_mode),0o700)

    def test_remote_installer_rejects_symlink_directory(self):
        home=self.root/'air'; home.mkdir(); (home/'.local').symlink_to(self.root)
        payload={'public_key':'ssh-ed25519 AAAATESTKEY personalos-files-readonly','agent':'pass','core':'pass'}
        stream=io.TextIOWrapper(io.BytesIO(json.dumps(payload).encode()))
        with mock.patch.object(install_files_remote,'HOME',home), mock.patch.object(install_files_remote,'DESTINATION',home/'.local/share/personal-os-files'), \
             mock.patch.object(pathlib.Path,'home',return_value=home), mock.patch.object(sys,'stdin',stream):
            with self.assertRaisesRegex(ValueError,'symbolic-link'): install_files_remote.main()
        self.assertFalse((self.root/'share').exists())





if __name__=='__main__':unittest.main(verbosity=2)
