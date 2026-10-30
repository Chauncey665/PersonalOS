import datetime as dt,json,pathlib,sys,tempfile,time,unittest,uuid
sys.path.insert(0,str(pathlib.Path(__file__).resolve().parents[1]/'server'))
from personal_os import Store
from auto_planner import instant,overlaps
from fitness_week import select_week
ZONE=dt.timezone(dt.timedelta(hours=8))
class WeekTests(unittest.TestCase):
 def setUp(self):
  self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup);self.s=Store(pathlib.Path(self.tmp.name));self.o='week-owner';self.now=time.time()
  with self.s.db() as c:c.execute('INSERT INTO devices VALUES(?,?)',(self.o,'test'))
  self.s.fitness.profile(self.o,{'goal':'增肌、体能','sport':'曾专业棒球','experience':'近期运动很少','restrictions':'无','equipment':'健身房','training_schedule':'没有固定比赛','adult':True})
  self.s.planner.configure(self.o,{'enabled':True})
  self.start=dt.datetime.now(ZONE).replace(hour=0,minute=0,second=0,microsecond=0)
  self.ctx={'captured_at':time.time(),'time_zone':'Asia/Hong_Kong','range_start':self.start.isoformat(),'range_end':(self.start+dt.timedelta(days=8)).isoformat(),'truncated':False,'events':[]}
  self.s.planner.context(self.o,self.ctx)
 def plan(self):return self.s.fitness.preview(self.o,rolling=True)
 def test_seven_days_spacing_and_notes_receipt(self):
  p=self.plan();strength=[x for x in p['sessions'] if x['key'].startswith('strength')]
  self.assertEqual(len(strength),2);self.assertGreaterEqual(instant(strength[1]['slot_start'])-instant(strength[0]['slot_end']),48*3600)
  for x in p['sessions']:self.assertTrue(p['period_start']<=x['day']<=p['period_end'])
  a=self.s.fitness.commit(self.o,p['id']);self.s.fitness.commit(self.o,p['id']);self.assertEqual(len(self.s.todos.list(self.o)['items']),len(p['sessions']))
  events=self.s.calendar.entries(self.o)['entries'];self.assertEqual(len(events),len(p['sessions']))
  for e in events:
   self.assertEqual(e['device_sync'],'pending')
   if '全身力量' in e['title']:self.assertIn('https://www.mayoclinic.org',e['original_text']);self.assertIn('2组',e['original_text'])
 def test_full_class_day_and_meals_are_not_used(self):
  day=self.start+dt.timedelta(days=1);self.ctx['events']=[{'start':day.replace(hour=10).isoformat(),'end':day.replace(hour=23).isoformat(),'all_day':False}];self.s.planner.context(self.o,self.ctx)
  p=self.plan();self.assertNotIn(day.date().isoformat(),[x['day'] for x in p['sessions']])
  for x in p['sessions']:
   d=dt.datetime.fromisoformat(x['slot_start']);pair=(instant(x['slot_start']),instant(x['slot_end']))
   for h in (13,19):self.assertFalse(overlaps(pair,(d.replace(hour=h,minute=0).timestamp(),d.replace(hour=h,minute=45).timestamp())))
 def test_stale_snapshot_cannot_create_plan(self):
  with self.s.db() as c:c.execute('UPDATE planning_context SET received=?',(time.time()-901,))
  with self.assertRaisesRegex(ValueError,'刷新'):self.plan()
  self.assertFalse(self.s.todos.list(self.o)['items'])
 def test_deadline_pressure_changes_day_choice(self):
  p=self.plan();day=[x['day'] for x in p['sessions'] if x['key']=='strength-0'][0]
  with self.s.db() as c:
   c.execute("INSERT INTO todos(id,owner,kind,title,notes,deadline,completed,revision,created,updated,source_task) VALUES(?,?,'task','Exam','',?,0,1,?,?,'')",(str(uuid.uuid4()),self.o,day,time.time(),time.time()))
  q=self.plan();self.assertNotIn(day,[x['day'] for x in q['sessions'] if x['key'].startswith('strength')])
 def test_changed_calendar_cannot_force_conflicting_slot(self):
  p=self.plan();self.ctx['events']=[{'start':x['slot_start'],'end':x['slot_end'],'all_day':False} for x in p['sessions']];self.ctx['captured_at']=time.time();self.s.planner.context(self.o,self.ctx)
  self.s.fitness.commit(self.o,p['id']);self.assertFalse(self.s.calendar.entries(self.o)['entries'])
 def test_direct_request_auto_commits_but_preview_does_not(self):
  for text,expected in [('给我未来一周训练计划，先别同步到日历',False),('帮我安排未来一周训练计划，结合课表同步到日历，动作演示找教程',True)]:
   r=self.s.fitness.run({'id':str(uuid.uuid4()),'created':time.time(),'owner':self.o,'body':json.dumps({'text':text})});self.assertEqual(self.s.fitness.plan(self.o)['committed'],expected);self.assertNotIn('请告诉我具体动作',r['result'])
 def test_explicit_tutorial_not_fake_native_demo(self):
  r=self.s.fitness.run({'id':'tutorial','created':time.time(),'owner':self.o,'body':json.dumps({'text':'腿举怎么做，给我视频教程'})});self.assertIn('Mayo Clinic',r['result']);self.assertIn('tutorial_url',json.loads(r['plan']));self.assertNotIn('exercise_id',json.loads(r['plan']))
 def pending(self):
  with self.s.db() as c:c.execute('UPDATE planning_context SET received=?',(time.time()-901,))
  ident=self.s.submit({'text':'安排未来一周训练并同步到日历','request_id':str(uuid.uuid4())},self.o)
  self.s.update(ident,**self.s.fitness.run(self.s.task(ident)));self.assertEqual(self.s.task(ident)['state'],'needs_details');return ident
 def test_pending_survives_restart_and_resumes_once(self):
  ident=self.pending();self.s=Store(pathlib.Path(self.tmp.name));self.ctx['captured_at']=time.time();self.s.planner.context(self.o,self.ctx)
  self.s.fitness.resume_pending(self.o);first=self.s.fitness.plan(self.o);self.assertTrue(first['committed']);self.assertEqual(self.s.task(ident)['state'],'completed')
  self.s.fitness.resume_pending(self.o);self.assertEqual(len(self.s.todos.list(self.o)['items']),len(first['sessions']))
 def test_cancelled_pending_does_not_resume(self):
  ident=self.pending();self.s.update(ident,state='cancelled');self.ctx['captured_at']=time.time();self.s.planner.context(self.o,self.ctx);self.s.fitness.resume_pending(self.o)
  self.assertFalse(self.s.todos.list(self.o)['items'])
 def test_expired_pending_does_not_write(self):
  ident=self.pending()
  with self.s.db() as c:c.execute('UPDATE tasks SET created=? WHERE id=?',(time.time()-86401,ident))
  self.s.fitness.resume_pending(self.o);self.assertEqual(self.s.task(ident)['state'],'failed');self.assertFalse(self.s.todos.list(self.o)['items'])
 def test_other_device_refresh_does_not_resume(self):
  ident=self.pending();self.s.fitness.resume_pending('other');self.assertEqual(self.s.task(ident)['state'],'needs_details');self.assertFalse(self.s.todos.list(self.o)['items'])
if __name__=='__main__':unittest.main()
