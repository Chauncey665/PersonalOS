import copy,datetime as dt,json,pathlib,sys,tempfile,time,unittest,uuid
from unittest.mock import patch
sys.path.insert(0,str(pathlib.Path(__file__).resolve().parents[1]/'server'))
from personal_os import Store,Worker
from fitness import requested
from task_outcomes import describe
ZONE=dt.timezone(dt.timedelta(hours=8))
class FitnessTests(unittest.TestCase):
 def setUp(self):
  self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup);self.s=Store(pathlib.Path(self.tmp.name));self.o='fitness-user'
  with self.s.db() as c:c.executemany('INSERT INTO devices VALUES(?,?)',[(self.o,'A'),('other','B')])
  self.f=self.s.fitness;self.now=time.time()
  self.data={'captured_at':self.now,'day':dt.datetime.fromtimestamp(self.now,ZONE).date().isoformat(),'time_zone':'Asia/Hong_Kong','metrics':{'weight_kg':{'value':73,'unit':'kg','measured_at':self.now-86400,'source':'HUAWEI Health'}},'workouts':[]}
  self.profile={'goal':'增肌、体能','sport':'以前专业棒球','experience':'现在运动很少，偶尔健身','restrictions':'无','equipment':'学校健身房','training_schedule':'没有固定专项训练或比赛','adult':True}
 def task(self,text):
  row=self.s.submit({'text':text,'request_id':str(uuid.uuid4()),'time_zone':'Asia/Hong_Kong'},self.o);Worker(self.s).execute(self.s.task(row));return self.s.task(row)
 def test_scoped_summary_missing_not_zero(self):
  self.f.snapshot(self.o,self.data);a=self.f.status(self.o);self.assertEqual(a['metrics']['weight_kg']['value'],73);self.assertIn('dietary_kcal',a['missing_metrics']);self.assertFalse(self.f.status('other')['connected'])
  with self.assertRaises(ValueError):self.f.status('unknown')
 def test_no_stale_or_future_summary(self):
  self.f.snapshot(self.o,self.data);self.assertFalse(self.f.status(self.o,self.now+8000)['metrics'])
  for delta in [-700,100]:
   with self.assertRaises(ValueError):self.f.snapshot(self.o,{**self.data,'captured_at':self.now+delta})
 def test_old_measurement_not_refreshed_by_upload(self):
  self.data['metrics']['weight_kg']['measured_at']=self.now-40*86400;self.f.snapshot(self.o,self.data);self.assertNotIn('weight_kg',self.f.status(self.o)['metrics'])
 def test_values_units_and_dates_validated(self):
  for value in [float('nan'),float('inf'),True,0,700]:
   d=copy.deepcopy(self.data);d['metrics']['weight_kg']['value']=value
   with self.assertRaises(ValueError):self.f.snapshot(self.o,d)
  d=copy.deepcopy(self.data);d['metrics']['weight_kg']['unit']='lb'
  with self.assertRaises(ValueError):self.f.snapshot(self.o,d)
  with self.assertRaises(ValueError):self.f.snapshot(self.o,{**self.data,'day':'2020-01-01'})
 def test_older_upload_cannot_overwrite_newer(self):
  self.f.snapshot(self.o,self.data)
  with self.assertRaises(ValueError):self.f.snapshot(self.o,{**self.data,'captured_at':self.now-5})
 def test_clear_does_not_remove_other_owner_or_profile(self):
  self.f.snapshot(self.o,self.data);self.f.snapshot('other',self.data);self.f.profile(self.o,self.profile);self.f.clear(self.o)
  self.assertFalse(self.f.status(self.o)['connected']);self.assertTrue(self.f.status('other')['connected']);self.assertEqual(self.f.profile(self.o),self.profile)
 def test_calories_burned_never_become_food_intake(self):
  self.data['metrics']['active_kcal']={'value':1200,'unit':'kcal','measured_at':self.now,'source':'HUAWEI Health'};self.f.snapshot(self.o,self.data)
  row=self.task('今天能量摄入够不够能不能长肌肉');self.assertIn('还没有今天的饮食摄入记录',row['result']);self.assertNotIn('1200',row['result']);self.assertIn('不能确认',row['result'])
 def test_food_records_not_assumed_complete(self):
  self.data['metrics']['dietary_kcal']={'value':900,'unit':'kcal','measured_at':self.now,'source':'食物日志'};self.f.snapshot(self.o,self.data)
  row=self.task('今天吃够了吗');self.assertIn('900',row['result']);self.assertIn('已记录的部分',row['result'])
 def test_missing_profile_does_not_invent_schedule(self):
  row=self.task('我今晚练肩膀，帮我排一下下周训练');self.assertIn('先补齐',row['result']);self.assertNotIn('今晚有空',row['result']);self.assertEqual(self.s.calendar.entries(self.o)['entries'],[])
 def test_correct_dates_and_preview_no_write(self):
  p=self.f.preview(self.o,now=dt.datetime(2026,10,29,14,tzinfo=ZONE).timestamp());self.assertEqual([x['day'] for x in p['sessions']],['2026-11-02','2026-11-04','2026-11-05']);self.assertIn('2026-11-02（周一）',self.f.format_plan(p));self.assertFalse(self.s.todos.list(self.o)['items'])
 def test_commit_requires_unchanged_profile_and_adult(self):
  self.f.profile(self.o,{**self.profile,'adult':False});p=self.f.preview(self.o)
  with self.assertRaises(ValueError):self.f.commit(self.o,p['id'])
  self.f.profile(self.o,self.profile)
  with self.assertRaises(ValueError):self.f.commit(self.o,p['id'])
 def test_injury_prevents_general_plan_commit(self):
  self.f.profile(self.o,{**self.profile,'restrictions':'肩疼'});p=self.f.preview(self.o)
  with self.assertRaises(ValueError):self.f.commit(self.o,p['id'])
 def test_commit_idempotent_without_fake_calendar_success(self):
  self.f.profile(self.o,self.profile);p=self.f.preview(self.o);a=self.f.commit(self.o,p['id']);b=self.f.commit(self.o,p['id']);self.assertEqual(a['todo_ids'],b['todo_ids']);self.assertEqual(len(self.s.todos.list(self.o)['items']),3);self.assertFalse(self.s.calendar.entries(self.o)['entries']);self.assertIn('尚不能声称',a['message'])
  with self.s.db() as c:self.assertEqual(c.execute('SELECT count(*) FROM todo_schedule_windows').fetchone()[0],3)
 def test_scheduling_respects_class_and_waits_for_phone_receipt(self):
  self.f.profile(self.o,self.profile);p=self.f.preview(self.o);self.s.planner.configure(self.o,{'enabled':True})
  today=dt.datetime.fromtimestamp(time.time(),ZONE).date();start=dt.datetime.combine(today,dt.time(),ZONE);end=start+dt.timedelta(days=8)
  busy=[{'start':x['day']+'T11:00:00+08:00','end':x['day']+'T15:00:00+08:00','all_day':False} for x in p['sessions'] if x['day']<end.date().isoformat()]
  self.s.planner.context(self.o,{'captured_at':time.time(),'time_zone':'Asia/Hong_Kong','range_start':start.isoformat(),'range_end':end.isoformat(),'truncated':False,'events':busy})
  receipt=self.f.commit(self.o,p['id']);slots=self.s.planner.status(self.o)['slots'];self.assertTrue(slots)
  for slot in slots:
   if slot['start']:
    self.assertGreaterEqual(dt.datetime.fromisoformat(slot['start']).hour,15);self.assertEqual(slot['device_sync'],'pending')
  with self.s.db() as c:before=c.execute('SELECT count(*) FROM calendar_entries').fetchone()[0]
  self.f.commit(self.o,p['id'])
  with self.s.db() as c:self.assertEqual(c.execute('SELECT count(*) FROM calendar_entries').fetchone()[0],before)
 def test_future_workout_rejected_and_missing_workouts_not_no_activity(self):
  d=copy.deepcopy(self.data);d['workouts']=[{'kind':'跑步','source':'华为','start':self.now+100,'end':self.now+1000}]
  with self.assertRaises(ValueError):self.f.snapshot(self.o,d)
 def test_weather_and_explicit_calendar_not_captured_by_fitness(self):
  self.assertFalse(requested('今天训练会下雨吗'))
  self.assertIsNone(self.f.run({'owner':self.o,'body':json.dumps({'text':'安排明天15:00到16:00健身'})}))
 def test_sleep_signal_changes_tonight_draft_not_calendar(self):
  self.f.profile(self.o,self.profile);self.data['metrics']['sleep_hours']={'value':4.5,'unit':'h','measured_at':self.now-3600,'source':'华为'};self.f.snapshot(self.o,self.data)
  moment=dt.datetime.now(ZONE).replace(hour=18,minute=0).timestamp();p=self.f.preview(self.o,now=moment,include_tonight=True)
  self.assertEqual(p['sessions'][0]['title'],'今晚恢复');self.assertFalse(self.s.calendar.entries(self.o)['entries'])
 def test_tonight_and_next_week_dates_are_distinct(self):
  self.f.profile(self.o,self.profile);p=self.f.preview(self.o,now=dt.datetime(2026,10,29,18,tzinfo=ZONE).timestamp(),include_tonight=True)
  self.assertEqual(p['sessions'][0]['day'],'2026-10-29');self.assertEqual(p['sessions'][1]['day'],'2026-11-02');self.assertEqual(len(p['sessions']),4)
 def test_repeat_plan_query_reports_existing_todos(self):
  self.f.profile(self.o,self.profile);plan=self.f.preview(self.o);saved=self.f.commit(self.o,plan['id']);row=self.task('看看我的训练计划')
  self.assertEqual(json.loads(row['plan'])['todo_ids'],saved['todo_ids']);self.assertIn('已记录',json.loads(row['plan'])['spoken_reply'])
 def test_other_owner_cannot_commit_plan(self):
  self.f.profile(self.o,self.profile);p=self.f.preview(self.o)
  with self.assertRaises(ValueError):self.f.commit('other',p['id'])
 def test_skip_preserves_spacing_and_idempotency(self):
  self.f.profile(self.o,self.profile);p=self.f.preview(self.o);self.f.commit(self.o,p['id']);p=self.f.plan(self.o)
  row={'id':str(uuid.uuid4()),'owner':self.o,'created':dt.datetime.fromisoformat(p['sessions'][0]['day']+'T10:00:00+08:00').timestamp(),'body':json.dumps({'text':'今天不想练'})}
  a=self.f.skip(row);b=self.f.skip(row);self.assertEqual(a,b)
  q=self.f.plan(self.o);self.assertEqual(dt.date.fromisoformat(q['sessions'][0]['day'])-dt.date.fromisoformat(p['sessions'][0]['day']),dt.timedelta(days=1));self.assertEqual(dt.date.fromisoformat(q['sessions'][2]['day'])-dt.date.fromisoformat(q['sessions'][0]['day']),dt.timedelta(days=3))
 def test_completed_today_does_not_shift_later_sessions(self):
  self.f.profile(self.o,self.profile);p=self.f.preview(self.o);self.f.commit(self.o,p['id']);p=self.f.plan(self.o)
  with self.s.db() as c:c.execute('UPDATE todos SET completed=1 WHERE id=?',(p['sessions'][0]['todo_id'],))
  self.f.skip({'id':str(uuid.uuid4()),'owner':self.o,'created':dt.datetime.fromisoformat(p['sessions'][0]['day']+'T10:00:00+08:00').timestamp()});self.assertEqual(self.f.plan(self.o),p)
 def test_demo_known_vs_unknown(self):
  row=self.task('侧平举怎么做，给我动图');self.assertEqual(json.loads(row['plan'])['exercise_id'],'lateral_raise');row=self.task('这个动作怎么做');self.assertIn('具体动作名称',row['result'])
 def test_cardio_not_inferred_from_missing_workouts(self):
  row=self.task('今天做了有氧还能做什么无氧');self.assertIn('不能判断你没有运动',row['result']);self.assertIn('强度',row['result'])
 def test_fitness_not_steal_file_edit_or_completion(self):
  self.assertFalse(requested('修改健身文档'));self.assertIsNone(self.f.run({'owner':self.o,'body':json.dumps({'text':'把健身任务标记完成'})}))
 def test_voice_session_delegates_to_verified_handler(self):
  (self.s.home/'dialogue-engine.json').write_text('{"enabled":true}');sid=self.s.sessions.create(self.o)['id'];row=self.s.submit({'text':'今天能量摄入够不够','session_id':sid,'request_id':str(uuid.uuid4()),'time_zone':'Asia/Hong_Kong'},self.o);Worker(self.s).execute(self.s.task(row));st=self.s.sessions.state(self.o,sid);self.assertIn('资料不足',st['speech'])
if __name__=='__main__':unittest.main()
