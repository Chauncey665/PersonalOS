import datetime as dt,hashlib,json,pathlib,sys,tempfile,time,unittest
sys.path.insert(0,str(pathlib.Path(__file__).resolve().parents[1]/'server'))
from personal_os import Store
from auto_planner import instant,overlaps
from todos import TodoError
from unittest.mock import patch
NOW=dt.datetime.fromisoformat('2026-10-28T10:00:00+08:00').timestamp()
class PlannerTests(unittest.TestCase):
 def setUp(self):
  self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup);self.clock=patch('time.time',return_value=NOW);self.clock.start();self.addCleanup(self.clock.stop)
  self.s=Store(pathlib.Path(self.temp.name));self.a=hashlib.sha256(b'A').hexdigest();self.b=hashlib.sha256(b'B').hexdigest()
  with self.s.db() as c:c.executemany('INSERT INTO devices VALUES(?,?)',[(self.a,'A'),(self.b,'B')])
  self.s.planner.configure(self.a,{'enabled':True});self.context={'captured_at':NOW,'time_zone':'Asia/Hong_Kong','range_start':'2026-10-28T00:00:00+08:00','range_end':'2026-11-05T00:00:00+08:00','truncated':False,'events':[{'start':'2026-10-28T12:00:00+08:00','end':'2026-10-28T13:20:00+08:00','all_day':False},{'start':'2026-10-28T14:00:00+08:00','end':'2026-10-28T16:20:00+08:00','all_day':False}]}
 def add(self,title='任务',deadline='2026-10-30',children=None):
  x={'title':title,'deadline':deadline}
  if children:x.update(kind='project',children=children)
  return self.s.todos.create(self.a,{'request_id':str(time.monotonic_ns()),'items':[x]})['items']
 def slots(self):return self.s.planner.status(self.a)['slots']
 def test_vague_daypart_is_not_a_deadline_and_does_not_move_to_today(self):
  item=self.s.todos.create(self.a,{'request_id':'tomorrow','items':[{'title':'拿快递','notes':'原始要求：明天下午提醒我拿快递'}]})['items'][0]
  self.s.planner.context(self.a,self.context);slot=self.slots()[0]
  self.assertIsNone(item['deadline']);self.assertGreaterEqual(instant(slot['start']),instant('2026-10-29T12:00:00+08:00'));self.assertLessEqual(instant(slot['end']),instant('2026-10-29T18:00:00+08:00'))
 def test_actual_class_gaps_meals_deadlines_and_stable_ids(self):
  self.add('先到期','2026-10-28');self.add('后到期','2026-11-01');self.add('无截止',None)
  result=self.s.planner.context(self.a,self.context);slots=self.slots();self.assertEqual(len(slots),3)
  events=[(instant(e['start'])-900,instant(e['end'])+900) for e in self.context['events']]
  meals=[(instant(e['start']),instant(e['end'])) for e in result['details']['meals']]
  for slot in slots:
   pair=(instant(slot['start']),instant(slot['end']));self.assertEqual(pair[1]-pair[0],2700);self.assertFalse(any(overlaps(pair,b) for b in events+meals))
  self.assertTrue(all(instant(e['end'])-instant(e['start'])==2700 for e in result['details']['meals']))
  self.s.planner.reconcile(self.a);self.assertEqual({s['entry_id'] for s in self.slots()},{s['entry_id'] for s in slots})
  self.assertEqual(len(self.s.calendar.entries(self.a)['entries']),3);self.assertTrue(all('todo_id' in e for e in self.s.calendar.entries(self.a)['entries']));self.assertEqual(self.s.planner.status(self.b)['slots'],[])
 def test_missing_truncated_stale_and_owner_snapshots_cannot_invent_time(self):
  self.add();self.s.planner.reconcile(self.a);self.assertEqual(self.slots(),[])
  for value in [{**self.context,'truncated':True},{**self.context,'captured_at':NOW-300}]:
   with self.assertRaises(TodoError):self.s.planner.context(self.a,value)
  self.s.planner.context(self.b,self.context);self.assertEqual(self.slots(),[])
 def test_completed_task_moves_calendar_kind_and_missing_written_event_not_recreated(self):
  todo=self.add()[0];self.s.planner.context(self.a,self.context);slot=self.slots()[0]
  with self.s.db() as c:c.execute("UPDATE calendar_entries SET device_sync='written' WHERE id=?",(slot['entry_id'],))
  self.s.planner.reconcile(self.a);self.assertEqual(self.slots()[0]['state'],'manual')
  self.s.todos.update(self.a,todo['id'],{'revision':todo['revision'],'completed':True});self.s.planner.reconcile(self.a)
  self.assertEqual(self.s.calendar.entries(self.a)['entries'][0]['kind'],'done')
 def test_parent_deadline_bounds_children_without_overwriting_child_deadlines(self):
  rows=self.add('项目','2026-10-28',[{'title':'子任务一'},{'title':'子任务二'}]);self.s.planner.context(self.a,self.context)
  self.assertEqual(len(self.slots()),2);self.assertTrue(all(instant(s['end'])<=instant('2026-10-29T00:00:00+08:00') for s in self.slots()))
  self.assertTrue(all(r['deadline'] is None for r in rows if r['parent_id']))
 def test_cross_midnight_work_window_and_explicit_reschedule(self):
  todo=self.add(deadline=None)[0];self.s.planner.context(self.a,self.context);slot=self.slots()[0]
  self.s.planner.estimate(self.a,todo['id'],90,True);changed=self.slots()[0];self.assertEqual(instant(changed['end'])-instant(changed['start']),5400)
  later=instant('2026-10-28T23:30:00+08:00');context={**self.context,'captured_at':later,'events':[]}
  with patch('time.time',return_value=later):
   self.s.planner.estimate(self.a,todo['id'],45,True);self.s.planner.context(self.a,context)
   slot=self.slots()[0];self.assertGreaterEqual(instant(slot['start']),later+900);self.assertLessEqual(instant(slot['end']),instant('2026-10-29T01:00:00+08:00'))
 def test_external_movement_and_duplicate_auto_marker_freeze_schedule(self):
  self.add();self.s.planner.context(self.a,self.context);s=self.slots()[0]
  e={'start':'2026-10-28T20:00:00+08:00','end':'2026-10-28T20:45:00+08:00','all_day':False,'auto_id':s['entry_id']}
  self.s.planner.context(self.a,{**self.context,'events':[e]});self.assertEqual(self.slots()[0]['state'],'manual')
  self.assertEqual(self.slots()[0]['start'],s['start'])
 def test_completion_undo_past_written_slot_does_not_invent_new_time(self):
  todo=self.add()[0];self.s.planner.context(self.a,self.context);slot=self.slots()[0]
  with self.s.db() as c:c.execute("UPDATE calendar_entries SET device_sync='written' WHERE id=?",(slot['entry_id'],))
  self.s.todos.update(self.a,todo['id'],{'revision':1,'completed':True});self.s.planner.reconcile(self.a)
  later=instant(slot['end'])+3600
  with patch('time.time',return_value=later):
   self.s.todos.update(self.a,todo['id'],{'revision':2,'completed':False});self.s.planner.reconcile(self.a)
   # A refreshed snapshot must not turn unchecking into a new future time.
   self.s.planner.context(self.a,{**self.context,'captured_at':later,'events':[]})
   now=self.slots()[0];self.assertEqual(now['start'],slot['start']);self.assertEqual(now['state'],'started')
 def test_two_identical_auto_markers_lock_instead_of_treating_both_as_free(self):
  self.add();self.s.planner.context(self.a,self.context);s=self.slots()[0]
  e={'start':s['start'],'end':s['end'],'all_day':False,'auto_id':s['entry_id']}
  self.s.planner.context(self.a,{**self.context,'events':[e,e]});self.assertEqual(self.slots()[0]['state'],'manual');self.assertEqual(self.slots()[0]['start'],s['start'])
 def test_new_meeting_removes_last_available_slot_then_recovers_same_event_id(self):
  self.add(deadline='2026-10-28T12:00:00+08:00');self.s.planner.context(self.a,{**self.context,'events':[]});slot=self.slots()[0]
  self.assertTrue(slot['start'].endswith('11:00:00+08:00'))
  with self.s.db() as c:c.execute("UPDATE calendar_entries SET device_sync='written' WHERE id=?",(slot['entry_id'],))
  own={'start':slot['start'],'end':slot['end'],'all_day':False,'auto_id':slot['entry_id']}
  busy={'start':'2026-10-28T11:00:00+08:00','end':'2026-10-28T12:00:00+08:00','all_day':False}
  self.s.planner.context(self.a,{**self.context,'events':[own,busy]});self.assertEqual(self.slots()[0]['state'],'unscheduled')
  self.assertEqual(self.s.calendar.entries(self.a)['entries'][0]['status'],'cancelled')
  # Simulate successful removal on phone followed by a new complete snapshot.
  with self.s.db() as c:c.execute("UPDATE calendar_entries SET device_sync='removed' WHERE id=?",(slot['entry_id'],))
  self.s.planner.context(self.a,{**self.context,'events':[]});after=self.slots()[0]
  self.assertEqual(after['entry_id'],slot['entry_id']);self.assertEqual(after['state'],'scheduled');self.assertEqual(self.s.calendar.entries(self.a)['entries'][0]['status'],'ready')
if __name__=='__main__':unittest.main()
