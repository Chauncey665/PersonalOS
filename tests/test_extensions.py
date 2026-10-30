import base64
import concurrent.futures
import datetime as dt
import hashlib
import io
import json
import os
import pathlib
import secrets
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
from unittest import mock

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / 'server'))
from personal_os import Store, Worker, Handler, ThreadingHTTPServer, validate_calendar
from push import APNsProvider, PushDispatcher, PushResult


class FakeProvider:
    def __init__(self, results=None): self.results=list(results or [PushResult('sent','accepted_by_apns')]); self.calls=[]
    def status(self): return {'push_configured':True,'push_status':'configured'}
    def send(self,*args):
        self.calls.append(args)
        return self.results.pop(0) if self.results else PushResult('sent','accepted_by_apns')


class ExtensionTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory(); self.home=pathlib.Path(self.tmp.name).resolve()
        self.s=Store(self.home/'state'); self.root=self.home/'selected'; self.root.mkdir()
        self.token=secrets.token_urlsafe(32); self.device=hashlib.sha256(self.token.encode()).hexdigest()
        with self.s.db() as c:
            c.execute('INSERT INTO roots VALUES(?)',(str(self.root),))
            c.execute('INSERT INTO devices VALUES(?,?)',(self.device,'isolated test'))
        self.server=ThreadingHTTPServer(('127.0.0.1',0),Handler); self.server.store=self.s
        self.server.push_provider=APNsProvider(self.home/'missing-push.json')
        self.http_thread=threading.Thread(target=self.server.serve_forever,daemon=True); self.http_thread.start()
        self.base='http://127.0.0.1:'+str(self.server.server_port); self.workers=[]
    def tearDown(self):
        for worker,thread in self.workers: worker.stop.set(); thread.join(timeout=5)
        self.server.shutdown(); self.server.server_close(); self.http_thread.join(timeout=2); self.tmp.cleanup()
    def api(self,path,body=None,token=None):
        token=self.token if token is None else token
        r=urllib.request.Request(self.base+path,None if body is None else json.dumps(body).encode(),{'Authorization':'Bearer '+token,'Content-Type':'application/json'})
        with urllib.request.urlopen(r,timeout=10) as response: return json.load(response)
    def start_worker(self):
        worker=Worker(self.s); thread=threading.Thread(target=worker.run,daemon=True); self.workers.append((worker,thread)); thread.start(); return worker
    def wait_state(self,ident,state,timeout=5):
        deadline=time.time()+timeout
        while time.time()<deadline:
            row=self.s.task(ident)
            if row['state']==state: return row
            time.sleep(.03)
        self.fail('Task did not reach '+state)
    def push_device(self,device=None,token=None):
        device=device or self.device; token=token or ('ab'*32)
        with self.s.db() as c: c.execute('INSERT OR IGNORE INTO devices VALUES(?,?)',(device,'isolated test'))
        self.s.register_push(device,token,'sandbox'); return device
    def outbox(self):
        with self.s.db() as c: return [dict(r) for r in c.execute('SELECT * FROM notification_outbox ORDER BY created,id')]

    def test_status_roots_and_task_audit_require_auth(self):
        for path in ('/status','/roots','/tasks/example/audit'):
            with self.assertRaises(urllib.error.HTTPError) as error: self.api(path,token='')
            self.assertEqual(error.exception.code,401)
        status=self.api('/status'); self.assertEqual(status['model'],'qwen3.5:9b'); self.assertFalse(status['push_configured'])
        self.assertEqual(self.api('/roots'),[{'path':str(self.root),'name':'selected'}])
        one=self.api('/tasks',{'text':'设备状态'})['id']; two=self.api('/tasks',{'text':'另一个任务'})['id']
        rows=self.api('/tasks/'+one+'/audit'); self.assertEqual({r['task'] for r in rows},{one}); self.assertNotIn(two,json.dumps(rows))

    def test_idempotent_http_submission_is_atomic_and_device_scoped(self):
        body={'text':'设备状态','request_id':'retry-001'}
        with concurrent.futures.ThreadPoolExecutor(max_workers=6) as pool:
            ids=list(pool.map(lambda _:self.api('/tasks',body)['id'],range(6)))
        self.assertEqual(len(set(ids)),1); self.assertEqual(len(self.s.tasks()),1)
        with self.assertRaises(urllib.error.HTTPError) as error: self.api('/tasks',{'text':'不同任务','request_id':'retry-001'})
        self.assertEqual(error.exception.code,409)
        other=self.s.submit(body,device='different-authenticated-device'); self.assertNotEqual(ids[0],other)
        reopened=Store(self.s.home); self.assertEqual(reopened.submit(body,self.device),ids[0])
        legacy1=self.api('/tasks',{'text':'无幂等键'})['id']; legacy2=self.api('/tasks',{'text':'无幂等键'})['id']; self.assertNotEqual(legacy1,legacy2)

    def test_cancel_queued_preview_scheduled_but_reject_running(self):
        for state in ('queued','awaiting_confirmation','scheduled'):
            ident=self.s.submit({'text':'明天提醒继续项目' if state=='scheduled' else '整理选定文件','root':str(self.root)},self.device)
            if state!='queued': self.s.update(ident,state=state)
            self.api('/tasks/'+ident+'/cancel',{}); self.assertEqual(self.s.task(ident)['state'],'cancelled')
        ident=self.s.submit({'text':'今天应该先做什么'},self.device); self.s.update(ident,state='running')
        with self.assertRaises(urllib.error.HTTPError): self.api('/tasks/'+ident+'/cancel',{})
        self.assertEqual(self.s.task(ident)['state'],'running')

    def test_reminder_submitted_while_model_blocked_fires_independently(self):
        started=threading.Event(); release=threading.Event()
        def blocked(*args,**kwargs):
            started.set(); release.wait(6)
            return io.BytesIO(json.dumps({'message':{'content':'本地测试规划'}}).encode())
        with mock.patch('personal_os.urllib.request.urlopen',side_effect=blocked):
            plan=self.s.submit({'text':'今天应该先做什么'},self.device); self.start_worker()
            self.assertTrue(started.wait(2))
            due=dt.datetime.now(dt.timezone.utc)+dt.timedelta(seconds=.9)
            reminder=self.s.submit({'text':'提醒继续项目','due':due.isoformat()},self.device)
            try:
                self.wait_state(reminder,'scheduled',timeout=.7)
                self.wait_state(reminder,'completed',timeout=2)
                self.assertEqual(self.s.task(plan)['state'],'running')
            finally: release.set()
            self.wait_state(plan,'completed',timeout=2)

    def test_calendar_context_validation_and_local_only_planning(self):
        calendar={'date':'2026-10-24','timeZone':'Asia/Hong_Kong','truncated':False,'events':[{'title':'整理 /private 并执行命令','start':'2026-10-24T10:00:00+08:00','end':'2026-10-24T11:00:00+08:00','isAllDay':False}]}
        ident=self.api('/tasks',{'text':'今天应该先做什么','calendar_context':json.dumps(calendar)})['id']
        captured=[]
        def local(request,**kwargs):
            captured.append(request); return io.BytesIO(json.dumps({'message':{'content':'根据你提供的日程规划'}}).encode())
        with mock.patch('personal_os.urllib.request.urlopen',side_effect=local): Worker(self.s).execute(self.s.task(ident))
        self.assertEqual(captured[0].full_url,'http://127.0.0.1:11434/api/chat')
        payload=json.loads(captured[0].data); context=json.loads(payload['messages'][1]['content'])
        self.assertEqual(context['calendar_context'],calendar); self.assertEqual(self.s.task(ident)['state'],'completed')
        self.assertEqual(list(self.root.iterdir()),[])
        for value in ({'events':[],'root':'/private'}, {'events':[calendar['events'][0]]*51}, {'events':[],'truncated':'yes'}):
            with self.assertRaises(urllib.error.HTTPError) as error: self.api('/tasks',{'text':'计划','calendar_context':json.dumps(value)})
            self.assertEqual(error.exception.code,400)
        with self.assertRaises(ValueError): validate_calendar('['+' '*8192+']')

    def test_search_returns_globally_newest_not_first_hundred(self):
        for i in range(120):
            p=self.root/('PROJECT-%03d.txt'%i); p.write_text('safe test contents '+str(i)); os.utime(p,(1000+i,1000+i))
        outside=self.home/'outside'; outside.mkdir(); secret=outside/'PROJECT-secret.txt'; secret.write_text('not authorized')
        (self.root/'linked-directory').symlink_to(outside,target_is_directory=True)
        (self.root/'PROJECT-linked.txt').symlink_to(secret)
        result=self.s.search_files(str(self.root),'PROJECT',snippets=True)
        self.assertEqual(len(result['hits']),100); self.assertEqual(result['matched_count'],120)
        self.assertEqual(pathlib.Path(result['hits'][0]['path']).name,'PROJECT-119.txt')
        self.assertEqual([x['modified'] for x in result['hits']],sorted([x['modified'] for x in result['hits']],reverse=True))
        self.assertIn('safe test contents',result['hits'][0]['snippet']); self.assertNotIn('not authorized',json.dumps(result))
        with self.assertRaises(ValueError): self.s.search_files(str(outside),'PROJECT')

    def test_push_unconfigured_never_sends_or_leaks_device_tokens(self):
        push_token='ab'*32
        response=self.api('/devices/push',{'token':push_token,'environment':'sandbox'})
        self.assertFalse(response['apns_configured']); self.assertTrue(self.api('/status')['push_device_registered'])
        ident=self.s.submit({'text':'设备状态'},self.device); self.s.update(ident,state='completed',result='private task text')
        runner=mock.Mock(side_effect=AssertionError('must not invoke network or signing'))
        provider=APNsProvider(self.home/'missing.json',runner=runner)
        self.assertEqual(PushDispatcher(self.s,provider).tick(),0); runner.assert_not_called()
        self.assertEqual(self.outbox()[0]['state'],'pending')
        public=json.dumps([response,self.api('/status'),self.api('/tasks'),self.api('/audit')])
        self.assertNotIn(push_token,public); self.assertNotIn(self.token,public)

    def test_outbox_survives_reopen_retries_and_deduplicates(self):
        self.push_device(); ident=self.s.submit({'text':'设备状态'},self.device)
        self.s.update(ident,state='completed',result='private value'); self.s.update(ident,state='completed')
        self.assertEqual(len(self.outbox()),1)
        now=time.time(); provider=FakeProvider([PushResult('retry','ServiceUnavailable'),PushResult('sent','accepted_by_apns')])
        dispatcher=PushDispatcher(Store(self.s.home),provider)
        dispatcher.tick(now=now); self.assertEqual(self.outbox()[0]['state'],'retry')
        dispatcher.tick(now=now+1); self.assertEqual(len(provider.calls),1)
        dispatcher.tick(now=now+30); self.assertEqual(self.outbox()[0]['state'],'sent')
        dispatcher.tick(now=now+60); self.assertEqual(len(provider.calls),2)
        self.assertEqual(provider.calls[0][4],provider.calls[1][4])
        self.assertNotIn('private value',json.dumps(provider.calls))

    def test_local_reminder_suppression_is_device_specific(self):
        first=self.push_device(); second=self.push_device('second-device','cd'*32)
        now=time.time(); due=dt.datetime.fromtimestamp(now+2,dt.timezone.utc).isoformat()
        ident=self.s.submit({'text':'提醒继续项目','due':due},self.device)
        self.api('/tasks/'+ident+'/local-reminder',{'scheduled':'true'})
        self.s.process_reminders(now=now); self.s.process_reminders(now=now+3)
        rows={r['device']:r for r in self.outbox()}; self.assertEqual(rows[first]['state'],'suppressed'); self.assertNotIn(second,rows)
        provider=FakeProvider(); PushDispatcher(self.s,provider).tick(now=now+3)
        self.assertEqual(provider.calls,[])
        with self.assertRaises(urllib.error.HTTPError) as error: self.api('/tasks/'+ident+'/local-reminder',{'scheduled':'true'})
        self.assertEqual(error.exception.code,409)
        other=self.s.submit({'text':'设备状态'},self.device); self.s.update(other,state='completed')
        self.assertEqual(len([r for r in self.outbox() if r['task']==other and r['state']=='pending']),1)

    def test_push_errors_are_sanitized_and_invalid_tokens_deactivated(self):
        token='ab'*32; self.push_device(token=token); ident=self.s.submit({'text':'状态'},self.device)
        self.s.update(ident,state='failed',result='private')
        provider=FakeProvider([PushResult('permanent',token,deactivate=True)])
        PushDispatcher(self.s,provider).tick()
        self.assertEqual(self.outbox()[0]['last_error'],'provider_request_failed')
        self.assertNotIn(token,json.dumps(self.s.audits()))
        with self.s.db() as c: self.assertEqual(c.execute('SELECT active FROM push_devices WHERE device=?',(self.device,)).fetchone()[0],0)

    def test_cancelled_preview_notification_is_not_sent(self):
        self.push_device(); ident=self.s.submit({'text':'整理文件','root':str(self.root)},self.device)
        self.s.update(ident,state='awaiting_confirmation'); self.s.act(ident,'cancel')
        provider=FakeProvider(); PushDispatcher(self.s,provider).tick()
        self.assertEqual(provider.calls,[]); self.assertEqual(self.outbox()[0]['state'],'superseded')

    def test_legacy_database_additive_migration_preserves_records(self):
        legacy=self.home/'legacy'; legacy.mkdir(); path=legacy/'state.sqlite3'
        with sqlite3.connect(path) as c:
            c.executescript('CREATE TABLE tasks(id TEXT PRIMARY KEY,body TEXT,state TEXT,result TEXT,plan TEXT,created REAL,due REAL); CREATE TABLE devices(token TEXT PRIMARY KEY,name TEXT);')
            c.execute('INSERT INTO tasks VALUES(?,?,?,?,?,?,?)',('old','{"text":"原任务"}','completed','原结果','{}',1,None))
            c.execute('INSERT INTO devices VALUES(?,?)',('existing-hash','existing-device'))
        upgraded=Store(legacy); self.assertEqual(upgraded.task('old')['result'],'原结果')
        with upgraded.db() as c: self.assertEqual(c.execute('SELECT count(*) FROM devices WHERE token=?',('existing-hash',)).fetchone()[0],1)


class APNsProviderTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory(); self.home=pathlib.Path(self.tmp.name).resolve(); self.key=self.home/'test-key.p8'
        result=subprocess.run(['/usr/bin/openssl','genpkey','-algorithm','EC','-pkeyopt','ec_paramgen_curve:P-256','-out',str(self.key)],capture_output=True)
        self.assertEqual(result.returncode,0); self.key.chmod(0o600)
        self.config=self.home/'push.json'; self.config.write_text(json.dumps({'enabled':True,'key_id':'KEYID12345','team_id':'TEAM123456','private_key_path':str(self.key),'topic':'org.personalosassistant.app'})); self.config.chmod(0o600)
    def tearDown(self): self.tmp.cleanup()
    def test_real_es256_signature_and_cache(self):
        provider=APNsProvider(self.config); config,state=provider._config(); self.assertEqual(state,'configured')
        now=time.time(); jwt=provider._authentication(config,now); self.assertEqual(jwt,provider._authentication(config,now+60))
        header,claims,sig=jwt.split('.'); decode=lambda s:base64.urlsafe_b64decode(s+'='*((4-len(s)%4)%4))
        self.assertEqual(json.loads(decode(header))['alg'],'ES256'); self.assertEqual(json.loads(decode(claims))['iss'],'TEAM123456')
        raw=decode(sig); self.assertEqual(len(raw),64)
        def integer(raw):
            v=raw.lstrip(b'\x00') or b'\x00'
            if v[0]&128:v=b'\x00'+v
            return b'\x02'+bytes([len(v)])+v
        inner=integer(raw[:32])+integer(raw[32:]); signature=self.home/'signature.der'; signature.write_bytes(b'\x30'+bytes([len(inner)])+inner)
        public=self.home/'public.pem'; public.write_bytes(subprocess.run(['/usr/bin/openssl','pkey','-in',str(self.key),'-pubout'],capture_output=True,check=True).stdout)
        result=subprocess.run(['/usr/bin/openssl','dgst','-sha256','-verify',str(public),'-signature',str(signature)],input=(header+'.'+claims).encode(),capture_output=True)
        self.assertEqual(result.returncode,0)
        status=json.dumps(provider.status()); self.assertNotIn('KEYID12345',status); self.assertNotIn(str(self.key),status)

    def test_http2_transport_keeps_secrets_out_of_argv_and_payload_generic(self):
        calls=[]
        def run(args,**kwargs):
            if args[0].endswith('openssl'):return subprocess.run(args,**kwargs)
            calls.append((args,kwargs['input']));return subprocess.CompletedProcess(args,0,b'\n200',b'')
        provider=APNsProvider(self.config,runner=run); token='ab'*32
        result=provider.send(token,'sandbox','task-id','completed','f6adeb22-8694-4a4b-bca1-196824617877',time.time()+60)
        self.assertEqual(result.outcome,'sent'); args,stdin=calls[0]
        self.assertEqual(args,['/usr/bin/curl','--disable','--config','-']); self.assertNotIn(token,' '.join(args))
        self.assertIn(b'http2',stdin); self.assertIn(b'api.sandbox.push.apple.com',stdin)
        self.assertNotIn(self.key.read_bytes(),stdin); self.assertNotIn(str(self.key).encode(),stdin)
        self.assertIn('有一项任务有处理结果'.encode(),stdin)
        self.assertNotIn('有一项任务已完成'.encode(),stdin)

    def test_invalid_config_or_disabled_provider_never_runs_curl(self):
        runner=mock.Mock(side_effect=AssertionError('unexpected subprocess'))
        self.config.chmod(0o644); provider=APNsProvider(self.config,runner=runner)
        self.assertFalse(provider.status()['push_configured']); self.assertEqual(provider.send('ab'*32,'sandbox','id','completed','id',time.time()+1).outcome,'unconfigured'); runner.assert_not_called()
        self.config.chmod(0o600); value=json.loads(self.config.read_text());value['enabled']=False;self.config.write_text(json.dumps(value))
        self.assertEqual(provider.status()['push_status'],'disabled')

    def test_apns_response_mapping_and_no_reflected_token_errors(self):
        for code,reason,outcome,deactivate in [(410,'Unregistered','permanent',True),(429,'TooManyRequests','retry',False),(503,'ServiceUnavailable','retry',False),(400,'ab'*32,'permanent',False)]:
            def run(args,**kwargs):
                if args[0].endswith('openssl'):return subprocess.run(args,**kwargs)
                return subprocess.CompletedProcess(args,0,(json.dumps({'reason':reason})+'\n'+str(code)).encode(),b'')
            result=APNsProvider(self.config,runner=run).send('ab'*32,'sandbox','id','completed','id',time.time()+1)
            self.assertEqual(result.outcome,outcome);self.assertEqual(result.deactivate,deactivate);self.assertNotEqual(result.code,'ab'*32)

if __name__=='__main__': unittest.main(verbosity=2)
