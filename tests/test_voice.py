import base64
import concurrent.futures
import datetime as dt
import hashlib
import http.client
import io
import json
import os
import pathlib
import socket
import sys
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
from unittest import mock

sys.path.insert(0,str(pathlib.Path(__file__).resolve().parents[1]/'server'))
from personal_os import Store, Worker, Handler, ThreadingHTTPServer, validate_calendar, task_kind
from voice import VoiceService, VoiceError, bounded_transcribe, MAX_BASE64_BYTES, MAX_BODY_BYTES, MAX_PENDING


class VoiceTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory(); self.home=pathlib.Path(self.temp.name).resolve()
        self.store=Store(self.home/'state'); self.token='isolated-voice-device'
        self.device=hashlib.sha256(self.token.encode()).hexdigest()
        with self.store.db() as c: c.execute('INSERT INTO devices VALUES(?,?)',(self.device,'test phone'))
        self.service=self.store.voice
        self.service.python.parent.mkdir(parents=True); self.service.python.symlink_to(sys.executable)
        self.service.script.parent.mkdir(); self.service.script.write_text('# isolated fake runner'); self.service.script.chmod(0o600)
        self.service.model.mkdir(parents=True)
        for name in ('config.json','weights.safetensors','personalos-model-manifest.json'): (self.service.model/name).write_text('{}')
        self.service.enable()
        self.server=ThreadingHTTPServer(('127.0.0.1',0),Handler); self.server.store=self.store
        self.http=threading.Thread(target=self.server.serve_forever,daemon=True); self.http.start()
        self.base='http://127.0.0.1:'+str(self.server.server_port); self.workers=[]
        self.audio=b'RIFF'+b'\x00'*4+b'WAVE'+b'private audio fixture'

    def tearDown(self):
        for worker,thread in self.workers: worker.stop.set(); thread.join(3)
        self.server.shutdown(); self.server.server_close(); self.http.join(2); self.temp.cleanup()

    def body(self, request_id='voice-one', audio=None):
        return {'audio_base64':base64.b64encode(self.audio if audio is None else audio).decode(), 'mime_type':'audio/wav','request_id':request_id}

    def api(self,path,body=None,token=None):
        request=urllib.request.Request(self.base+path,None if body is None else json.dumps(body).encode(),{'Authorization':'Bearer '+(self.token if token is None else token),'Content-Type':'application/json'})
        with urllib.request.urlopen(request,timeout=3) as response: return response.status,json.load(response)

    def submit(self,body=None):
        return self.service.submit(self.body() if body is None else body,self.device,validate_calendar)

    def start_worker(self):
        worker=Worker(self.store); thread=threading.Thread(target=worker.run,daemon=True); self.workers.append((worker,thread)); thread.start()

    def wait(self, ident, state, timeout=3):
        end=time.monotonic()+timeout
        while time.monotonic()<end:
            row=self.store.task(ident)
            if row['state']==state: return row
            time.sleep(.02)
        self.fail('voice task did not reach '+state)

    def test_http_submission_fast_persistent_idempotent_and_device_scoped(self):
        self.service.runner=mock.Mock(side_effect=AssertionError('HTTP must not transcribe'))
        started=time.monotonic()
        with concurrent.futures.ThreadPoolExecutor(max_workers=4) as pool:
            responses=list(pool.map(lambda _:self.api('/voice',self.body()),range(4)))
        self.assertLess(time.monotonic()-started,2)
        self.assertEqual({x[0] for x in responses},{201}); ids={x[1]['id'] for x in responses}; self.assertEqual(len(ids),1)
        ident=ids.pop(); self.assertEqual(self.store.task(ident)['state'],'queued')
        raw=self.service._job_directory(ident)/'input.audio'; self.assertEqual(raw.read_bytes(),self.audio)
        self.assertEqual(raw.stat().st_mode&0o777,0o600); self.assertEqual(raw.parent.stat().st_mode&0o777,0o700)
        reopened=Store(self.store.home); self.assertEqual(reopened.voice.submit(self.body(),self.device,validate_calendar),ident)
        other=self.service.submit(self.body(),'other-device',validate_calendar); self.assertNotEqual(ident,other)
        with self.assertRaises(VoiceError) as error: self.submit(self.body(audio=self.audio+b'changed'))
        self.assertEqual(error.exception.status,409)
        self.assertEqual(len(self.store.tasks()),2); self.service.runner.assert_not_called()

    def test_authentication_precedes_large_body_read_and_limit(self):
        for authorized,size,expected in ((False,MAX_BODY_BYTES,401),(True,MAX_BODY_BYTES+1,413)):
            connection=http.client.HTTPConnection('127.0.0.1',self.server.server_port,timeout=1)
            connection.putrequest('POST','/voice'); connection.putheader('Content-Length',str(size))
            if authorized: connection.putheader('Authorization','Bearer '+self.token)
            connection.endheaders() # Deliberately send no body. Rejection must be immediate.
            self.assertEqual(connection.getresponse().status,expected); connection.close()
        self.assertEqual(self.store.tasks(),[])

    def test_invalid_inputs_and_internal_reference_rejected_without_audio_files(self):
        bad=[{'audio_base64':'!!!'}, {'mime_type':'text/plain'}, {'audio_base64':base64.b64encode(b'not wav').decode()},
             {'request_id':'../escape'}, {'audio_base64':'A'*(MAX_BASE64_BYTES+1)}, {'input_path':'/private/secret'},
             {'calendar_context':json.dumps({'date':'not date','events':[]})}]
        for change in bad:
            body=self.body(); body.update(change)
            with self.subTest(change=list(change)):
                with self.assertRaises((ValueError,VoiceError)): self.submit(body)
        with self.assertRaises(ValueError): self.store.submit({'text':'设备状态','_voice_id':'../../outside'})
        self.assertEqual(self.store.tasks(),[]); self.assertFalse(self.service.directory.exists())

    def test_transcribes_once_then_executes_same_task_and_sanitizes_audit(self):
        self.service.runner=mock.Mock(return_value={'text':'Show device status','language':'en'})
        ident=self.submit(); self.start_worker(); row=self.wait(ident,'completed')
        self.assertEqual(json.loads(row['body'])['text'],'Show device status'); self.assertIn('platform',json.loads(row['result']))
        self.service.runner.assert_called_once(); self.assertFalse(self.service._job_directory(ident).exists())
        audit=json.dumps(self.store.audits(ident)); self.assertNotIn('Show device status',audit); self.assertNotIn('private audio',audit)
        self.assertNotIn(self.body()['audio_base64'],audit)
        self.assertEqual(self.submit(),ident); self.assertEqual(self.service.runner.call_count,1)
        argv=self.service.runner.call_args.args[0]
        self.assertEqual(argv[:2],[str(self.service.python),str(self.service.script)])
        self.assertEqual(argv[-2:],['--model',str(self.service.model)])
        self.assertEqual(self.service.runner.call_args.kwargs['timeout'],150)

    def test_failed_transcription_never_calls_tools_and_cleans_decoded_files(self):
        def failed(argv,directory,timeout):
            (directory/'personalos-asr-test').mkdir(); (directory/'personalos-asr-test'/'speech.wav').write_bytes(b'private decoded audio')
            raise RuntimeError('unsafe secret diagnostic')
        self.service.runner=failed; ident=self.submit()
        with mock.patch.object(self.store,'plan_files',side_effect=AssertionError('must not execute')), \
             mock.patch('personal_os.urllib.request.urlopen',side_effect=AssertionError('must not plan')):
            self.start_worker(); row=self.wait(ident,'failed')
        # The failed state is committed before the worker's finally cleanup.
        # Wait for that asynchronous deletion, but retain a real bounded check.
        deadline=time.monotonic()+2
        while self.service._job_directory(ident).exists() and time.monotonic()<deadline:
            time.sleep(.01)
        self.assertFalse(self.service._job_directory(ident).exists())
        self.assertNotIn('unsafe secret',row['result']); self.assertNotIn('unsafe secret',json.dumps(self.store.audits()))

    def test_reminders_independent_and_transcribed_reminder_is_scheduled(self):
        started=threading.Event(); release=threading.Event()
        def blocked(*args,**kwargs):
            started.set(); release.wait(4); return {'text':'明天上午九点提醒继续项目','language':'zh'}
        self.service.runner=blocked; voice=self.submit(); self.start_worker(); self.assertTrue(started.wait(2))
        due=dt.datetime.now(dt.timezone.utc)+dt.timedelta(seconds=.7)
        reminder=self.store.submit({'text':'提醒继续','due':due.isoformat()})
        try:
            self.wait(reminder,'completed',2); self.assertEqual(self.store.task(voice)['state'],'running')
        finally: release.set()
        result=self.wait(voice,'scheduled'); self.assertGreater(result['due'],time.time())
        self.assertFalse(self.service._job_directory(voice).exists())

    def test_cancel_and_restart_cleanup_do_not_replay_interrupted_or_completed(self):
        cancelled=self.submit(self.body('cancelled')); self.store.act(cancelled,'cancel')
        self.assertFalse(self.service._job_directory(cancelled).exists())
        queued=self.submit(self.body('queued')); interrupted=self.submit(self.body('interrupted'))
        with self.store.db() as c:
            c.execute("UPDATE voice_jobs SET state='transcribing' WHERE task=?",(interrupted,))
            c.execute("UPDATE tasks SET state='running' WHERE id=?",(interrupted,))
        orphan=self.service.directory/'orphan'; orphan.mkdir(); (orphan/'leftover.wav').write_bytes(b'private')
        Store(self.store.home).voice.recover()
        self.assertEqual(self.store.task(interrupted)['state'],'failed'); self.assertFalse(self.service._job_directory(interrupted).exists())
        self.assertEqual(self.store.task(queued)['state'],'queued'); self.assertTrue(self.service._job_directory(queued).exists())
        self.assertFalse(orphan.exists()); self.assertEqual(self.store.task(cancelled)['state'],'cancelled')
        self.service.runner=mock.Mock(return_value={'text':'设备状态','language':'zh'}); self.start_worker(); self.wait(queued,'completed')
        self.service.recover(); self.assertEqual(self.service.runner.call_count,1)

    def test_config_is_fixed_local_private_and_disabled_rejects_new_jobs(self):
        self.assertEqual(self.service.config_path,self.store.home/'voice.json')
        self.assertTrue(self.api('/status')[1]['voice_configured'])
        self.service.config_path.chmod(0o644); self.assertFalse(self.service.status()['voice_configured'])
        self.service.config_path.chmod(0o600)
        value=json.loads(self.service.config_path.read_text()); value['python_path']='/bin/sh'
        self.service.config_path.write_text(json.dumps(value)); self.assertFalse(self.service.status()['voice_configured'])
        self.service.enable(False)
        with self.assertRaises(VoiceError) as error: self.submit()
        self.assertEqual(error.exception.status,503); self.assertEqual(self.store.tasks(),[])

    def test_queue_budget_and_cleanup_error_cannot_replay_successful_transcript(self):
        for index in range(MAX_PENDING): self.submit(self.body('bounded-'+str(index)))
        with self.assertRaises(VoiceError) as error: self.submit(self.body('over-budget'))
        self.assertEqual(error.exception.status,429); self.assertEqual(len(list(self.service.directory.iterdir())),MAX_PENDING)
        ident=self.store.tasks()[0]['id']; self.store.update(ident,state='running')
        self.service.runner=mock.Mock(return_value={'text':'设备状态','language':'zh'})
        with mock.patch.object(self.service,'cleanup',side_effect=OSError('private filesystem diagnostic')):
            Worker(self.store).execute(self.store.task(ident))
        self.assertEqual(self.store.task(ident)['state'],'queued')
        self.assertNotIn('_voice_id',json.loads(self.store.task(ident)['body']))
        self.assertNotIn('private filesystem diagnostic',json.dumps(self.store.audits(ident)))
        Worker(self.store).execute(self.store.task(ident)); self.assertEqual(self.store.task(ident)['state'],'completed')
        self.assertEqual(self.service.runner.call_count,1)
        self.service.recover(); self.assertFalse(self.service._job_directory(ident).exists())

    def test_subprocess_output_errors_timeout_and_fixed_offline_environment(self):
        script=self.home/'test_runner.py'; directory=self.home/'job'; directory.mkdir()
        script.write_text('import os,json; assert os.environ["HF_HUB_OFFLINE"]=="1"; print(json.dumps({"text":"设备状态","language":"zh"}))')
        self.assertEqual(bounded_transcribe([sys.executable,str(script)],directory,timeout=2)['text'],'设备状态')
        for source,expected in [('import time; time.sleep(5)','voice_timeout'), ('print("x"*70000)','voice_failed'),
                                ('import sys,json; print(json.dumps({"code":"audio_too_long","error":"secret diagnostic"}),file=sys.stderr); sys.exit(2)','audio_too_long'),
                                ('print("{\\"text\\":\\"\\",\\"language\\":\\"zh\\"}")','voice_failed')]:
            script.write_text(source)
            with self.assertRaises(VoiceError) as error: bounded_transcribe([sys.executable,str(script)],directory,timeout=.3)
            self.assertEqual(error.exception.code,expected); self.assertNotIn('secret diagnostic',str(error.exception))

    def test_calendar_legacy_timezone_date_only_invalid_and_idempotency(self):
        calendar={'date':'2026-10-23T16:00:00Z','timeZone':'Asia/Hong_Kong','truncated':False,'events':[]}
        for index,date in enumerate(('2026-10-23T16:00:00Z','2026-10-24T00:00:00+08:00','2026-10-24','2026-10-23T16:00:00.000Z')):
            calendar['date']=date; self.assertEqual(validate_calendar(json.dumps(calendar)),calendar)
            body=self.body('calendar-'+str(index)); body['calendar_context']=json.dumps(calendar)
            ident=self.submit(body); self.assertEqual(self.submit(body),ident)
            stored=json.loads(json.loads(self.store.task(ident)['body'])['calendar_context']); self.assertEqual(stored,calendar)
            task={'text':'设备状态','calendar_context':json.dumps(calendar),'request_id':'legacy-task-'+str(index)}
            task_id=self.api('/tasks',task)[1]['id']; self.assertEqual(self.api('/tasks',task)[1]['id'],task_id)
            self.assertEqual(json.loads(json.loads(self.store.task(task_id)['body'])['calendar_context']),calendar)
        for date in ('2026-02-30','20261024','2026-W43-6','2026-10-24T00:00:00','2026-13-01T00:00:00Z','2026-10-24T25:00:00Z','not-a-date',''):
            calendar['date']=date
            with self.assertRaisesRegex(ValueError,'日历日期无效'): validate_calendar(json.dumps(calendar))

    def test_english_status_aliases_are_narrow_and_preserve_routes(self):
        for text in ('Show device status','Check my computer status.','System status','check status','设备状态'):
            self.assertEqual(task_kind(text),'status')
        for text in ('what is the status quo','statustext','check secret files'):
            self.assertEqual(task_kind(text),'plan')
        self.assertEqual(task_kind('整理文件和设备状态'),'organize')
        self.assertEqual(task_kind('提醒检查状态'),'plan')


if __name__=='__main__': unittest.main(verbosity=2)
