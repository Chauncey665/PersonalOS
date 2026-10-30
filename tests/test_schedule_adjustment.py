import datetime as dt,io,json,pathlib,sys,tempfile,time,unittest
from unittest.mock import patch
sys.path.insert(0,str(pathlib.Path(__file__).resolve().parents[1]/'server'))
from personal_os import Store
from schedule_adjustment import ScheduleAdjustment,normalized
from todos import TodoError
N=dt.datetime.fromisoformat('2026-10-28T21:00:00+08:00').timestamp()
TEXT='我今晚只要做三一零一的复习和三一零一的 cheat sheet，然后帮我把其他的任务都推到明天。然后因为明晚不是截止那个三二零零的作业吗？所以提醒我要把三二零零做完。'
class AdjustmentTests(unittest.TestCase):
 def setUp(self):
  self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup);self.clock=patch('time.time',return_value=N);self.clock.start();self.addCleanup(self.clock.stop);self.s=Store(pathlib.Path(self.tmp.name));self.o='own'
  with self.s.db() as c:c.executemany('INSERT INTO devices VALUES(?,?)',[(self.o,'A'),('other','B')])
  self.rows=self.s.todos.create(self.o,{'request_id':'fixture','items':[{'title':'3101复习','deadline':'2026-10-28T23:59:00+08:00'},{'title':'Assignment 3','notes':'CS3200','deadline':'2026-10-30'},{'title':'Lab5','notes':'CS3102','deadline':'2026-11-03'}]})['items']
  self.s.planner.configure(self.o,{'enabled':True});self.session=self.s.sessions.create(self.o)['id'];self.ident=self.s.submit({'text':TEXT,'time_zone':'Asia/Hong_Kong'},self.o);self.row=self.s.task(self.ident)
  self.p={'focus':[{'existing_id':self.rows[0]['id'],'new_title':'','quote':'三一零一的复习'},{'existing_id':'','new_title':'3101 cheat sheet','quote':'三一零一的 cheat sheet'}],'remind_ids':[self.rows[1]['id']]}
 def apply(self):return ScheduleAdjustment(self.s).apply(self.row,self.p)
 def context(self):return {'captured_at':N,'time_zone':'Asia/Hong_Kong','range_start':'2026-10-28T00:00:00+08:00','range_end':'2026-11-05T00:00:00+08:00','truncated':False,'events':[{'start':'2026-10-29T11:00:00+08:00','end':'2026-10-29T12:00:00+08:00','all_day':False}]}
 def test_exact_utterance_creates_only_missing_item_and_preserves_deadline(self):
  out=self.apply();self.assertEqual(out['state'],'completed');items=self.s.todos.list(self.o)['items'];self.assertEqual(len(items),4);self.assertEqual(next(x for x in items if x['id']==self.rows[1]['id'])['deadline'],'2026-10-30');self.assertIsNone(items[-1]['deadline']);self.apply();self.assertEqual(len(self.s.todos.list(self.o)['items']),4)
 def test_waits_for_fresh_calendar_then_schedules_tonight_and_tomorrow(self):
  self.apply();self.assertFalse(self.s.planner.status(self.o)['slots']);st=self.s.planner.context(self.o,self.context())
  slots={x['todo_id']:x for x in st['slots']};self.assertTrue(slots[self.rows[0]['id']]['start'].startswith('2026-10-28T'))
  self.assertTrue(slots[self.rows[1]['id']]['start'].startswith('2026-10-29T'));self.assertNotIn('11:00:00',slots[self.rows[1]['id']]['start'])
  with self.s.db() as c:rem=c.execute("SELECT due FROM tasks WHERE state='scheduled'").fetchall()
  self.assertEqual(len(rem),1);self.s.planner.reconcile(self.o)
  with self.s.db() as c:self.assertEqual(c.execute("SELECT count(*) FROM tasks WHERE state='scheduled'").fetchone()[0],1)
 def test_forged_id_course_and_unknown_title_rollback(self):
  for mode in ('id','title','focus'):
   p=json.loads(json.dumps(self.p))
   if mode=='id':p['focus'][0]['existing_id']='bad'
   if mode=='title':p['focus'][1]['new_title']='杜撰事项'
   if mode=='focus':p['focus'][0]['quote']='三二零零做完'
   with self.assertRaises(TodoError):ScheduleAdjustment(self.s).apply(self.row,p)
   self.assertEqual(len(self.s.todos.list(self.o)['items']),3)
 def test_mistaken_known_id_cannot_turn_cheat_sheet_into_canvas_assignment(self):
  p=json.loads(json.dumps(self.p));p['focus'][1].update(existing_id=self.rows[1]['id'],new_title='')
  ScheduleAdjustment(self.s).apply(self.row,p)
  with self.s.db() as c:
   rule=c.execute('SELECT not_before FROM todo_schedule_windows WHERE todo_id=?',(self.rows[1]['id'],)).fetchone()[0]
  self.assertGreater(rule,N);self.assertEqual(len(self.s.todos.list(self.o)['items']),4)
 def test_other_owner_cannot_modify_tasks(self):
  with self.assertRaises(TodoError):ScheduleAdjustment(self.s).apply({**self.row,'owner':'other'},self.p)
 def test_negation_no_write(self):
  b=json.loads(self.row['body']);b['text']=TEXT.replace('帮我把其他','不要帮我把其他');row={**self.row,'body':json.dumps(b)}
  self.assertIsNone(ScheduleAdjustment(self.s).run(row));self.assertEqual(len(self.s.todos.list(self.o)['items']),3)
 def test_real_interpreter_contract_uses_context_not_invented_ids(self):
  self.s.brain.transport=lambda *a,**k:io.BytesIO(json.dumps({'message':{'content':json.dumps(self.p)}}).encode());out=ScheduleAdjustment(self.s).run(self.row);self.assertIn('其他 2 项',out['result']);self.assertEqual(normalized('三一零一的 cheat sheet'),'3101cheatsheet')
if __name__=='__main__':unittest.main()
