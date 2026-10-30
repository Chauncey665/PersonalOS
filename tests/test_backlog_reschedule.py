import datetime as dt,json,pathlib,sys,tempfile,time,unittest,uuid
from unittest.mock import patch
sys.path.insert(0,str(pathlib.Path(__file__).resolve().parents[1]/'server'))
from personal_os import Store,Worker
from backlog_reschedule import BacklogReschedule,requested
from failure_evidence import run as explain
from auto_planner import instant,overlaps
from task_outcomes import describe
NOW=instant('2026-10-30T10:00:00+08:00')
class BacklogTests(unittest.TestCase):
 def setUp(self):
  self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup);self.clock=patch('time.time',return_value=NOW);self.clock.start();self.addCleanup(self.clock.stop)
  self.s=Store(pathlib.Path(self.tmp.name));self.o='owner';self.b='other'
  with self.s.db() as c:c.executemany('INSERT INTO devices VALUES(?,?)',[(self.o,'phone'),(self.b,'other')])
  self.s.planner.configure(self.o,{'enabled':True});self.h=BacklogReschedule(self.s)
  self.ctx={'captured_at':NOW,'time_zone':'Asia/Hong_Kong','range_start':'2026-10-30T00:00:00+08:00','range_end':'2026-11-07T00:00:00+08:00','truncated':False,'events':[{'start':'2026-10-30T11:15:00+08:00','end':'2026-10-30T11:35:00+08:00','all_day':False}]}
 def add(self,title,day='2026-10-29',completed=False,owner=None,manual=False,deadline=None):
  owner=owner or self.o;t=self.s.todos.create(owner,{'request_id':uuid.uuid4().hex,'items':[{'title':title,'deadline':deadline}]})['items'][0];eid=str(uuid.uuid4());start=day+'T15:00:00+08:00';end=day+'T15:45:00+08:00'
  with self.s.db() as c:
   c.execute('UPDATE todos SET completed=? WHERE id=?',(int(completed),t['id']))
   c.execute("INSERT INTO calendar_entries(id,owner,kind,title,start,end,time_zone,all_day,status,revision,question,device_sync,sync_error,original_text,created,updated,last_op,last_hash,previous_revision) VALUES(?,?,?,?,?,?,'Asia/Hong_Kong',0,'ready',1,'','written','','',?,? ,?,'',0)",(eid,owner,'done' if completed else 'plan',title,start,end,NOW-86400,NOW-86400,'resolve' if manual else 'auto_plan'))
   c.execute('INSERT INTO auto_schedule VALUES(?,?,?,?,?,?)',(owner,t['id'],eid,'manual' if manual else 'started','',NOW-86400))
  return t['id'],eid
 def row(self,text='帮我把我昨天没做的东西挪到今天吧。'):
  ident=self.s.submit({'text':text,'request_id':uuid.uuid4().hex,'time_zone':'Asia/Hong_Kong'},self.o);return self.s.task(ident)
 def test_real_old_schedules_selected_not_deadlines_future_or_completed(self):
  a,e=self.add('yesterday',deadline='2026-10-30');done,_=self.add('done',completed=True);future,_=self.add('fitness','2026-11-01');older,_=self.add('older','2026-10-28');other,_=self.add('foreign',owner=self.b);manual,_=self.add('manual',manual=True)
  self.s.planner.context(self.o,self.ctx);r=self.row();out=self.h.run(r);self.s.update(r['id'],**out)
  self.assertEqual(json.loads(out['plan'])['todo_ids'],[a]);self.assertEqual(next(x for x in self.s.todos.list(self.o)['items'] if x['id']==a)['deadline'],'2026-10-30')
  slot=next(x for x in self.s.planner.status(self.o)['slots'] if x['todo_id']==a);self.assertEqual(slot['entry_id'],e);self.assertEqual(slot['device_sync'],'pending');self.assertTrue(slot['start'].startswith('2026-10-30'))
  self.assertNotEqual(describe(self.s,self.s.task(r['id']))['status'],'calendar_synced')
  self.assertEqual(self.h.run(r),out)
  with self.s.db() as c:self.assertEqual(c.execute('SELECT count(*) FROM calendar_entries WHERE id=?',(e,)).fetchone()[0],1)
 def test_stale_context_records_intent_then_fresh_context_schedules(self):
  a,e=self.add('old');r=self.row();out=self.h.run(r);slot=self.s.planner.status(self.o)['slots'][0];self.assertEqual(slot['state'],'reschedule');self.assertIsNone(slot['start'])
  self.s.planner.context(self.o,self.ctx);slot=self.s.planner.status(self.o)['slots'][0];self.assertEqual(slot['entry_id'],e);self.assertEqual(slot['state'],'scheduled')
 def test_completed_after_selection_prevents_partial_changes(self):
  a,_=self.add('A');b,_=self.add('B');original=self.h.candidates
  def race(owner,spec):
   result=original(owner,spec)
   with self.s.db() as c:c.execute('UPDATE todos SET completed=1,revision=revision+1 WHERE id=?',(b,))
   return result
  with patch.object(self.h,'candidates',side_effect=race):
   with self.assertRaises(ValueError):self.h.run(self.row())
  with self.s.db() as c:self.assertEqual(c.execute('SELECT count(*) FROM backlog_receipts').fetchone()[0],0);self.assertEqual(c.execute('SELECT count(*) FROM todo_schedule_windows').fetchone()[0],0)
 def test_overdue_does_not_silently_change_deadline(self):
  a,_=self.add('overdue',deadline='2026-10-29');self.s.planner.context(self.o,self.ctx);self.h.run(self.row());self.assertEqual(next(x for x in self.s.todos.list(self.o)['items'] if x['id']==a)['deadline'],'2026-10-29');self.assertEqual(self.s.planner.status(self.o)['slots'][0]['state'],'unscheduled')
 def test_no_space_never_overlaps_or_claims_calendar_success(self):
  self.add('A');self.ctx['events']=[{'start':'2026-10-30T00:00:00+08:00','end':'2026-10-31T02:00:00+08:00','all_day':False}];self.s.planner.context(self.o,self.ctx);out=self.h.run(self.row());self.assertEqual(self.s.planner.status(self.o)['slots'][0]['state'],'unscheduled')
 def test_rejection_and_readonly_query(self):
  for text in ['不要把昨天没做的东西挪到今天','为什么昨天未完成任务没挪到今天','翻译“把昨天未完成任务挪到今天”','把昨天的文件挪到今天文件夹']:
   self.assertFalse(requested(text))
  self.add('old');data=self.s.brain.execute_tool('list_scheduled_todos',{'date':'昨天'},self.row('查看昨天安排'),{});self.assertEqual(data['items'][0]['title'],'old')
  with self.assertRaises(ValueError):self.s.brain.execute_tool('reschedule_unfinished',{},self.row('查看昨天安排'),{})
 def test_original_voice_wording_routes_without_model_or_timeout(self):
  self.add('old');r=self.row('我的意思是把昨天排了计划，但是我最后没有打勾，就是我没有做完的东西，挪到，就是重新排时间。')
  with patch.object(self.s.brain,'run',side_effect=AssertionError('model must not guess bulk target')):Worker(self.s).execute(r)
  self.assertEqual(self.s.task(r['id'])['state'],'completed');self.assertEqual(len(json.loads(self.s.task(r['id'])['plan'])['todo_ids']),1)
 def test_failure_explanation_from_actual_timeout_and_no_foreign_reference(self):
  prev=self.row('failed');self.s.update(prev['id'],state='failed');self.s.audit(prev['id'],'brain_model_error',{'type':'timeout','elapsed_seconds':110})
  row=self.row('为什么刚才的任务本地模型无法完成');body=json.loads(row['body']);body['reply_to']=prev['id'];row['body']=json.dumps(body)
  out=explain(self.s,row);self.assertIn('110',out['result']);self.assertIn('没有切换到云端',out['result']);self.assertNotIn('尝试切换',out['result'])
  row['owner']=self.b;out=explain(self.s,row);self.assertEqual(out['state'],'needs_details');self.assertNotIn('110',out['result'])
 def test_history_limit_normalized_and_inventory_contains_real_schedule(self):
  self.add('old');r=self.row('查看任务')
  for value in ('5',50,5.0,None):
   result=self.s.brain.execute_tool('task_history',{'limit':value},r,{});self.assertLessEqual(len(result['tasks']),20)
  with self.assertRaises(ValueError):self.s.brain.execute_tool('task_history',{'limit':True},r,{})
  result=self.s.brain.execute_tool('list_todos',{},r,{});self.assertTrue(result['items'][0]['execution_schedule']['start'].startswith('2026-10-29'))
if __name__=='__main__':unittest.main()
