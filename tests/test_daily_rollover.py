import datetime as dt,json,pathlib,sys,tempfile,time,unittest,uuid
from unittest.mock import patch
sys.path.insert(0,str(pathlib.Path(__file__).resolve().parents[1]/'server'))
from personal_os import Store
from daily_rollover import DailyRollover
from auto_planner import instant,overlaps
from push import PushDispatcher,PushResult
START=instant('2026-10-30T12:00:00+08:00');NEXT=instant('2026-10-31T01:06:00+08:00')
class DailyTests(unittest.TestCase):
 def setUp(self):
  self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup);self.now=START;self.clock=patch('time.time',side_effect=lambda:self.now);self.clock.start();self.addCleanup(self.clock.stop)
  self.s=Store(pathlib.Path(self.tmp.name));self.o='daily-owner';self.other='other';self.d=DailyRollover(self.s)
  with self.s.db() as c:c.executemany('INSERT INTO devices VALUES(?,?)',[(self.o,'A'),(self.other,'B')]);c.executemany('INSERT INTO push_devices VALUES(?,?,?,?,1)',[(self.o,'a'*64,'sandbox',START),(self.other,'b'*64,'sandbox',START)])
  self.s.planner.configure(self.o,{'enabled':True});self.d.configure(self.o,True)
 def add(self,title='unfinished',day='2026-10-30',complete=False,manual=False,owner=None,deadline=None):
  owner=owner or self.o;item=self.s.todos.create(owner,{'request_id':uuid.uuid4().hex,'items':[{'title':title,'deadline':deadline}]})['items'][0];eid=str(uuid.uuid4())
  with self.s.db() as c:
   c.execute('UPDATE todos SET completed=? WHERE id=?',(int(complete),item['id']))
   c.execute("INSERT INTO calendar_entries(id,owner,kind,title,start,end,time_zone,all_day,status,revision,question,device_sync,sync_error,original_text,created,updated,last_op,last_hash,previous_revision) VALUES(?,?,?,?,?,?,'Asia/Hong_Kong',0,'ready',1,'','written','','',?,? ,?,'',0)",(eid,owner,'done' if complete else 'plan',title,day+'T16:00:00+08:00',day+'T16:45:00+08:00',START,START,'resolve' if manual else 'auto_plan'))
   c.execute('INSERT INTO auto_schedule VALUES(?,?,?,?,?,?)',(owner,item['id'],eid,'manual' if manual else 'started','',START))
  return item['id'],eid
 def context(self):
  start=dt.datetime.fromtimestamp(self.now,dt.timezone(dt.timedelta(hours=8))).replace(hour=0,minute=0,second=0,microsecond=0)
  return {'captured_at':self.now,'time_zone':'Asia/Hong_Kong','range_start':start.isoformat(),'range_end':(start+dt.timedelta(days=8)).isoformat(),'truncated':False,'events':[{'start':start.replace(hour=11).isoformat(),'end':start.replace(hour=12).isoformat(),'all_day':False}]}
 def advance(self):self.now=NEXT;return self.s.planner.context(self.o,self.context())
 def ack(self):
  for x in self.s.calendar.entries(self.o)['entries']:
   if x['device_sync']=='pending':self.s.calendar.ack(x['id'],self.o,{'revision':str(x['revision']),'status':'removed' if x['status']=='cancelled' else 'written'})
 def test_optin_first_run_tomorrow_and_protected_records(self):
  a,e=self.add();done,_=self.add('done',complete=True);future,_=self.add('future','2026-11-02');manual,_=self.add('manual',manual=True);foreign,_=self.add('other',owner=self.other)
  self.d.prepare(self.o,START)
  with self.s.db() as c:self.assertEqual(c.execute('SELECT count(*) FROM daily_rollover_runs').fetchone()[0],0)
  self.advance()
  with self.s.db() as c:
   ids=[x[0] for x in c.execute('SELECT todo_id FROM daily_rollover_items')];self.assertEqual(ids,[a])
  slot=next(x for x in self.s.planner.status(self.o)['slots'] if x['todo_id']==a);self.assertEqual(slot['entry_id'],e);self.assertTrue(slot['start'].startswith('2026-10-31'));self.assertEqual(slot['device_sync'],'pending')
 def test_stale_snapshot_waits_and_restarts_do_not_duplicate(self):
  a,e=self.add();self.now=NEXT;self.s.planner.reconcile(self.o)
  self.assertEqual(self.s.planner.status(self.o)['slots'][0]['state'],'reschedule')
  self.s=Store(pathlib.Path(self.tmp.name));self.d=DailyRollover(self.s);self.s.planner.context(self.o,self.context());self.s.planner.reconcile(self.o)
  with self.s.db() as c:self.assertEqual(c.execute('SELECT count(*) FROM daily_rollover_runs').fetchone()[0],1);self.assertEqual(c.execute('SELECT count(*) FROM tasks').fetchone()[0],1)
 def test_digest_waits_for_current_receipt_is_private_once_and_morning(self):
  self.add();self.advance();self.d.finalize(self.o,self.now)
  with self.s.db() as c:self.assertEqual(c.execute('SELECT count(*) FROM notification_outbox').fetchone()[0],0)
  self.ack();self.d.finalize(self.o,self.now);self.d.finalize(self.o,self.now)
  with self.s.db() as c:
   out=c.execute('SELECT * FROM notification_outbox').fetchall();self.assertEqual(len(out),1);self.assertEqual(out[0]['device'],self.o);self.assertEqual(out[0]['next_attempt'],instant('2026-10-31T10:30:00+08:00'))
   task=c.execute('SELECT result,plan FROM tasks').fetchone();self.assertIn('1项已同步',task['result']);self.assertIn('unfinished',json.loads(task['plan'])['daily_rollover_summary'])
 def test_expired_deadline_remains_but_work_is_scheduled_as_overdue(self):
  a,e=self.add(deadline='2026-10-30');self.advance();slot=self.s.planner.status(self.o)['slots'][0];self.assertEqual(slot['state'],'scheduled');self.assertIn('补做',slot['reason'])
  with self.s.db() as c:self.assertEqual(c.execute('SELECT deadline FROM todos WHERE id=?',(a,)).fetchone()[0],'2026-10-30')
 def test_no_changes_no_digest_and_disable_stops_future_runs(self):
  self.add(complete=True);self.advance();self.d.finalize(self.o,self.now)
  with self.s.db() as c:self.assertEqual(c.execute('SELECT count(*) FROM tasks').fetchone()[0],0);self.assertEqual(c.execute('SELECT count(*) FROM notification_outbox').fetchone()[0],0)
  self.d.configure(self.o,False);self.add('old');self.now+=86400;self.s.planner.reconcile(self.o)
  with self.s.db() as c:self.assertEqual(c.execute('SELECT count(*) FROM daily_rollover_runs').fetchone()[0],1)
 def test_overnight_block_not_moved_before_day_finishes(self):
  a,e=self.add()
  with self.s.db() as c:c.execute("UPDATE calendar_entries SET start='2026-10-30T23:45:00+08:00',end='2026-10-31T00:30:00+08:00' WHERE id=?",(e,))
  self.now=instant('2026-10-31T00:31:00+08:00');self.d.prepare(self.o,self.now)
  with self.s.db() as c:self.assertEqual(c.execute('SELECT count(*) FROM daily_rollover_items').fetchone()[0],0)
  self.advance()
  with self.s.db() as c:self.assertEqual(c.execute('SELECT count(*) FROM daily_rollover_items').fetchone()[0],1)
 def test_training_series_shifts_together_without_extra_volume(self):
  a,e=self.add('strength A');b,f=self.add('strength B','2026-11-03')
  plan={'committed':True,'sessions':[{'todo_id':a,'day':'2026-10-30','slot_start':'2026-10-30T16:00:00+08:00','slot_end':'2026-10-30T16:45:00+08:00'},{'todo_id':b,'day':'2026-11-03','slot_start':'2026-11-03T16:00:00+08:00','slot_end':'2026-11-03T16:45:00+08:00'}]}
  with self.s.db() as c:c.execute('INSERT INTO fitness_plans VALUES(?,?,?,?)',(self.o,'plan',json.dumps(plan),START))
  self.advance()
  with self.s.db() as c:q=json.loads(c.execute('SELECT payload FROM fitness_plans WHERE owner=?',(self.o,)).fetchone()[0]);self.assertEqual(len(q['sessions']),2);self.assertEqual([x['day'] for x in q['sessions']],['2026-10-31','2026-11-04'])
 def test_push_uses_verified_digest_and_no_early_dispatch(self):
  self.add();self.advance();self.ack();self.d.finalize(self.o,self.now)
  class Provider:
   def __init__(self):self.seen=[]
   def status(self):return {'push_configured':True}
   def send(self,*args,**kwargs):self.seen.append(kwargs['summary']);return PushResult('sent','accepted_by_apns')
  p=Provider();d=PushDispatcher(self.s,provider=p);self.assertEqual(d.tick(),0);self.now=instant('2026-10-31T10:31:00+08:00');self.assertEqual(d.tick(),1);self.assertIn('1项已同步',p.seen[0]);self.assertEqual(d.tick(),0)
 def test_send_time_rechecks_changed_event_and_waits_for_new_receipt(self):
  a,e=self.add();self.advance();self.ack();self.d.finalize(self.o,self.now)
  with self.s.db() as c:c.execute("UPDATE calendar_entries SET start='2026-10-31T17:00:00+08:00',end='2026-10-31T17:45:00+08:00',revision=revision+1,device_sync='pending' WHERE id=?",(e,))
  class Provider:
   def __init__(self):self.seen=[]
   def status(self):return {'push_configured':True}
   def send(self,*args,**kwargs):self.seen.append(kwargs['summary']);return PushResult('sent','accepted_by_apns')
  p=Provider();d=PushDispatcher(self.s,provider=p);self.now=instant('2026-10-31T10:31:00+08:00');self.assertEqual(d.tick(),0);self.assertFalse(p.seen)
  self.ack();self.now+=61;self.assertEqual(d.tick(),1);self.assertIn('17:00',p.seen[0])
 def test_no_space_is_explicit_and_cancellation_not_resurrected(self):
  self.add();self.now=NEXT;context=self.context();context['events']=[{'start':'2026-10-31T00:00:00+08:00','end':'2026-11-01T02:00:00+08:00','all_day':False}];self.s.planner.context(self.o,context);self.ack();self.d.finalize(self.o,self.now)
  with self.s.db() as c:r=c.execute('SELECT result FROM tasks').fetchone();self.assertIn('0项已同步',r['result']);self.assertIn('1项未排定',r['result'])
 def test_old_snapshot_after_receipt_does_not_report_false_unplaced(self):
  a,e=self.add();self.advance();self.ack();self.s.planner.reconcile(self.o)
  with self.s.db() as c:self.assertEqual(c.execute('SELECT count(*) FROM notification_outbox').fetchone()[0],0)
  slot=self.s.planner.status(self.o)['slots'][0];context=self.context();context['events'].append({'start':slot['start'],'end':slot['end'],'all_day':False,'auto_id':e});self.now+=1;context['captured_at']=self.now;self.s.planner.context(self.o,context)
  with self.s.db() as c:self.assertEqual(c.execute('SELECT count(*) FROM notification_outbox').fetchone()[0],1);self.assertIn('1项已同步',c.execute('SELECT result FROM tasks').fetchone()[0])
 def test_digest_history_and_direct_read_are_owner_private(self):
  self.add();self.advance()
  own=self.s.tasks(self.o);self.assertEqual(len(own),1);self.assertEqual(self.s.tasks(self.other),[])
  from calendar_journal import CalendarError
  with self.assertRaises(CalendarError):self.s.task(own[0]['id'],self.other)
 def test_disabled_policy_cancels_only_its_pending_digest(self):
  self.add();self.advance();self.ack();self.d.finalize(self.o,self.now);self.d.configure(self.o,False)
  with self.s.db() as c:self.assertEqual(c.execute('SELECT state FROM notification_outbox').fetchone()[0],'cancelled')
 def test_disable_before_receipt_does_not_queue_late_push(self):
  self.add();self.advance();self.d.configure(self.o,False);self.ack();self.d.finalize(self.o,self.now)
  with self.s.db() as c:self.assertEqual(c.execute('SELECT count(*) FROM notification_outbox').fetchone()[0],0)
 def test_cancelled_digest_is_not_resurrected(self):
  self.add();self.advance()
  with self.s.db() as c:c.execute("UPDATE tasks SET state='cancelled'")
  self.ack();self.d.finalize(self.o,self.now)
  with self.s.db() as c:self.assertEqual(c.execute('SELECT state FROM tasks').fetchone()[0],'cancelled');self.assertEqual(c.execute('SELECT count(*) FROM notification_outbox').fetchone()[0],0)
 def test_completed_while_waiting_is_not_moved_again_next_day(self):
  a,e=self.add();self.advance()
  with self.s.db() as c:c.execute('UPDATE todos SET completed=1,revision=revision+1 WHERE id=?',(a,))
  self.now+=86400;self.s.planner.context(self.o,self.context())
  with self.s.db() as c:self.assertEqual(c.execute('SELECT count(*) FROM daily_rollover_items').fetchone()[0],1);self.assertEqual(c.execute('SELECT day FROM daily_rollover_items').fetchone()[0],'2026-10-31')
if __name__=='__main__':unittest.main()
