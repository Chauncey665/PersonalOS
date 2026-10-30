import datetime as dt
import hashlib
import json
import pathlib
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import urllib.request
from unittest import mock

sys.path.insert(0,str(pathlib.Path(__file__).resolve().parents[1]/'server'))
from personal_os import Store, Worker, Handler, ThreadingHTTPServer
from push import APNsProvider
from remote_sources import ProjectRemoteSource, RemoteSourceError, bounded_output, PROJECT_URI, PROJECT_HOST, PROJECT_USER, PROJECT_REMOTE_ROOT, MAX_OUTPUT


class RemoteSourceTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory(); self.home=pathlib.Path(self.temp.name).resolve()
        self.key=self.home/'read only key';self.key.write_text('isolated fake private key');self.key.chmod(0o600)
        self.known=self.home/'known hosts';self.known.write_text('isolated fake pinned host key')
        self.config=self.home/'project-source.json'
        self.config_data={'host':PROJECT_HOST,'user':PROJECT_USER,'remote_root':PROJECT_REMOTE_ROOT,'key_path':str(self.key),'known_hosts_path':str(self.known)}
        self.config.write_text(json.dumps(self.config_data));self.config.chmod(0o600)
        self.payload={'version':1,'remote_root':PROJECT_REMOTE_ROOT,'fetched_at':dt.datetime.now(dt.timezone.utc).isoformat(),
                      'hits':[{'path':'notes/architecture.md','modified':10,'snippet':'架构设计'},
                              {'path':'README.md','modified':20,'snippet':'The current project overview'}],
                      'scanned':2,'truncated':False}
        self.runner=mock.Mock(side_effect=lambda *args,**kwargs:json.dumps(self.payload).encode())
        self.source=ProjectRemoteSource(self.config,runner=self.runner)
    def tearDown(self):self.temp.cleanup()

    def test_ssh_is_fixed_read_only_and_search_is_fresh_each_time(self):
        first=self.source.search('PROJECT');self.assertEqual(pathlib.PurePosixPath(first['hits'][0]['path']).name,'README.md')
        self.assertEqual(len(first['hits']),2);self.assertTrue(first['read_only']);self.assertEqual(first['remote_root'],PROJECT_REMOTE_ROOT)
        self.payload['hits'][0]['modified']=30
        second=self.source.search('');self.assertEqual(pathlib.PurePosixPath(second['hits'][0]['path']).name,'architecture.md')
        self.assertEqual(self.runner.call_count,2)
        args,kwargs=self.runner.call_args;argv=args[0]
        self.assertEqual(argv[-1],PROJECT_USER+'@'+PROJECT_HOST)
        self.assertIn('StrictHostKeyChecking=yes',argv);self.assertIn('IdentitiesOnly=yes',argv);self.assertIn('BatchMode=yes',argv)
        self.assertIn('ProxyCommand=/Applications/Tailscale.app/Contents/MacOS/Tailscale nc %h %p',argv)
        self.assertIn('UserKnownHostsFile="'+str(self.known)+'"',argv)
        self.assertEqual(kwargs,{'timeout':20,'max_output':MAX_OUTPUT})
        self.assertNotIn('PROJECT',argv)

    def test_openssh_accepts_known_hosts_path_containing_spaces(self):
        argv=self.source._arguments(self.config_data)
        inspected=subprocess.run([argv[0],'-G']+argv[1:],capture_output=True,timeout=3)
        self.assertEqual(inspected.returncode,0)
        line=next(line for line in inspected.stdout.decode().splitlines() if line.startswith('userknownhostsfile '))
        self.assertIn(str(self.known),line)

    def test_query_filters_only_exported_document_name_and_snippet(self):
        self.assertEqual(len(self.source.search('架构')['hits']),1)
        self.assertEqual(len(self.source.search('readme')['hits']),1)
        self.assertEqual(self.source.search('absent')['hits'],[])
        self.assertEqual(len(self.source.search('PrOjEcT')['hits']),2)

    def test_config_restricts_host_root_and_private_key_permissions(self):
        for field,value in [('host','example.com'),('user','root'),('remote_root','/')]:
            config=dict(self.config_data);config[field]=value;self.config.write_text(json.dumps(config))
            self.assertFalse(self.source.status()['project_source_configured']);self.assertEqual(self.source.roots(),[])
            with self.assertRaises(RemoteSourceError):self.source.search('PROJECT')
        self.runner.assert_not_called()
        self.config.write_text(json.dumps(self.config_data));self.key.chmod(0o644)
        self.assertEqual(self.source.status()['project_source_status'],'private_permissions_required')
        self.runner.assert_not_called()

    def test_export_path_and_data_validation_rejects_untrusted_values(self):
        for path in ('../secret.md','/etc/private.md','notes/../../secret.txt','line\nbreak.md','script.py'):
            self.payload['hits'][0]['path']=path
            with self.assertRaises(RemoteSourceError):self.source.search('PROJECT')
        self.payload['hits'][0]['path']='notes/architecture.md'
        self.payload['hits'][0]['modified']=float('nan')
        with self.assertRaises(RemoteSourceError):self.source.search('PROJECT')
        self.payload['hits'][0]['modified']=1;self.payload['hits'][0]['snippet']='x'*501
        with self.assertRaises(RemoteSourceError):self.source.search('PROJECT')
        self.runner.side_effect=None;self.runner.return_value=b'x'*(MAX_OUTPUT+1)
        with self.assertRaises(RemoteSourceError):self.source.search('PROJECT')

    def test_disconnected_source_never_reuses_previous_success(self):
        self.assertEqual(len(self.source.search('PROJECT')['hits']),2)
        self.runner.side_effect=RemoteSourceError('连接 MacBook Air 超时；本次未提供 PROJECT 检索结果。')
        with self.assertRaises(RemoteSourceError):self.source.search('PROJECT')
        self.assertEqual(self.runner.call_count,2)

    def test_api_roots_status_and_worker_use_read_only_source(self):
        store=Store(self.home/'state');store.remote_source=self.source
        auth='isolated-auth-token';digest=hashlib.sha256(auth.encode()).hexdigest()
        with store.db() as c:c.execute('INSERT INTO devices VALUES(?,?)',(digest,'test'))
        server=ThreadingHTTPServer(('127.0.0.1',0),Handler);server.store=store;server.push_provider=APNsProvider(self.home/'missing-push.json')
        http=threading.Thread(target=server.serve_forever,daemon=True);http.start()
        worker=Worker(store);thread=threading.Thread(target=worker.run,daemon=True);thread.start()
        base='http://127.0.0.1:'+str(server.server_port)
        def api(path,body=None):
            req=urllib.request.Request(base+path,None if body is None else json.dumps(body).encode(),{'Authorization':'Bearer '+auth,'Content-Type':'application/json'})
            with urllib.request.urlopen(req,timeout=3) as response:return json.load(response)
        try:
            roots=api('/roots');self.assertEqual(roots,[{'id':'project-air','path':PROJECT_URI,'name':'PROJECT · MacBook Air（只读）','read_only':True}])
            status=api('/status');self.assertTrue(status['project_source_configured']);self.assertNotIn(str(self.key),json.dumps(status))
            ident=api('/tasks',{'text':'查找 PROJECT 最新资料','root':PROJECT_URI,'query':'PROJECT'})['id']
            deadline=time.time()+3
            while time.time()<deadline:
                task=store.task(ident)
                if task['state'] in ('completed','failed'):break
                time.sleep(.03)
            self.assertEqual(task['state'],'completed');result=json.loads(task['result']);self.assertEqual(result['source'],'project-air')
            self.assertEqual(len(result['hits']),2)
            count=self.runner.call_count
            with self.assertRaisesRegex(ValueError,'只读'):store.plan_files(PROJECT_URI)
            self.assertEqual(self.runner.call_count,count)
        finally:
            worker.stop.set();thread.join(timeout=3);server.shutdown();server.server_close();http.join(timeout=2)


class BoundedTransportTests(unittest.TestCase):
    def test_actual_subprocess_output_limit(self):
        with self.assertRaises(RemoteSourceError):bounded_output([sys.executable,'-c','import sys;sys.stdout.write("x"*1025)'],max_output=1024,timeout=2)
    def test_actual_subprocess_timeout(self):
        started=time.monotonic()
        with self.assertRaises(RemoteSourceError):bounded_output([sys.executable,'-c','import time;time.sleep(30)'],timeout=.15)
        self.assertLess(time.monotonic()-started,3)
    def test_stderr_does_not_leak_into_failure(self):
        marker='DO_NOT_EXPOSE_KEY_OR_PASSWORD'
        with self.assertRaises(RemoteSourceError) as error:
            bounded_output([sys.executable,'-c','import sys;sys.stderr.write("'+marker+'");sys.exit(3)'],timeout=2)
        self.assertNotIn(marker,str(error.exception))
    def test_actual_subprocess_success(self):
        result=bounded_output([sys.executable,'-c','print("{\\"ok\\":true}")'],timeout=2)
        self.assertEqual(json.loads(result),{'ok':True})

if __name__=='__main__':unittest.main(verbosity=2)
