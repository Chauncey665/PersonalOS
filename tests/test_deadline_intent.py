import datetime,json,pathlib,sys,tempfile,unittest
sys.path.insert(0,str(pathlib.Path(__file__).resolve().parents[1]/'server'))
from personal_os import Store,Worker,task_kind
from deadline_intent import deadline_fact
from todos import TodoStore
from zoneinfo import ZoneInfo
class DeadlineIntentTests(unittest.TestCase):
 def setUp(self):
  self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup);self.s=Store(self.temp.name);self.owner='test-owner';self.now=datetime.datetime(2026,10,28,2,tzinfo=ZoneInfo('Asia/Hong_Kong')).timestamp()
  with self.s.db() as c:c.execute('INSERT INTO devices VALUES(?,?)',(self.owner,'test'))
 def test_deadline_never_becomes_calendar_or_clock_reminder(self):
  for text in ['我要在今晚之前把3101复习完','今晚之前做完作业，然后复习','今天23:59前完成3101复习','新增一个在今天晚上23.59之前要完成的：3101复习 的ddl提醒','提醒我拿文件']:
   self.assertEqual(task_kind(text),'plan',text)
 def test_explicit_interval_and_clock_reminder_are_preserved(self):
  self.assertEqual(task_kind('明天11:00到12:00安排简报会'),'calendar');self.assertEqual(task_kind('明天15:00提醒我拿快递'),'reminder')
 def test_original_failed_request_records_real_todo_without_model(self):
  text='新增一个在今天晚上23.59之前要完成的：3101复习 的ddl提醒';ident=self.s.submit({'text':text,'time_zone':'Asia/Hong_Kong'},self.owner)
  with self.s.db() as c:c.execute('UPDATE tasks SET created=? WHERE id=?',(self.now,ident))
  self.s.brain.transport=lambda *a,**k:self.fail('explicit deadline must not need model')
  Worker(self.s).execute(self.s.task(ident));items=self.s.todos.list(self.owner)['items'];self.assertEqual(len(items),1);self.assertEqual(items[0]['title'],'3101复习');self.assertEqual(items[0]['deadline'],'2026-10-28T23:59:00+08:00')
  with self.s.db() as c:self.assertEqual(c.execute('SELECT count(*) FROM calendar_entries').fetchone()[0],0)
  Worker(self.s).execute(self.s.task(ident));self.assertEqual(len(self.s.todos.list(self.owner)['items']),1)
 def test_exact_quotes_support_tonight_and_dot_clock(self):
  for quote,value in [('今天晚上23.59之前','2026-10-28T23:59:00+08:00'),('今晚之前','2026-10-28')]:
   result=TodoStore.model_items([{'title':'复习','deadline':value,'deadline_quote':quote}],quote+'要完成复习',self.now,'Asia/Hong_Kong');self.assertEqual(result[0]['deadline'],value)
 def test_false_deadline_or_invented_hour_still_rejected(self):
  for quote,value in [('今晚之前','2026-10-29'),('今晚之前','2026-10-28T09:00:00+08:00')]:
   with self.assertRaises(ValueError):TodoStore.model_items([{'title':'复习','deadline':value,'deadline_quote':quote}],quote+'完成',self.now,'Asia/Hong_Kong')
 def test_no_time_reminders_create_todos_without_a_model(self):
  for text,title in [('提醒我拿快递','拿快递'),('明天提醒继续项目','继续项目'),('Remind me to review notes','review notes')]:
   ident=self.s.submit({'text':text,'time_zone':'Asia/Hong_Kong'},self.owner)
   self.s.brain.transport=lambda *a,**k:self.fail('untimed reminder must not need model')
   Worker(self.s).execute(self.s.task(ident));item=self.s.todos.list(self.owner)['items'][-1]
   self.assertEqual(item['title'],title);self.assertIsNone(item['deadline']);self.assertEqual(self.s.task(ident)['state'],'completed')
 def test_vague_deadline_does_not_invent_clock(self):
  self.assertEqual(deadline_fact('今晚之前要做完',self.now,'Asia/Hong_Kong'),'2026-10-28')
if __name__=='__main__':unittest.main()
