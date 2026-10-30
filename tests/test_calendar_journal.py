import base64
import datetime as dt
import hashlib
import json
import pathlib
import sqlite3
import sys
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
from unittest import mock

sys.path.insert(0,str(pathlib.Path(__file__).resolve().parents[1]/'server'))
from calendar_journal import CalendarError, DEFAULT_ZONE, parse_entry, resolved_fields, validate_zone
from personal_os import Store, Worker, Handler, ThreadingHTTPServer, task_kind, validate_calendar

NOW=dt.datetime(2026,10,25,17,tzinfo=validate_zone(DEFAULT_ZONE)).timestamp()


class CalendarParserTests(unittest.TestCase):
    def test_explicit_chinese_english_ranges_and_task_created_anchor(self):
        cases=[('我今天从14:00到15:00写了代码','done','2026-10-25T14:00:00+08:00','2026-10-25T15:00:00+08:00'),
               ('I finished PROJECT today from 14:00 to 15:00','done','2026-10-25T14:00:00+08:00','2026-10-25T15:00:00+08:00'),
               ('下周三下午三点到四点开会','plan','2026-10-28T15:00:00+08:00','2026-10-28T16:00:00+08:00'),
               ('Schedule PROJECT tomorrow from 10 to 11','plan','2026-10-26T10:00:00+08:00','2026-10-26T11:00:00+08:00'),
               ('明天我要完成PROJECT界面 15:00到16:00','plan','2026-10-26T15:00:00+08:00','2026-10-26T16:00:00+08:00'),
               ('安排后天下午三点半到四点一刻整理文件','plan','2026-10-27T15:30:00+08:00','2026-10-27T16:15:00+08:00'),
               ('10月28日15:00到16:00开会','plan','2026-10-28T15:00:00+08:00','2026-10-28T16:00:00+08:00'),
               ('Schedule review on 2026-10-29 from 3 to 4 pm','plan','2026-10-29T15:00:00+08:00','2026-10-29T16:00:00+08:00')]
        for text,kind,start,end in cases:
            with self.subTest(text=text),mock.patch('calendar_journal.time.time',return_value=NOW+9*86400):
                entry=parse_entry(text,NOW)
                self.assertEqual((entry['status'],entry['kind'],entry['start'],entry['end']),('ready',kind,start,end))
                self.assertEqual(entry['original_text'],text); self.assertTrue(entry['title'])
                self.assertEqual(task_kind(text),'calendar')

    def test_missing_dates_endpoints_vague_multi_items_need_details(self):
        for text in ('我今天写了代码','刚刚完成了PROJECT界面','明天我要去健身','安排开会','安排明天10点开会',
                     '明天上午9点开会，下午3点健身','安排明天10点到11点开会然后健身','安排明天全天健身和写代码',
                     '安排明天10点左右到11点开会','安排明天10点到11点开会，后天再去健身','安排明天10点到11点开会，健身',
                     'Schedule review tomorrow at 10','Schedule review tomorrow around 10 to 11',
                     'Schedule review tomorrow from 10 to 11 and gym from 12 to 13'):
            with self.subTest(text=text):
                entry=parse_entry(text,NOW); self.assertEqual(entry['status'],'needs_details'); self.assertTrue(entry['question'])

    def test_negations_queries_hypothetical_never_create_or_route_writes(self):
        for text in ('今天应该先做什么','我做了什么','我没有完成PROJECT界面','不要安排明天10点到11点整理文件',
                     '如果我明天10点到11点整理文件','明天10点到11点安排什么？','I did not finish PROJECT today from 10 to 11',
                     'If I schedule cleanup tomorrow from 10 to 11','I would schedule a meeting tomorrow from 10 to 11',
                     '我明天不会安排10点到11点整理PROJECT','我明天不去健身10:00到11:00',"I won't go to the gym tomorrow from 10 to 11"):
            with self.subTest(text=text): self.assertEqual(task_kind(text),'plan')
        self.assertEqual(task_kind('明天提醒继续项目'),'plan')
        self.assertEqual(task_kind('安排明天提醒继续项目'),'plan')
        self.assertEqual(task_kind('查找 PROJECT 最新资料'),'search')
        self.assertEqual(task_kind('整理选定文件'),'organize')

    def test_invalid_date_order_clock_boundaries_and_future_done(self):
        for text in ('安排2026-02-30 10:00到11:00开会','安排明天16:00到15:00开会','安排明天100:00到11:00开会',
                     '安排明天10:99到11:00开会','Schedule tomorrow from 13pm to 14pm',
                     'Schedule tomorrow from 0am to 1am','我今天18:00到19:00完成了PROJECT界面',
                     '安排今天10:00到11:00开会','安排明天晚上23点到1点工作','安排明天晚上十二点到一点工作','安排明天上午13点到14点工作'):
            with self.subTest(text=text): self.assertEqual(parse_entry(text,NOW)['status'],'needs_details')

    def test_all_day_requires_explicit_words_and_midnight_exclusive_end(self):
        entry=parse_entry('安排明天全天整理PROJECT文档',NOW)
        self.assertEqual(entry['status'],'ready'); self.assertTrue(entry['all_day'])
        self.assertEqual((entry['start'],entry['end']),('2026-10-26T00:00:00+08:00','2026-10-27T00:00:00+08:00'))
        self.assertEqual(parse_entry('安排明天整理PROJECT文档',NOW)['status'],'needs_details')
        self.assertEqual(parse_entry('安排明天全天10:00到11:00整理文档',NOW)['status'],'needs_details')

    def test_timezone_dst_gap_fold_and_offset_mismatch(self):
        before=dt.datetime(2026,1,1,tzinfo=dt.timezone.utc).timestamp()
        for text in ('Schedule on 2026-03-08 from 02:30 to 03:30','Schedule on 2026-11-01 from 01:15 to 01:45'):
            self.assertEqual(parse_entry(text,before,'America/Los_Angeles')['status'],'needs_details')
        text='Schedule tomorrow from 10 to 11'
        near_midnight=dt.datetime(2026,10,25,0,30,tzinfo=dt.timezone.utc).timestamp()
        self.assertEqual(parse_entry(text,near_midnight,'America/Los_Angeles')['start'],'2026-10-25T10:00:00-07:00')
        self.assertEqual(parse_entry(text,near_midnight,'Asia/Tokyo')['start'],'2026-10-26T10:00:00+09:00')
        with self.assertRaises(CalendarError): parse_entry(text,NOW,'../../UTC')
        body={'title':'meeting','kind':'plan','start':'2026-03-08T02:30:00-08:00','end':'2026-03-08T03:30:00-07:00','time_zone':'America/Los_Angeles','all_day':'false','revision':'1'}
        with self.assertRaises(CalendarError): resolved_fields(body)

        body.update(start='2026-11-01T01:15:00-07:00',end='2026-11-01T01:45:00-07:00')
        self.assertEqual(resolved_fields(body)['start'],body['start']) # explicit offset selects a fold
        body.update(start='2026-10-26T10:00:00+00:00',end='2026-10-26T11:00:00+00:00',time_zone='Asia/Hong_Kong')
        with self.assertRaises(CalendarError): resolved_fields(body)

    def test_concise_titles_remove_confirmed_times_without_guessing_project_names(self):
        cases=[('昨天上午十点到十一点我完成了日历验收','日历验收'),
               ('我昨天上午十点到十一点完成了日历验收','日历验收'),
               ('明天下午三点到四点安排PROJECT界面设计','PROJECT界面设计'),
               ('请帮我安排明天15:00到16:00继续PROJECT项目','继续PROJECT项目'),
               ('I finished the calendar test yesterday from 10 to 11','the calendar test'),
               ('Yesterday from 10 to 11 I finished the calendar test','the calendar test'),
               ('Schedule PROJECT interface review tomorrow from 10 to 11','PROJECT interface review'),
               ('Please schedule tomorrow from 10 to 11 PROJECT review','PROJECT review'),
               ('安排明天15:00到16:00阅读《明天》','阅读《明天》'),
               ('安排明天15:00到16:00讨论PROJECT明天计划','讨论PROJECT明天计划'),
               ('安排明天15:00到16:00明天计划','明天计划'),
               ('安排明天15:00到16:00Finished Business项目','Finished Business项目'),
               ('安排明天15:00到16:00Schedule Manager项目','Schedule Manager项目'),
               ('安排明天15:00到16:00安排大师项目','安排大师项目'),
               ('安排明天全天PROJECT设计','PROJECT设计')]
        for text,title in cases:
            with self.subTest(text=text):
                entry=parse_entry(text,NOW); self.assertEqual(entry['status'],'ready')
                self.assertEqual(entry['title'],title); self.assertEqual(entry['original_text'],text)
        empty='安排明天15:00到16:00'; self.assertEqual(parse_entry(empty,NOW)['title'],empty)

    def test_numeric_ranges_inherit_explicit_daypart_and_reject_conflicts(self):
        for text,start,end,title in (
            ('安排明天下午3:00-4:00开会','15:00:00','16:00:00','开会'),
            ('安排明天上午9:00-10:00开会','09:00:00','10:00:00','开会'),
            ('Schedule review tomorrow afternoon from 3:00 to 4:00','15:00:00','16:00:00','review'),
            ('Schedule review tomorrow in the morning from 9:00 to 10:00','09:00:00','10:00:00','review'),
            ('安排明天下午3:00pm-4:00pm开会','15:00:00','16:00:00','开会')):
            with self.subTest(text=text):
                entry=parse_entry(text,NOW); self.assertEqual(entry['status'],'ready')
                self.assertEqual(entry['start'],'2026-10-26T'+start+'+08:00')
                self.assertEqual(entry['end'],'2026-10-26T'+end+'+08:00'); self.assertEqual(entry['title'],title)
        for text in ('安排明天晚上11:00-1:00工作','安排明天下午3:00am-4:00am开会',
                     '安排明天上午9:00pm-10:00pm开会','Schedule review tomorrow afternoon from 3am to 4am'):
            with self.subTest(text=text): self.assertEqual(parse_entry(text,NOW)['status'],'needs_details')

    def test_time_clause_commas_allow_one_activity_but_never_merge_two(self):
        for text,title,kind in (
            ('今天10:00到11:00，我完成了日历验收','日历验收','done'),
            ('明天15:00到16:00，安排继续PROJECT项目','继续PROJECT项目','plan'),
            ('我完成了日历验收，今天10:00到11:00','日历验收','done'),
            ('今天，10:00到11:00，我完成了日历验收','日历验收','done'),
            ('Tomorrow from 15:00 to 16:00, schedule PROJECT review','PROJECT review','plan'),
            ('I finished the calendar test, today from 10 to 11','the calendar test','done'),
            ('安排明天15:00到16:00阅读《Hello, world》','阅读《Hello, world》','plan')):
            with self.subTest(text=text):
                entry=parse_entry(text,NOW); self.assertEqual((entry['status'],entry['kind'],entry['title']),('ready',kind,title))
                self.assertEqual(entry['original_text'],text)
        for text in ('明天上午9点开会，下午3点健身','安排明天10:00到11:00开会，健身',
                     '今天10:00到11:00，我完成了日历验收，修复了按钮','安排明天全天开会，健身',
                     'Schedule review tomorrow from 10 to 11, go to the gym'):
            with self.subTest(text=text): self.assertEqual(parse_entry(text,NOW)['status'],'needs_details')

    def test_english_postfix_daypart_is_part_of_one_range_and_short_title(self):
        for text,start,end,title,kind in (
            ('Today from 10 to 11 in the morning, I completed the calendar test.','2026-10-25T10:00:00+08:00','2026-10-25T11:00:00+08:00','the calendar test','done'),
            ('Tomorrow from 3 to 4 in the afternoon, schedule a project review.','2026-10-26T15:00:00+08:00','2026-10-26T16:00:00+08:00','a project review','plan'),
            ('Schedule PROJECT review tomorrow from 3 pm to 4 pm in the afternoon.','2026-10-26T15:00:00+08:00','2026-10-26T16:00:00+08:00','PROJECT review','plan')):
            with self.subTest(text=text):
                entry=parse_entry(text,NOW)
                self.assertEqual((entry['status'],entry['kind'],entry['start'],entry['end'],entry['title']),('ready',kind,start,end,title))
                self.assertEqual(entry['original_text'],text)
        for text in ('Schedule review tomorrow afternoon from 3 to 4 in the morning.',
                     'Tomorrow from 3 am to 4 am in the afternoon, schedule review.',
                     'Tomorrow from 11 to 1 in the evening, schedule review.',
                     'Tomorrow from 3 to 4 in the afternoon, schedule review, go to the gym.'):
            with self.subTest(text=text): self.assertEqual(parse_entry(text,NOW)['status'],'needs_details')


class CalendarHTTPTests(unittest.TestCase):
    def setUp(self):
        self.temporary=tempfile.TemporaryDirectory(); self.home=pathlib.Path(self.temporary.name).resolve(); self.store=Store(self.home/'state')
        self.tokens=['calendar-phone-one','calendar-phone-two']; self.owners=[hashlib.sha256(x.encode()).hexdigest() for x in self.tokens]
        with self.store.db() as c:
            for owner in self.owners: c.execute('INSERT INTO devices VALUES(?,?)',(owner,'fixture'))
        self.server=ThreadingHTTPServer(('127.0.0.1',0),Handler); self.server.store=self.store
        self.thread=threading.Thread(target=self.server.serve_forever,daemon=True); self.thread.start(); self.base='http://127.0.0.1:'+str(self.server.server_port)
    def tearDown(self): self.server.shutdown(); self.server.server_close(); self.thread.join(2); self.temporary.cleanup()
    def api(self,path,body=None,device=0):
        request=urllib.request.Request(self.base+path,None if body is None else json.dumps(body).encode(),{'Authorization':'Bearer '+self.tokens[device]})
        with urllib.request.urlopen(request,timeout=3) as response: return json.load(response)
    def create(self,text='我今天写了代码',owner=0,request_id=None):
        body={'text':text,'time_zone':DEFAULT_ZONE}
        if request_id: body['request_id']=request_id
        ident=self.api('/tasks',body,owner)['id']
        with self.store.db() as c: c.execute('UPDATE tasks SET created=? WHERE id=?',(NOW,ident))
        Worker(self.store).execute(self.store.task(ident)); return ident
    def resolve_body(self,revision='1'):
        return {'title':'PROJECT界面','kind':'done','start':'2026-10-25T14:00:00+08:00','end':'2026-10-25T15:00:00+08:00','time_zone':DEFAULT_ZONE,'all_day':'false','revision':revision}
    def error(self,code,call):
        with self.assertRaises(urllib.error.HTTPError) as error: call()
        self.assertEqual(error.exception.code,code)

    def test_owned_without_request_id_no_false_phone_write_and_ack_idempotency(self):
        ident=self.create(); row=self.api('/tasks/'+ident)
        self.assertNotIn('owner',row); self.assertEqual(row['state'],'needs_details')
        entry=self.api('/calendar/entries')['entries'][0]; self.assertEqual(entry['id'],ident); self.assertEqual(entry['device_sync'],'pending')
        resolved=self.api('/calendar/entries/'+ident+'/resolve',self.resolve_body())
        self.assertEqual(resolved['revision'],2); self.assertEqual(resolved['status'],'ready')
        message=json.loads(self.api('/tasks/'+ident)['result'])['message']; self.assertIn('待写入',message); self.assertNotIn('已写入',message)
        ack={'revision':'2','status':'written'}; first=self.api('/calendar/entries/'+ident+'/ack',ack); second=self.api('/calendar/entries/'+ident+'/ack',ack)
        self.assertEqual(first,second); self.assertEqual(first['device_sync'],'written')
        self.assertIn('已写入',json.loads(self.api('/tasks/'+ident)['result'])['message'])
        self.assertEqual(sum(x['event']=='calendar_sync_ack' for x in self.store.audits(ident)),1)

    def test_resolve_revision_retry_cancel_ack_state_owner_enforced(self):
        ident=self.create(); endpoint='/calendar/entries/'+ident
        for action,body in [('resolve',self.resolve_body()),('cancel',{'revision':'1'}),('ack',{'revision':'1','status':'written'})]:
            self.error(404,lambda a=action,b=body:self.api(endpoint+'/'+a,b,1))
        self.assertEqual(self.api('/calendar/entries',device=1)['entries'],[])
        self.error(404,lambda:self.api('/tasks/'+ident,device=1)); self.assertNotIn(ident,[x['id'] for x in self.api('/tasks',device=1)])
        self.error(409,lambda:self.api(endpoint+'/ack',{'revision':'1','status':'written'}))
        self.error(400,lambda:self.api(endpoint+'/cancel',{}))
        first=self.api(endpoint+'/resolve',self.resolve_body()); self.assertEqual(first,self.api(endpoint+'/resolve',self.resolve_body()))
        changed=self.resolve_body(); changed['title']='changed'; self.error(409,lambda:self.api(endpoint+'/resolve',changed))
        self.error(409,lambda:self.api(endpoint+'/ack',{'revision':'1','status':'written'}))
        self.error(409,lambda:self.api(endpoint+'/cancel',{'revision':'1'}))
        cancelled=self.api(endpoint+'/cancel',{'revision':'2'}); self.assertEqual(cancelled['revision'],3)
        self.assertEqual(cancelled,self.api(endpoint+'/cancel',{'revision':'2'}))
        self.error(409,lambda:self.api(endpoint+'/ack',{'revision':'3','status':'written'}))
        removed=self.api(endpoint+'/ack',{'revision':'3','status':'removed'}); self.assertEqual(removed['device_sync'],'removed')
        self.assertEqual(self.store.task(ident)['state'],'cancelled')

    def test_sync_error_audit_redaction_and_success_not_downgraded_by_late_error(self):
        ident=self.create('安排明天10:00到11:00开会'); path='/calendar/entries/'+ident+'/ack'
        self.api(path,{'revision':'1','status':'error','message':'calendar fixture diagnostic'})
        self.assertEqual(self.api('/calendar/entries')['entries'][0]['device_sync'],'error')
        self.assertNotIn('calendar fixture diagnostic',json.dumps(self.store.audits(ident)))
        self.api(path,{'revision':'1','status':'written'})
        self.assertEqual(self.api(path,{'revision':'1','status':'error','message':'late retry'})['device_sync'],'written')
        self.error(400,lambda:self.api(path,{'revision':'1','status':'written','event_id':'must not store'}))

    def test_pagination_keeps_old_pending_visible_and_rejects_bad_cursors(self):
        identifiers=[]
        for index in range(107):
            ident=self.store.submit({'text':'安排明天10:00到11:00开会'},self.owners[0]); identifiers.append(ident)
            with self.store.db() as c: c.execute('UPDATE tasks SET created=? WHERE id=?',(NOW+index,ident))
            self.store.calendar.create(self.store.task(ident))
        for ident in identifiers[:3]: self.store.calendar.ack(ident,self.owners[0],{'revision':'1','status':'written'})
        first=self.api('/calendar/entries?limit=100'); self.assertEqual(len(first['entries']),100)
        self.assertEqual(first['entries'][0]['id'],identifiers[3]); self.assertTrue(first['next_cursor'])
        second=self.api('/calendar/entries?cursor='+first['next_cursor']); self.assertEqual(len(second['entries']),7)
        self.assertEqual({x['id'] for x in first['entries']+second['entries']},set(identifiers))
        for cursor in ('%%%',base64.urlsafe_b64encode(b'[0,NaN,"00000000-0000-0000-0000-000000000000"]').decode()):
            self.error(400,lambda c=cursor:self.api('/calendar/entries?cursor='+c))

    def test_task_idempotency_private_history_and_calendar_no_tool_execution(self):
        body={'text':'安排明天10:00到11:00整理PROJECT文件','request_id':'record-once','time_zone':DEFAULT_ZONE}
        ident=self.api('/tasks',body)['id']; self.assertEqual(self.api('/tasks',body)['id'],ident)
        with self.store.db() as c: c.execute('UPDATE tasks SET created=? WHERE id=?',(NOW,ident))
        with mock.patch.object(self.store,'plan_files',side_effect=AssertionError('journal must not move')),mock.patch.object(self.store,'search_files',side_effect=AssertionError('journal must not search')):
            Worker(self.store).execute(self.store.task(ident)); Worker(self.store).execute(self.store.task(ident))
        self.assertEqual(len(self.api('/calendar/entries')['entries']),1)
        shared=self.api('/tasks',{'text':'设备状态'})['id']
        with self.assertRaises(urllib.error.HTTPError) as error:self.api('/tasks/'+shared,device=1)
        self.assertEqual(error.exception.code,404)
        self.assertNotIn(shared,[row['id'] for row in self.api('/tasks',device=1)])
        self.assertNotIn(self.owners[0],json.dumps(self.api('/tasks')))
        with self.store.db() as c: c.execute('DELETE FROM devices WHERE token=?',(self.owners[0],))
        self.assertEqual(self.api('/calendar/entries',device=1)['entries'],[]) # no ownership transfer

    def test_voice_keeps_owner_timezone_without_calendar_sharing(self):
        audio=b'RIFF'+b'\x00'*4+b'WAVEfixture'
        payload={'audio_base64':base64.b64encode(audio).decode(),'mime_type':'audio/wav','request_id':'calendar-voice','time_zone':'Asia/Tokyo'}
        with mock.patch.object(self.store.voice,'_ready',return_value=True):
            ident=self.store.voice.submit(payload,self.owners[0],validate_calendar)
            self.assertEqual(self.store.voice.submit(payload,self.owners[0],validate_calendar),ident)
            with self.store.db() as c: c.execute("UPDATE tasks SET state='running',created=? WHERE id=?",(NOW,ident))
            self.store.voice.runner=mock.Mock(return_value={'text':'安排明天10:00到11:00开会','language':'zh'})
            self.store.voice.transcribe(self.store.task(ident)); Worker(self.store).execute(self.store.task(ident))
        entry=self.api('/calendar/entries')['entries'][0]
        self.assertEqual(entry['time_zone'],'Asia/Tokyo'); self.assertTrue(entry['start'].endswith('+09:00'))
        self.assertEqual(self.store.task(ident)['owner'],self.owners[0]); self.assertNotIn('calendar_context',json.loads(self.store.task(ident)['body']))
        self.error(400,lambda:self.api('/tasks',{'text':'设备状态','time_zone':'NoSuch/Zone'}))
        invalid=dict(payload,time_zone='NoSuch/Zone',request_id='invalid-zone')
        self.error(400,lambda:self.api('/voice',invalid))


class CalendarMigrationTests(unittest.TestCase):
    def test_old_schema_additive_owner_backfill_no_guess_or_reset(self):
        with tempfile.TemporaryDirectory() as temporary:
            home=pathlib.Path(temporary); database=sqlite3.connect(home/'state.sqlite3')
            database.executescript('CREATE TABLE tasks(id TEXT PRIMARY KEY,body TEXT,state TEXT,result TEXT,plan TEXT,created REAL,due REAL); CREATE TABLE task_requests(device TEXT,request_id TEXT,task TEXT,body_hash TEXT,PRIMARY KEY(device,request_id));')
            for name in ('owned','unowned','ambiguous'): database.execute('INSERT INTO tasks VALUES(?,?,?,?,?,?,?)',(name,'{"text":"old"}','completed','preserved','{}',1,None))
            database.execute('INSERT INTO task_requests VALUES(?,?,?,?)',('phone','retry','owned','preserved-hash'))
            database.execute('INSERT INTO task_requests VALUES(?,?,?,?)',('a','a','ambiguous','same'))
            database.execute('INSERT INTO task_requests VALUES(?,?,?,?)',('b','b','ambiguous','same'))
            database.commit(); database.close()
            store=Store(home); Store(home)
            self.assertEqual(store.task('owned')['owner'],'phone'); self.assertEqual(store.task('unowned')['owner'],''); self.assertEqual(store.task('ambiguous')['owner'],'')
            self.assertEqual(store.task('owned')['result'],'preserved'); self.assertEqual(store.calendar.entries('phone')['entries'],[])
            with store.db() as c: self.assertEqual(c.execute("SELECT body_hash FROM task_requests WHERE task='owned'").fetchone()[0],'preserved-hash')


if __name__=='__main__': unittest.main(verbosity=2)
