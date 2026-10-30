import base64
import contextlib
import hashlib
import io
import json
import os
import pathlib
import sys
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
from unittest import mock

sys.path.insert(0,str(pathlib.Path(__file__).resolve().parents[1]/'server'))
import personal_os
from personal_os import Store,Handler,ThreadingHTTPServer
from push import APNsProvider
from file_access import LocalFileAccess,RemoteFileAccess,FileAccessService,FileAccessError,CHUNK_BYTES,AIR_HOST,AIR_USER,AIR_HOME,ssh_exchange


class LocalAccessTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.home=pathlib.Path(self.temp.name).resolve();self.files=self.home/'files';self.files.mkdir()
        self.access=LocalFileAccess(self.home)
    def tearDown(self):self.temp.cleanup()
    def test_browse_pagination_reaches_every_ordinary_item(self):
        for i in range(230):(self.files/('document-%03d.txt'%i)).write_text(str(i))
        (self.files/'.project-notes').mkdir();(self.files/'.env').write_text('do not publish')
        (self.files/'link').symlink_to(self.home,target_is_directory=True)
        paths=[];cursor=''
        while True:
            page=self.access.handle('list',str(self.files),cursor=cursor)
            self.assertLessEqual(len(page['entries']),100);paths.extend(item['path'] for item in page['entries'])
            cursor=page['next_cursor']
            if not cursor:break
        self.assertEqual(len(paths),231);self.assertEqual(len(set(paths)),231)
        self.assertNotIn(str(self.files/'.env'),paths);self.assertIn(str(self.files/'.project-notes'),paths)
    def test_directory_changes_invalidate_cursor(self):
        for i in range(101):(self.files/str(i)).write_text('x')
        cursor=self.access.handle('list',str(self.files))['next_cursor']
        (self.files/'new').write_text('new')
        with self.assertRaises(FileAccessError) as error:self.access.handle('list',str(self.files),cursor=cursor)
        self.assertEqual(error.exception.code,'directory_changed')
    def test_secret_paths_rejected_but_normal_library_and_dot_projects_visible(self):
        allowed=self.files/'Library'/'Projects';allowed.mkdir(parents=True);(allowed/'notes.txt').write_text('hello')
        secret=self.files/'Library'/'Keychains';secret.mkdir();(secret/'test.txt').write_text('secret')
        self.assertTrue(self.access.handle('list',str(allowed))['entries'])
        for path in (secret,self.files/'.ssh'/'config',self.files/'config.p8',self.files/'.env',self.files/'Library'/'Application Support'/'Google'/'Chrome'/'Default'):
            with self.assertRaises(FileAccessError) as error:self.access.handle('read',str(path))
            self.assertEqual(error.exception.code,'sensitive_path')
    def test_no_path_traversal_or_symlink_following(self):
        outside=self.home/'outside.txt';outside.write_text('OUTSIDE')
        (self.files/'link.txt').symlink_to(outside)
        (self.files/'linked-dir').symlink_to(self.home,target_is_directory=True)
        for path in (str(self.files/'link.txt'),str(self.files/'linked-dir'/'outside.txt'),str(self.files)+'/../outside.txt'):
            with self.assertRaises(FileAccessError):self.access.handle('read',path)
    def test_file_descriptor_protects_against_path_swap_during_read(self):
        file=self.files/'normal.txt';file.write_text('original')
        outside=self.home/'outside.txt';outside.write_text('MUST_NOT_RETURN')
        original=self.access._read_up_to
        def replaced(fd,size):
            file.unlink();file.symlink_to(outside)
            return original(fd,size)
        with mock.patch.object(self.access,'_read_up_to',side_effect=replaced):
            try:
                result=self.access.handle('read',str(file));self.assertNotIn('MUST_NOT_RETURN',result['content'])
            except FileAccessError as error:self.assertEqual(error.code,'file_changed')
    def test_preview_is_bounded_and_large_download_has_no_total_size_limit(self):
        file=self.files/'large.bin'
        with file.open('wb') as stream:stream.seek(12*1024*1024);stream.write(b'Z')
        preview=self.access.handle('read',str(file));self.assertTrue(preview['download_allowed']);self.assertFalse(preview['previewable']);self.assertTrue(preview['truncated'])
        first=self.access.handle('download',str(file),max_bytes=100)
        self.assertEqual(len(base64.b64decode(first['content_base64'])),100);self.assertFalse(first['eof'])
        last=self.access.handle('download',str(file),offset=12*1024*1024,max_bytes=1,etag=first['etag'])
        self.assertEqual(base64.b64decode(last['content_base64']),b'Z');self.assertTrue(last['eof']);self.assertEqual(last['size'],12*1024*1024+1)
    def test_chunk_etag_rejects_changed_or_missing_identity(self):
        file=self.files/'text.txt';file.write_text('abcdefghijklmnop')
        first=self.access.handle('download',str(file),max_bytes=5)
        with self.assertRaises(FileAccessError):self.access.handle('download',str(file),offset=5,max_bytes=5)
        modified=file.stat().st_mtime_ns
        file.write_text('ABCDEFGHIJKLMNOP');os.utime(file,ns=(modified,modified))
        with self.assertRaises(FileAccessError) as error:self.access.handle('download',str(file),offset=5,max_bytes=5,etag=first['etag'])
        self.assertEqual(error.exception.status,409)
    def test_sensitive_content_not_returned_even_in_boundary_chunks(self):
        file=self.files/'notes.txt';file.write_bytes(b'A'*10000+b'-----BEGIN PRIVATE KEY-----\nexample')
        for op,kwargs in [('read',{}),('download',{'offset':0,'max_bytes':5000})]:
            with self.assertRaises(FileAccessError) as error:self.access.handle(op,str(file),**kwargs)
            self.assertEqual(error.exception.code,'sensitive_content')
    def test_fifo_cannot_block_preview(self):
        fifo=self.files/'pipe';os.mkfifo(fifo)
        started=time.monotonic()
        with self.assertRaises(FileAccessError):self.access.handle('read',str(fifo))
        self.assertLess(time.monotonic()-started,1)
    def test_search_stays_within_budget_and_skips_sensitive_subtree(self):
        (self.files/'.ssh').mkdir();(self.files/'.ssh'/'find-me.txt').write_text('hidden')
        (self.files/'normal').mkdir();(self.files/'normal'/'find-me.txt').write_text('visible')
        result=self.access.handle('search',str(self.files),query='find-me')
        self.assertEqual(len(result['entries']),1);self.assertIn('/normal/',result['entries'][0]['path'])
        with mock.patch('file_access.SEARCH_SCAN_BUDGET',1):limited=self.access.handle('search',str(self.files),query='find')
        self.assertLessEqual(limited['scanned'],1);self.assertTrue(limited['truncated'])
    def test_permission_denial_is_explicit_without_os_error_text(self):
        file=self.files/'text.txt';file.write_text('hello')
        with mock.patch('file_access.os.open',side_effect=PermissionError(13,'private OS details')):
            with self.assertRaises(FileAccessError) as error:self.access.handle('read',str(file))
        self.assertEqual(error.exception.status,403);self.assertEqual(error.exception.code,'permission_denied');self.assertNotIn('private OS details',error.exception.message)


class FileApiTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.home=pathlib.Path(self.temp.name).resolve();self.s=Store(self.home/'state')
        self.auth='isolated-file-api-auth'
        with self.s.db() as c:c.execute('INSERT INTO devices VALUES(?,?)',(hashlib.sha256(self.auth.encode()).hexdigest(),'isolated'))
        self.server=ThreadingHTTPServer(('127.0.0.1',0),Handler);self.server.store=self.s;self.server.push_provider=APNsProvider(self.home/'missing-push.json')
        self.thread=threading.Thread(target=self.server.serve_forever,daemon=True);self.thread.start();self.base='http://127.0.0.1:'+str(self.server.server_port)
    def tearDown(self):self.server.shutdown();self.server.server_close();self.thread.join(timeout=2);self.temp.cleanup()
    def api(self,path,body=None,auth=True):
        request=urllib.request.Request(self.base+path,None if body is None else json.dumps(body).encode(),{'Authorization':'Bearer '+self.auth if auth else '','Content-Type':'application/json'})
        with urllib.request.urlopen(request,timeout=3) as response:return json.load(response)
    def enable(self):
        config=self.s.home/'files-access.json';config.write_text('{"enabled":true}');config.chmod(0o600)
    def test_auth_and_explicit_local_enable_are_both_required(self):
        with self.assertRaises(urllib.error.HTTPError) as error:self.api('/files/devices',auth=False)
        self.assertEqual(error.exception.code,401)
        self.assertFalse(self.api('/files/devices')['devices'][0]['available'])
        with self.assertRaises(urllib.error.HTTPError) as error:self.api('/files/list',{'device':'mini','path':str(self.home)})
        self.assertEqual(error.exception.code,403)
        self.enable();self.assertTrue(self.api('/files/devices')['devices'][0]['available'])
        self.assertIn('entries',self.api('/files/list',{'device':'mini','path':str(self.home)}))
        self.assertEqual(self.s.roots(),[])  # Broad read access never grants organize/write.
    def test_read_and_download_audit_path_but_never_file_contents(self):
        self.enable();file=self.home/'note.txt';file.write_text('unique private document contents')
        preview=self.api('/files/read',{'device':'mini','path':str(file)})
        self.assertEqual(preview['content'],'unique private document contents')
        download=self.api('/files/download',{'device':'mini','path':str(file),'offset':'0','max_bytes':'10'})
        self.assertEqual(download['next_offset'],10)
        audit=json.dumps(self.s.audits());self.assertIn(str(file),audit);self.assertNotIn('unique private document contents',audit)
        self.assertNotIn(download['content_base64'],audit)
    def test_files_enable_cli_is_audited_and_preserves_write_roots(self):
        with mock.patch.object(sys,'argv',['personal_os.py','--home',str(self.s.home),'files-enable']),contextlib.redirect_stdout(io.StringIO()):personal_os.main()
        self.assertTrue(self.s.file_access.mini_enabled());self.assertEqual(self.s.roots(),[])
        self.assertIn('file_access_enabled',[row['event'] for row in self.s.audits()])


class RemoteFileTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.home=pathlib.Path(self.temp.name).resolve()
        self.key=self.home/'key';self.key.write_text('isolated key fixture');self.key.chmod(0o600)
        self.known=self.home/'known hosts';self.known.write_text('isolated known hosts')
        self.config=self.home/'air-files-source.json';self.config.write_text(json.dumps({'enabled':True,'host':AIR_HOST,'user':AIR_USER,'remote_home':AIR_HOME,'key_path':str(self.key),'known_hosts_path':str(self.known)}));self.config.chmod(0o600)
    def tearDown(self):self.temp.cleanup()
    def test_remote_uses_single_readonly_rpc_with_no_command_argument(self):
        calls=[]
        def runner(args,payload,**kwargs):
            calls.append((args,payload,kwargs))
            return json.dumps({'version':1,'ok':True,'result':{'path':AIR_HOME,'parent':'/Users','entries':[],'next_cursor':None,'truncated':False}}).encode()
        source=RemoteFileAccess(self.config,runner)
        result=source.handle('list',AIR_HOME);self.assertEqual(result['entries'],[])
        args,payload,kwargs=calls[0];self.assertEqual(args[-1],AIR_USER+'@'+AIR_HOST)
        self.assertIn('StrictHostKeyChecking=yes',args);self.assertIn('BatchMode=yes',args);self.assertEqual(payload['operation'],'list');self.assertEqual(kwargs['max_output'],2*1024*1024)
    def test_remote_permission_error_is_sanitized(self):
        response={'version':1,'ok':False,'status':403,'code':'permission_denied','error':'do not reveal remote error contents'}
        source=RemoteFileAccess(self.config,lambda *a,**kw:json.dumps(response).encode())
        with self.assertRaises(FileAccessError) as error:source.handle('list',AIR_HOME)
        self.assertEqual(error.exception.code,'permission_denied');self.assertNotIn(response['error'],error.exception.message)
    def test_rpc_actual_stdin_json_and_bounded_stdout(self):
        program='import json,sys;x=json.loads(sys.stdin.readline());print(json.dumps({"version":1,"ok":True,"result":x}))'
        result=ssh_exchange([sys.executable,'-c',program],{'version':1,'operation':'list','path':'/'},timeout=2)
        self.assertEqual(json.loads(result)['result']['path'],'/')
        with self.assertRaises(FileAccessError):ssh_exchange([sys.executable,'-c','print("x"*1025)'],{},timeout=2,max_output=1024)
    def test_default_service_does_not_touch_global_air_config(self):
        service=FileAccessService(self.home/'test-state')
        self.assertEqual(service.remote.config_path,self.home/'test-state'/'air-files-source.json')
        self.assertFalse(service.remote.available())

if __name__=='__main__':unittest.main(verbosity=2)
