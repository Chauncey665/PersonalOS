import datetime as dt,io,json,pathlib,sys,tempfile,time,unittest,uuid
from unittest.mock import patch
sys.path.insert(0,str(pathlib.Path(__file__).resolve().parents[1]/'server'))
from personal_os import Store,Worker
from live_info import LiveInfo
from task_outcomes import describe,public_task
from task_editing import TaskEditing
from todos import TodoError
ZONE=dt.timezone(dt.timedelta(hours=8))
class RealtimeTests(unittest.TestCase):
 def setUp(self):
  self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup);self.s=Store(pathlib.Path(self.tmp.name));self.o='test-owner';self.calls=[]
  with self.s.db() as c:c.executemany('INSERT INTO devices VALUES(?,?)',[(self.o,'A'),('other','B')])
  (self.s.home/'dialogue-engine.json').write_text('{"enabled":true}')
  self.sid=self.s.sessions.create(self.o)['id'];self.s.brain.transport=lambda *a,**k:self.fail('A live fact must not be inferred by LLM')
  self.s.live.location(self.o,{'latitude':22.3,'longitude':114.3,'time_zone':'Asia/Hong_Kong','captured_at':time.time()})
  self.temperature=26;self.age=0;self.partial=False;self.s.live.transport=self.network
 def network(self,request,**kwargs):
  self.calls.append(request.full_url);now=dt.datetime.fromtimestamp(time.time(),ZONE);labels=[]
  for d in range(2):
   for h in range(24):labels.append((now.replace(hour=0,minute=0,second=0,microsecond=0)+dt.timedelta(days=d,hours=h)).strftime('%Y-%m-%dT%H:%M'))
  if self.partial:labels=labels[:now.hour+1]
  return io.BytesIO(json.dumps({'timezone':'Asia/Hong_Kong','current':{'time':(now-dt.timedelta(seconds=self.age)).strftime('%Y-%m-%dT%H:%M'),'temperature_2m':self.temperature,'precipitation':0},'hourly':{'time':labels,'temperature_2m':[self.temperature]*len(labels),'precipitation_probability':[10]*len(labels),'precipitation':[0]*len(labels)}}).encode())
 def turn(self,text,sid=None):
  sid=sid or self.sid;st=self.s.sessions.state(self.o,sid)
  ident=self.s.submit({'text':text,'time_zone':'Asia/Hong_Kong','session_id':sid,'reply_to':st['task_id'],'request_id':uuid.uuid4().hex},self.o)
  Worker(self.s).execute(self.s.task(ident));return self.s.task(ident),self.s.sessions.state(self.o,sid)
 def test_temperature_followup_fetches_again_instead_of_using_old_answer(self):
  self.turn('今天天气怎么样');self.temperature=31
  row,st=self.turn('那不，现在多少度？')
  self.assertEqual(len(self.calls),2);self.assertIn('31',st['speech']);self.assertNotIn('26',st['speech']);self.assertEqual(describe(self.s,row)['status'],'answered')
 def test_tomorrow_followup_uses_new_source_and_correct_day(self):
  self.turn('今天会下雨吗');row,st=self.turn('那明天呢')
  p=json.loads(row['plan']);self.assertEqual(len(self.calls),2);self.assertEqual(p['query_day'],(dt.datetime.now(ZONE).date()+dt.timedelta(days=1)).isoformat());self.assertIn(p['query_day'],row['result'])
 def test_temperature_topic_is_preserved_for_tomorrow_followup(self):
  self.turn('现在多少度');self.temperature=33
  row,st=self.turn('那明天呢');self.assertIn('33',st['speech']);self.assertIn('气温',st['speech']);self.assertNotIn('降雨概率',st['speech'])
  self.assertIn((dt.datetime.now(ZONE).date()+dt.timedelta(days=1)).isoformat(),st['speech'])
 def test_future_weather_without_question_mark_never_creates_calendar_event(self):
  row,st=self.turn('明天天气');self.assertEqual(json.loads(row['plan'])['live_query'],'weather')
  self.assertEqual(self.s.calendar.entries(self.o)['entries'],[])
 def test_network_error_never_reuses_previous_values_and_does_not_prompt_endless_recording(self):
  self.turn('现在多少度');self.s.live.transport=lambda *a,**k:(_ for _ in ()).throw(OSError('offline'))
  row,st=self.turn('现在多少度')
  self.assertNotIn('26',st['speech']);self.assertIn('无法核对',st['speech']);self.assertFalse(st['can_continue']);self.assertEqual(describe(self.s,row)['status'],'query_failed')
 def test_stale_future_missing_and_invalid_weather_rejected(self):
  for age,temp in [(7200,26),(-900,26),(0,None),(0,999),(0,float('nan'))]:
   self.age=age;self.temperature=temp
   with self.subTest(age=age,temp=temp),self.assertRaises(TodoError):self.s.live.weather(self.o)
 def test_incomplete_forecast_does_not_claim_whole_day_low_rain(self):
  self.partial=True;row,st=self.turn('今天会下雨吗');self.assertIn('不完整',st['speech']);self.assertNotIn('机会较低',st['speech'])
 def test_old_location_requires_refresh_and_no_other_device_location(self):
  with self.s.db() as c:c.execute('UPDATE weather_regions SET captured=?',(time.time()-8000,))
  row,st=self.turn('现在多少度');self.assertEqual(len(self.calls),0);self.assertIn('刷新位置',st['speech'])
  with self.assertRaises(TodoError):self.s.live.weather('other')
 def test_old_weather_replay_is_marked_historical(self):
  row,st=self.turn('现在多少度')
  with patch('time.time',return_value=time.time()+1000):self.assertEqual(public_task(self.s,self.s.task(row['id']))['speech'],'这是之前的查询结果，不能代表现在；请重新查询。')
 def test_unpublished_holiday_is_never_invented(self):
  self.s.live.transport=lambda *a,**k:io.BytesIO(json.dumps({'vcalendar':[{'vevent':[{'dtstart':['20261001',{}],'summary':'National Day'}]}]}).encode())
  row,st=self.turn('2030年国庆节放假是哪天');self.assertIn('尚未提供',row['result'])
 def test_scope_does_not_treat_cpu_temperature_as_local_weather(self):
  from live_intent import readonly
  self.assertFalse(readonly('CPU温度多少度'));self.assertFalse(readonly('解释天气API'));self.assertTrue(readonly('今天热不热'))

class ReceiptTests(unittest.TestCase):
 def setUp(self):
  self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup);self.s=Store(pathlib.Path(self.tmp.name));self.o='receipt-owner'
  with self.s.db() as c:c.executemany('INSERT INTO devices VALUES(?,?)',[(self.o,'A'),('other','B')])
  (self.s.home/'dialogue-engine.json').write_text('{"enabled":true}')
  self.sid=self.s.sessions.create(self.o)['id']
  class Delegate:
   def plan(inner,row,context):
    text=json.loads(row['body'])['text'];return {'relation':'replace','steps':[{'kind':'delegate','text':text,'source_quote':text}],'source':'fixture'}
  self.s.sessions.engine.router=Delegate()
 def submit(self,text):
  st=self.s.sessions.state(self.o,self.sid);ident=self.s.submit({'text':text,'time_zone':'Asia/Hong_Kong','session_id':self.sid,'reply_to':st['task_id']},self.o);return self.s.task(ident)
 def test_direct_calendar_changes_only_after_current_version_phone_ack(self):
  row=self.submit('明天15:00到16:00，安排日历验收');Worker(self.s).execute(row);parent=self.s.task(row['id'])
  self.assertEqual(describe(self.s,parent)['status'],'waiting_calendar');self.assertIn('尚未收到',self.s.sessions.state(self.o,self.sid)['speech'])
  entries=self.s.calendar.entries(self.o)['entries'];self.assertEqual(len(entries),1);e=entries[0]
  self.s.calendar.ack(e['id'],self.o,{'revision':str(e['revision']),'status':'written'})
  self.assertEqual(describe(self.s,parent)['status'],'calendar_synced');self.assertIn('已确认',self.s.sessions.state(self.o,self.sid)['speech'])
  with self.assertRaises(ValueError):self.s.calendar.ack(e['id'],'other',{'revision':str(e['revision']),'status':'written'})
 def test_sync_error_and_removal_never_become_completed(self):
  row=self.submit('明天15:00到16:00，安排日历验收');Worker(self.s).execute(row);e=self.s.calendar.entries(self.o)['entries'][0]
  self.s.calendar.ack(e['id'],self.o,{'revision':str(e['revision']),'status':'error','message':'permission denied'})
  self.assertEqual(describe(self.s,self.s.task(row['id']))['status'],'sync_error')
  self.s.calendar.cancel(e['id'],self.o,{'revision':str(e['revision'])});e=self.s.calendar.entries(self.o)['entries'][0]
  self.assertEqual(describe(self.s,self.s.task(row['id']))['status'],'waiting_calendar')
  self.s.calendar.ack(e['id'],self.o,{'revision':str(e['revision']),'status':'removed'})
  self.assertEqual(describe(self.s,self.s.task(row['id']))['status'],'calendar_removed')
 def test_status_followup_checks_real_receipt_instead_of_completed_execution_flag(self):
  row=self.submit('明天15:00到16:00，安排日历验收');Worker(self.s).execute(row)
  # Direct brain path shares the actual internal session containing the calendar task.
  with self.s.db() as c:child=c.execute('SELECT id,body FROM tasks WHERE owner=? AND id<>? ORDER BY created DESC LIMIT 1',(self.o,row['id'])).fetchone()
  b=json.loads(child['body']);current=self.s.submit({'text':'刚才的日历同步了吗','session_id':b['session_id'],'reply_to':child['id']},self.o)
  self.s.brain.transport=lambda *a,**k:self.fail('Must query receipts')
  out=self.s.brain.run(self.s.task(current));self.assertIn('尚未收到',out['result'])
 def test_deadline_ack_required_and_stale_content_cannot_claim_sync(self):
  item=self.s.todos.create(self.o,{'request_id':'deadline','items':[{'title':'9876作业','deadline':'2030-01-01'}]})['items'][0]
  row=self.submit('记录待办');self.s.update(row['id'],state='completed',plan=json.dumps({'todo_ids':[item['id']]}));row=self.s.task(row['id'])
  self.assertEqual(describe(self.s,row)['status'],'waiting_calendar')
  receipt={'revision':item['revision'],'fingerprint':self.s.todos.calendar_fingerprint(item),'status':'written'}
  with self.assertRaises(TodoError):self.s.todos.calendar_ack('other',item['id'],receipt)
  self.s.todos.calendar_ack(self.o,item['id'],receipt);self.assertEqual(describe(self.s,row)['label'],'截止已同步')
  self.s.todos.update(self.o,item['id'],{'revision':1,'completed':True})
  with self.assertRaises(TodoError):self.s.todos.calendar_ack(self.o,item['id'],receipt)
  self.assertEqual(describe(self.s,row)['status'],'waiting_calendar')
 def test_project_completion_invalidates_deadline_receipt_even_without_parent_revision_change(self):
  items=self.s.todos.create(self.o,{'request_id':'project','items':[{'title':'项目','deadline':'2030-01-01','children':[{'title':'子任务'}]}]})['items']
  parent=items[0];receipt={'revision':parent['revision'],'fingerprint':self.s.todos.calendar_fingerprint(parent),'status':'written'}
  self.s.todos.calendar_ack(self.o,parent['id'],receipt)
  self.s.todos.update(self.o,items[1]['id'],{'revision':1,'completed':True})
  with self.assertRaises(TodoError):self.s.todos.calendar_ack(self.o,parent['id'],receipt)
 def test_named_todo_query_uses_checkbox_not_execution_status(self):
  item=self.s.todos.create(self.o,{'request_id':'named','items':[{'title':'3101复习'}]})['items'][0]
  row=self.submit('3101复习完成了吗');Worker(self.s).execute(row);self.assertIn('还没有标记完成',self.s.task(row['id'])['result'])
  self.s.todos.update(self.o,item['id'],{'revision':1,'completed':True})
  row=self.submit('3101复习完成了吗');Worker(self.s).execute(row);self.assertIn('已标记完成',self.s.task(row['id'])['result'])
 def test_plain_answer_is_processed_not_a_completed_todo(self):
  row=self.submit('你好');self.s.update(row['id'],state='completed',result='你好')
  self.assertEqual(describe(self.s,self.s.task(row['id']))['label'],'已处理')
 def test_unsupported_model_success_claim_without_tool_is_blocked(self):
  self.s.brain.transport=lambda *a,**k:io.BytesIO(json.dumps({'message':{'role':'assistant','content':'已经完成，已写入Apple日历。'}}).encode())
  row=self.submit('请帮我处理一下');out=self.s.brain.run(row);self.assertIn('尚未执行',out['result']);self.assertNotIn('已经完成',out['result'])
if __name__=='__main__':unittest.main()
