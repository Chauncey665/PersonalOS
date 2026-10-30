"""Guard the temporary --home boundary without reading any real credential config."""
import contextlib
import hashlib
import io
import json
import pathlib
import sys
import tempfile
import threading
import unittest
import urllib.request
from unittest import mock

sys.path.insert(0,str(pathlib.Path(__file__).resolve().parents[1]/'server'))
import personal_os
from personal_os import Store, Handler, ThreadingHTTPServer
from push import APNsProvider, PushDispatcher
from remote_sources import ProjectRemoteSource


class ConfigIsolationTests(unittest.TestCase):
    def setUp(self):
        self.temporary=tempfile.TemporaryDirectory()
        self.home=pathlib.Path(self.temporary.name).resolve()/'isolated state'
        self.seen_push=[];self.seen_project=[]
        # Intercept BEFORE any exists/stat/read: touching the real support config is impossible.
        def guarded_push(provider):
            self.assertEqual(provider.config_path,self.home/'push.json','APNs escaped the selected --home')
            self.seen_push.append(provider.config_path)
            return None,'not_configured'
        def guarded_project(source):
            self.assertEqual(source.config_path,self.home/'project-source.json','PROJECT escaped the selected --home')
            self.seen_project.append(source.config_path)
            return None,'not_configured'
        self.guards=contextlib.ExitStack()
        self.guards.enter_context(mock.patch.object(APNsProvider,'_config',guarded_push))
        self.guards.enter_context(mock.patch.object(ProjectRemoteSource,'_config',guarded_project))
        # APNs and SSH transports must remain completely idle in these tests.
        self.transport=self.guards.enter_context(mock.patch('subprocess.run',side_effect=AssertionError('Unexpected subprocess')))
        self.ssh=self.guards.enter_context(mock.patch('remote_sources.bounded_output',side_effect=AssertionError('Unexpected SSH')))
    def tearDown(self):
        self.guards.close();self.temporary.cleanup()

    def test_main_custom_home_binds_both_configs_without_global_fallback(self):
        class FakeServer:
            def serve_forever(inner):
                self.assertEqual(inner.store.home,self.home)
                self.assertFalse(inner.push_provider.status()['push_configured'])
                self.assertFalse(inner.store.remote_source.status()['project_source_configured'])
            def server_close(inner):pass
        server=FakeServer()
        with mock.patch.object(sys,'argv',['personal_os.py','--home',str(self.home),'serve','--port','0']), \
             mock.patch.object(personal_os,'ThreadingHTTPServer',return_value=server), \
             mock.patch.object(personal_os.threading,'Thread') as thread, \
             contextlib.redirect_stdout(io.StringIO()):
            personal_os.main()
        self.assertEqual(self.seen_push,[self.home/'push.json'])
        self.assertEqual(self.seen_project,[self.home/'project-source.json'])
        self.transport.assert_not_called();self.ssh.assert_not_called()

    def test_handler_fallback_and_dispatcher_use_store_home(self):
        store=Store(self.home)
        auth='isolated-test-authentication';digest=hashlib.sha256(auth.encode()).hexdigest()
        with store.db() as c:c.execute('INSERT INTO devices VALUES(?,?)',(digest,'isolated fixture'))
        server=ThreadingHTTPServer(('127.0.0.1',0),Handler);server.store=store
        # Deliberately omit server.push_provider to exercise Handler's fallback.
        thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
        try:
            request=urllib.request.Request('http://127.0.0.1:'+str(server.server_port)+'/status',headers={'Authorization':'Bearer '+auth})
            with urllib.request.urlopen(request,timeout=3) as response:status=json.load(response)
            self.assertFalse(status['push_configured']);self.assertFalse(status['project_source_configured'])
            dispatcher=PushDispatcher(store)
            self.assertEqual(dispatcher.provider.config_path,self.home/'push.json')
            self.assertEqual(dispatcher.tick(),0)
            self.assertGreaterEqual(len(self.seen_push),2);self.assertEqual(self.seen_project,[self.home/'project-source.json'])
            self.transport.assert_not_called();self.ssh.assert_not_called()
        finally:
            server.shutdown();server.server_close();thread.join(timeout=2)

if __name__=='__main__':unittest.main(verbosity=2)
