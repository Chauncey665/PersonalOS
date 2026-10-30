import hashlib, importlib.util, json, pathlib, secrets, subprocess, sys, tempfile, threading, time, unittest, urllib.request, urllib.error
sys.path.insert(0,str(pathlib.Path(__file__).resolve().parents[1]/'server'))
from personal_os import Store, Worker, Handler, ThreadingHTTPServer
from push import APNsProvider

class SystemTest(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory(); self.home=pathlib.Path(self.tmp.name).resolve()
        self.s=Store(self.home/'state'); self.root=self.home/'selected'; self.root.mkdir()
        with self.s.db() as c: c.execute('INSERT INTO roots VALUES(?)',(str(self.root),))
        self.server=ThreadingHTTPServer(('127.0.0.1',0),Handler); self.server.store=self.s; self.server.push_provider=APNsProvider(self.home/'missing-push.json')
        threading.Thread(target=self.server.serve_forever,daemon=True).start()
        self.worker=Worker(self.s); self.worker_thread=threading.Thread(target=self.worker.run,daemon=True); self.worker_thread.start()
        self.token=''; self.base='http://127.0.0.1:'+str(self.server.server_port)
    def tearDown(self):
        self.worker.stop.set(); self.worker_thread.join(timeout=5); self.server.shutdown(); self.server.server_close(); self.tmp.cleanup()
    def api(self,path,body=None):
        req=urllib.request.Request(self.base+path,None if body is None else json.dumps(body).encode(),{'Authorization':'Bearer '+self.token,'Content-Type':'application/json'})
        with urllib.request.urlopen(req,timeout=130) as r: return json.load(r)
    def pair(self):
        code=secrets.token_urlsafe(18)
        with self.s.db() as c: c.execute('INSERT INTO pairing VALUES(?,?)',(hashlib.sha256(code.encode()).hexdigest(),time.time()+60))
        self.token=self.api('/pair',{'code':code})['token']; return code
    def wait(self,ident,states=('completed','failed','awaiting_confirmation','scheduled')):
        deadline=time.time()+125
        while time.time()<deadline:
            row=next(t for t in self.api('/tasks') if t['id']==ident)
            if row['state'] in states: return row
            time.sleep(.2)
        self.fail('任务超时')
    def submit(self,**b): return self.api('/tasks',b)['id']
    def test_auth_single_use_and_revocation(self):
        with self.assertRaises(urllib.error.HTTPError) as err: self.api('/tasks')
        self.assertEqual(err.exception.code,401)
        code=self.pair()
        with self.assertRaises(urllib.error.HTTPError): self.api('/pair',{'code':code})
        with self.s.db() as c:
            stored=c.execute('SELECT token FROM devices').fetchone()[0]; self.assertNotEqual(stored,self.token)
            c.execute('DELETE FROM devices')
        with self.assertRaises(urllib.error.HTTPError): self.api('/tasks')
    @unittest.skipUnless(__import__("os").environ.get("PERSONALOS_LIVE_TESTS")=="1", "Set PERSONALOS_LIVE_TESTS=1 with local Ollama to run model integration")
    def test_four_scenarios_and_audit(self):
        self.pair()
        (self.root/'PROJECT-notes.txt').write_text('PROJECT project reference')
        (self.root/'other.pdf').write_text('sample')
        # Actual local Ollama inference, no mock.
        daily=self.wait(self.submit(text='今天应该先做什么'))
        self.assertEqual(daily['state'],'completed',daily['result']); self.assertGreater(len(daily['result']),10)
        search=self.wait(self.submit(text='查找 PROJECT 最新资料',root=str(self.root)))
        self.assertIn('PROJECT-notes.txt',search['result'])
        ident=self.submit(text='整理选定文件',root=str(self.root)); preview=self.wait(ident)
        self.assertEqual(preview['state'],'awaiting_confirmation'); self.assertTrue((self.root/'other.pdf').exists())
        self.api('/tasks/'+ident+'/approve',{}); self.assertTrue((self.root/'PersonalOS-pdf/other.pdf').exists())
        self.api('/tasks/'+ident+'/undo',{}); self.assertTrue((self.root/'other.pdf').exists())
        due=__import__('datetime').datetime.now(__import__('datetime').timezone.utc)+__import__('datetime').timedelta(seconds=2)
        reminder=self.submit(text='明天提醒继续项目',due=due.isoformat()); self.assertEqual(self.wait(reminder)['state'],'scheduled')
        self.assertEqual(self.wait(reminder,('completed',))['state'],'completed')
        events=[r['event'] for r in self.api('/audit')]
        for event in ['local_model','preview','move','undo_move','reminder_due']: self.assertIn(event,events)
        # Persistence after reopening the database.
        reopened=Store(self.s.home); self.assertEqual(len(reopened.tasks()),4)
        report={'daily_result':daily['result'],'search_result':json.loads(search['result']),'checks':['live_local_model','authenticated_HTTP','local_search_fixture','preview_before_move','confirmed_move','undo','due_reminder','persistent_history','audit']}
        out=self.home/'verification.json'; out.write_text(json.dumps(report,ensure_ascii=False,indent=2))
    def test_file_boundaries_and_modified_conflict(self):
        self.pair(); outside=self.home/'private'; outside.mkdir(); (outside/'secret.txt').write_text('secret')
        failed=self.wait(self.submit(text='整理文件',root=str(outside))); self.assertEqual(failed['state'],'failed')
        (self.root/'escape').symlink_to(outside,target_is_directory=True)
        (self.root/'x.txt').write_text('before')
        ident=self.submit(text='整理文件',root=str(self.root)); plan=self.wait(ident)
        self.assertEqual(len(json.loads(plan['plan'])['moves']),1)
        (self.root/'x.txt').write_text('changed')
        with self.assertRaises(urllib.error.HTTPError): self.api('/tasks/'+ident+'/approve',{})
        self.assertEqual((self.root/'x.txt').read_text(),'changed')
    def test_concurrent_approval_has_single_winner(self):
        self.pair(); (self.root/'x.txt').write_text('one')
        ident=self.submit(text='整理文件',root=str(self.root)); self.wait(ident)
        import concurrent.futures
        def approve():
            try: self.api('/tasks/'+ident+'/approve',{}); return 'ok'
            except urllib.error.HTTPError: return 'rejected'
        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
            results=list(pool.map(lambda _:approve(),range(2)))
        self.assertEqual(sorted(results),['ok','rejected'])
        self.assertEqual((self.root/'PersonalOS-txt/x.txt').read_text(),'one')

    def test_cancel_and_invalid_input(self):
        self.pair()
        ident=self.submit(text='明天上午九点提醒继续项目'); self.wait(ident); self.api('/tasks/'+ident+'/cancel',{})
        self.assertEqual(next(t for t in self.api('/tasks') if t['id']==ident)['state'],'cancelled')
        with self.assertRaises(urllib.error.HTTPError): self.submit(text='')
        with self.assertRaises(urllib.error.HTTPError): self.api('/tasks/'+ident+'/approve',{})

if __name__=='__main__': unittest.main(verbosity=2)
