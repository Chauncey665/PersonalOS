import datetime as dt,io,json,pathlib,sys,tempfile,unittest,uuid
from unittest.mock import patch
sys.path.insert(0,str(pathlib.Path(__file__).resolve().parents[1]/'server'))
from personal_os import Store,Worker
from task_editing import TaskEditing,requested
from task_entities import candidates
from todos import TodoError
from auto_planner import instant,overlaps
N=dt.datetime.fromisoformat('2026-10-29T11:00:00+08:00').timestamp()
class TaskEditTests(unittest.TestCase):
 def setUp(self):
  self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
  self.clock=patch('time.time',return_value=N);self.clock.start();self.addCleanup(self.clock.stop)
  self.s=Store(pathlib.Path(self.tmp.name));self.o='owner'
  with self.s.db() as c:c.executemany('INSERT INTO devices VALUES(?,?)',[(self.o,'A'),('other','B')])
  self.items=self.s.todos.create(self.o,{'request_id':'seed','items':[{'title':'CS 3101 复习','deadline':'2026-11-01'},{'title':'CS 3101 cheat sheet'},{'title':'Assignment 3: Texture Mapping','notes':'CS3200','deadline':'2026-10-30'},{'title':'Lab 5','notes':'CS3102','deadline':'2026-11-03'},{'title':'Homework 2','notes':'CS3102','deadline':'2026-11-06'}]})['items']
  self.edit=TaskEditing(self.s)
  self.ctx={'captured_at':N,'time_zone':'Asia/Hong_Kong','range_start':'2026-10-29T00:00:00+08:00','range_end':'2026-11-06T00:00:00+08:00','truncated':False,'events':[{'start':'2026-10-30T12:30:00+08:00','end':'2026-10-30T14:20:00+08:00','all_day':False}]}
 def row(self,text,**body):return self.s.task(self.s.submit({'text':text,'time_zone':'Asia/Hong_Kong',**body},self.o))
 def spec(self,target,op,value,text):return {'target':target,'operation':op,'value':value,'quote':text}
 def current(self,i):return next(x for x in self.s.todos.list(self.o)['items'] if x['id']==self.items[i]['id'])
 def test_course_aliases_disambiguate_and_do_not_confuse_courses(self):
  for q in ['三一零一的复习','3101复习','CS3101 复习']:self.assertEqual(candidates(self.items,q)[0]['id'],self.items[0]['id'])
  self.assertEqual(candidates(self.items,'三二零零的作业')[0]['id'],self.items[2]['id'])
  self.assertEqual(len(candidates(self.items,'3102作业')),2)
  self.assertEqual(candidates(self.items,'3102 HW2')[0]['id'],self.items[4]['id'])
  self.assertEqual(candidates(self.items,'3101作业三'),[])
 def test_postpone_duration_real_calendar_class_buffer_deadline_and_idempotency(self):
  self.s.planner.configure(self.o,{'enabled':True});self.s.planner.context(self.o,self.ctx)
  text='把三一零一的复习改到明天下午，预计用时改成一个半小时'
  row=self.row(text);specs=[self.spec('三一零一的复习','postpone','明天下午',text),self.spec('三一零一的复习','duration','一个半小时',text)]
  out=self.edit.prepare(row,specs);self.assertEqual(out['state'],'completed')
  slot=next(x for x in self.s.planner.status(self.o)['slots'] if x['todo_id']==self.items[0]['id'])
  self.assertGreaterEqual(instant(slot['start']),instant('2026-10-30T12:00:00+08:00'));self.assertLessEqual(instant(slot['end']),instant('2026-10-30T18:00:00+08:00'));self.assertEqual(instant(slot['end'])-instant(slot['start']),5400)
  self.assertFalse(overlaps((instant(slot['start']),instant(slot['end'])),(instant('2026-10-30T12:15:00+08:00'),instant('2026-10-30T14:35:00+08:00'))))
  self.assertEqual(self.current(0)['deadline'],'2026-11-01');revision=self.current(0)['revision'];self.edit.prepare(row,specs);self.assertEqual(self.current(0)['revision'],revision)
  self.assertEqual(slot['device_sync'],'pending');self.assertNotIn('已写入',out['result'])
 def test_ambiguous_choice_changes_nothing_until_same_session_selection(self):
  session=self.s.sessions.create(self.o)['id'];text='把3102的作业优先级改为高'
  row=self.row(text,session_id=session);out=self.edit.prepare(row,[self.spec('3102的作业','priority','高',text)]);self.s.update(row['id'],**out)
  self.assertEqual(out['state'],'needs_details')
  with self.s.db() as c:self.assertEqual(c.execute('SELECT count(*) FROM todo_priorities').fetchone()[0],0)

  with self.s.db() as c:c.execute("UPDATE assistant_sessions SET status='needs_input' WHERE id=?",(session,))
  r=self.row('第二个',session_id=session,reply_to=row['id']);result=self.edit.followup(r,self.s.task(row['id']))
  self.assertEqual(result['state'],'completed');self.assertEqual(self.current(3)['revision'],1);self.assertEqual(self.current(4)['revision'],2)
  with self.assertRaises(TodoError):self.edit.followup({**r,'owner':'other'},self.s.task(row['id']))
 def test_stale_candidate_cannot_edit_new_revision(self):
  session=self.s.sessions.create(self.o)['id'];text='把3102的作业优先级改为高';row=self.row(text,session_id=session)
  self.s.update(row['id'],**self.edit.prepare(row,[self.spec('3102的作业','priority','高',text)]))
  self.s.todos.update(self.o,self.items[4]['id'],{'revision':1,'notes':'Changed'})
  with self.s.db() as c:c.execute("UPDATE assistant_sessions SET status='needs_input' WHERE id=?",(session,))
  r=self.row('第二个',session_id=session,reply_to=row['id'])
  with self.assertRaises(TodoError):self.edit.followup(r,self.s.task(row['id']))
 def test_atomic_rollback_on_second_invalid_edit_and_no_fabricated_values(self):
  text='把3101复习预计用时改成45分钟，优先级提高';row=self.row(text)
  specs=[self.spec('3101复习','duration','45分钟',text),self.spec('3101复习','priority','提高',text)]
  # 提高 is deliberately unsupported until an actual level is resolved.
  specs[1]['value']='优先级'
  with self.assertRaises(TodoError):self.edit.prepare(row,specs)
  self.assertEqual(self.current(0)['revision'],1)
  with self.s.db() as c:self.assertEqual(c.execute('SELECT count(*) FROM todo_estimates').fetchone()[0],0)
  with self.assertRaises(TodoError):self.edit.prepare(row,[self.spec('3101复习','duration','90分钟',text)])
 def test_pause_resume_complete_reopen_keep_todo_and_calendar_receipts(self):
  self.s.planner.configure(self.o,{'enabled':True});self.s.planner.context(self.o,self.ctx)
  for text,op in [('3101复习暂停安排','pause'),('3101复习恢复安排','resume'),('3101复习标记完成','complete'),('3101复习取消完成','reopen')]:
   out=self.edit.prepare(self.row(text),[self.spec('3101复习',op,'',text)]);self.assertEqual(out['state'],'completed')
   slot=next(x for x in self.s.planner.status(self.o)['slots'] if x['todo_id']==self.items[0]['id'])
   if op=='pause':self.assertEqual(slot['state'],'paused');self.assertIsNone(slot['start'])
   if op=='complete':self.assertTrue(self.current(0)['completed'])
  self.assertFalse(self.current(0)['completed']);self.assertEqual(len(self.s.todos.list(self.o)['items']),5)
 def test_split_preserves_source_deadline_and_cancels_parent_calendar(self):
  self.s.planner.configure(self.o,{'enabled':True});self.s.planner.context(self.o,self.ctx)
  text='把3200作业拆成查资料、写初稿、检查';row=self.row(text)
  self.edit.prepare(row,[self.spec('3200作业','split','拆成查资料、写初稿、检查',text)])
  parent=self.current(2);self.assertEqual(parent['kind'],'project');self.assertEqual(parent['deadline'],'2026-10-30')
  children=[x for x in self.s.todos.list(self.o)['items'] if x['parent_id']==parent['id']]
  self.assertEqual(len(children),3);self.assertTrue(all(x['deadline'] is None for x in children))
  slot=next(x for x in self.s.planner.status(self.o)['slots'] if x['todo_id']==parent['id']);self.assertEqual(slot['state'],'paused')
 def test_explicit_deadline_vs_execution_date_and_rename(self):
  text='把3200作业截止改到下周一';out=self.edit.prepare(self.row(text),[self.spec('3200作业','deadline','下周一',text)])
  self.assertIn('2026-11-02',out['result']);self.assertEqual(self.current(2)['deadline'],'2026-11-02')
  text='把3101复习标题改为考前复习';self.edit.prepare(self.row(text),[self.spec('3101复习','rename','考前复习',text)])
  self.assertEqual(self.current(0)['title'],'考前复习')
 def test_negations_creation_questions_and_quoted_commands_are_not_edits(self):
  for text in ['不要把3101复习改到明天','怎么把3101复习改到明天','解释“把3101复习改到明天”','新增一个任务预计45分钟','把任务拆分是什么意思']:
   self.assertFalse(requested(text))
 def test_worker_and_real_dialogue_followup_contract(self):
  (self.s.home/'dialogue-engine.json').write_text('{"enabled":true}')
  text='把3102作业预计用时改为45分钟';spec=self.spec('3102作业','duration','45分钟',text)
  self.s.brain.transport=lambda *a,**k:io.BytesIO(json.dumps({'message':{'content':json.dumps({'edits':[spec]})}}).encode())
  session=self.s.sessions.create(self.o)['id'];row=self.row(text,session_id=session);Worker(self.s).execute(row)
  st=self.s.sessions.state(self.o,session);self.assertEqual(st['status'],'needs_input')
  row2=self.row('第二个',session_id=session,reply_to=row['id']);Worker(self.s).execute(row2)
  st=self.s.sessions.state(self.o,session);self.assertEqual(st['status'],'completed',st)
  self.assertEqual(self.s.planner.status(self.o)['estimates'][self.items[4]['id']],45)
 def test_contextual_duration_is_same_session_and_rejects_stale_object(self):
  (self.s.home/'dialogue-engine.json').write_text('{"enabled":true}')
  text='把3101复习预计用时改为45分钟';spec=self.spec('3101复习','duration','45分钟',text)
  self.s.brain.transport=lambda *a,**k:io.BytesIO(json.dumps({'message':{'content':json.dumps({'edits':[spec]})}}).encode())
  session=self.s.sessions.create(self.o)['id'];row=self.row(text,session_id=session);Worker(self.s).execute(row)
  st=self.s.sessions.state(self.o,session)
  r=self.row('再改成90分钟',session_id=session,reply_to=row['id']);Worker(self.s).execute(r)
  self.assertEqual(self.s.planner.status(self.o)['estimates'][self.items[0]['id']],90)
  other=self.s.sessions.create(self.o)['id'];r2=self.row('再改成60分钟',session_id=other)
  self.assertIsNone(self.edit.contextual(r2))
 def test_calendar_ack_is_versioned_and_stale_ack_does_not_confirm_new_schedule(self):
  self.s.planner.configure(self.o,{'enabled':True});self.s.planner.context(self.o,self.ctx)
  before=next(x for x in self.s.planner.status(self.o)['slots'] if x['todo_id']==self.items[0]['id'])
  text='把3101复习改到明天下午';self.edit.prepare(self.row(text),[self.spec('3101复习','postpone','明天下午',text)])
  after=next(x for x in self.s.planner.status(self.o)['slots'] if x['todo_id']==self.items[0]['id'])
  self.assertEqual(before['entry_id'],after['entry_id']);self.assertGreater(after['revision'],before['revision'])
  with self.assertRaises(ValueError):self.s.calendar.ack(after['entry_id'],self.o,{'revision':str(before['revision']),'status':'written'})
  receipt=self.s.calendar.ack(after['entry_id'],self.o,{'revision':str(after['revision']),'status':'written'})
  self.assertEqual(receipt['device_sync'],'written')
 def test_project_pause_and_postpone_apply_to_children_without_changing_deadline(self):
  self.s.planner.configure(self.o,{'enabled':True});self.s.planner.context(self.o,self.ctx)
  text='把3200作业拆成查资料、写初稿、检查';self.edit.prepare(self.row(text),[self.spec('3200作业','split','拆成查资料、写初稿、检查',text)])
  children=[x for x in self.s.todos.list(self.o)['items'] if x['parent_id']==self.items[2]['id']]
  text='3200作业暂停安排';self.edit.prepare(self.row(text),[self.spec('3200作业','pause','',text)])
  slots=self.s.planner.status(self.o)['slots'];self.assertTrue(all(x['state']=='paused' for x in slots if x['todo_id'] in {c['id'] for c in children}))
  text='3200作业恢复安排';self.edit.prepare(self.row(text),[self.spec('3200作业','resume','',text)])
  text='3200作业改到明天下午';self.edit.prepare(self.row(text),[self.spec('3200作业','postpone','明天下午',text)])
  slots=self.s.planner.status(self.o)['slots']
  for slot in slots:
   if slot['todo_id'] in {c['id'] for c in children} and slot['start']:self.assertGreaterEqual(instant(slot['start']),instant('2026-10-30T12:00:00+08:00'))
  self.assertEqual(self.current(2)['deadline'],'2026-10-30')
 def test_restart_keeps_pending_choice_and_replay_receipt(self):
  (self.s.home/'dialogue-engine.json').write_text('{"enabled":true}')
  text='3102作业预计用时改成45分钟';spec=self.spec('3102作业','duration','45分钟',text)
  self.s.brain.transport=lambda *a,**k:io.BytesIO(json.dumps({'message':{'content':json.dumps({'edits':[spec]})}}).encode())
  session=self.s.sessions.create(self.o)['id'];row=self.row(text,session_id=session);Worker(self.s).execute(row)
  self.s=Store(self.s.home)
  r=self.row('第二个',session_id=session,reply_to=row['id']);Worker(self.s).execute(r)
  self.assertEqual(self.s.sessions.state(self.o,session)['status'],'completed')
  self.assertEqual(self.s.planner.status(self.o)['estimates'][self.items[4]['id']],45)
  before=self.current(4)['revision'];Worker(self.s).execute(self.s.task(r['id']));self.assertEqual(self.current(4)['revision'],before)
 def test_old_reminder_is_replaced_after_window_change_and_stale_context_retracts(self):
  self.s.planner.configure(self.o,{'enabled':True})
  with self.s.db() as c:c.execute('INSERT INTO todo_schedule_windows VALUES(?,?,?,?,?,?)',(self.o,self.items[0]['id'],N,None,1,N))
  self.s.planner.context(self.o,self.ctx)
  with self.s.db() as c:before=c.execute("SELECT due FROM tasks WHERE state='scheduled'").fetchone()[0]
  text='3101复习改到明天下午';self.edit.prepare(self.row(text),[self.spec('3101复习','postpone','明天下午',text)])
  with self.s.db() as c:
   rows=c.execute("SELECT due FROM tasks WHERE state='scheduled'").fetchall();self.assertEqual(len(rows),1);self.assertGreater(rows[0]['due'],before)
  with patch('time.time',return_value=N+1000):
   text='3101复习改到后天下午';self.edit.prepare(self.row(text),[self.spec('3101复习','postpone','后天下午',text)])
   with self.s.db() as c:self.assertEqual(c.execute("SELECT count(*) FROM tasks WHERE state='scheduled'").fetchone()[0],0)
 def test_model_spacing_is_grounded_and_negated_completion_becomes_reopen(self):
  text='把3101复习用时改成45分钟';spec=self.spec('3101 复习','duration','45 分钟','把 3101 复习用时改成 45 分钟')
  self.edit.prepare(self.row(text),[spec]);self.assertEqual(self.s.planner.status(self.o)['estimates'][self.items[0]['id']],45)
  self.s.todos.update(self.o,self.items[0]['id'],{'revision':2,'completed':True})
  text='3101复习取消完成';self.edit.prepare(self.row(text),[self.spec('3101复习','complete','',text)])
  self.assertFalse(self.current(0)['completed'])
 def test_postponing_past_deadline_reports_unplaced_without_changing_deadline(self):
  self.s.planner.configure(self.o,{'enabled':True});self.s.planner.context(self.o,self.ctx)
  text='3200作业改到下周一';out=self.edit.prepare(self.row(text),[self.spec('3200作业','postpone','下周一',text)])
  self.assertIn('未排上',out['result']);self.assertEqual(json.loads(out['plan'])['calendar_sync'],'unscheduled')
  self.assertEqual(self.current(2)['deadline'],'2026-10-30')
 def test_no_fresh_context_records_window_without_invented_slot(self):
  text='把3101复习改到明天下午';self.edit.prepare(self.row(text),[self.spec('3101复习','postpone','明天下午',text)])
  self.assertEqual(self.s.planner.status(self.o)['slots'],[])
if __name__=='__main__':unittest.main()
