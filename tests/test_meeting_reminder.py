import datetime as dt
import hashlib,json,pathlib,sys,tempfile,unittest
from unittest.mock import patch
sys.path.insert(0,str(pathlib.Path(__file__).resolve().parents[1]/'server'))
from personal_os import Store,Worker,task_kind
from calendar_journal import parse_entry,validate_zone
TEXT='明天早上十一点到十二点有help会，就是info会，info day的简报会，提醒我参加。'
NOW=dt.datetime(2026,10,27,23,12,tzinfo=validate_zone('Asia/Hong_Kong')).timestamp()
class MeetingReminderTests(unittest.TestCase):
 def test_real_utterance_routes_full_interval_and_final_corrected_title(self):
  e=parse_entry(TEXT,NOW);self.assertEqual(task_kind(TEXT),'calendar');self.assertEqual(e['status'],'ready');self.assertEqual(e['title'],'info day的简报会');self.assertEqual(e['start'],'2026-10-28T11:00:00+08:00');self.assertEqual(e['end'],'2026-10-28T12:00:00+08:00')
 def test_noon_and_explicit_midnight_remain_distinct(self):
  for text,start,end in [('安排明天上午十一点到十二点开会',11,12),('安排明天凌晨十二点到一点开会',0,1)]:
   e=parse_entry(text,NOW);self.assertEqual(e['status'],'ready');self.assertEqual(dt.datetime.fromisoformat(e['start']).hour,start);self.assertEqual(dt.datetime.fromisoformat(e['end']).hour,end)
 def test_single_reminders_and_negated_meetings_never_become_event_writes(self):
  self.assertEqual(task_kind('明天上午十一点提醒我参加简报会'),'reminder')
  self.assertEqual(task_kind('不要安排明天早上十一点到十二点开会，提醒我参加。'),'plan')
 def test_two_actual_activities_still_need_clarification(self):
  self.assertEqual(parse_entry('明天早上十一点到十二点就是开会，健身，提醒我参加。',NOW)['status'],'needs_details')
 def test_persistent_interval_and_start_notification_are_idempotent(self):
  with tempfile.TemporaryDirectory() as tmp,patch('calendar_journal.time.time',return_value=NOW):
   s=Store(pathlib.Path(tmp));owner=hashlib.sha256(b'synthetic-owner').hexdigest()
   with s.db() as c:c.execute('INSERT INTO devices VALUES(?,?)',(owner,'fixture'))
   ident=s.submit({'text':TEXT,'time_zone':'Asia/Hong_Kong'},owner)
   with s.db() as c:c.execute('UPDATE tasks SET created=? WHERE id=?',(NOW,ident))
   worker=Worker(s);worker.execute(s.task(ident));worker.execute(s.task(ident))
   row=s.task(ident);self.assertEqual(row['state'],'scheduled');self.assertEqual(row['due'],dt.datetime.fromisoformat('2026-10-28T11:00:00+08:00').timestamp());entry=json.loads(row['result'])['calendar_entry'];self.assertEqual(entry['reminder_at'],entry['start'])
   with s.db() as c:self.assertEqual(c.execute('SELECT count(*) FROM calendar_entries WHERE id=?',(ident,)).fetchone()[0],1)
   s.process_reminders(now=row['due']+1);s.process_reminders(now=row['due']+2);self.assertEqual([a['event'] for a in s.audits(ident)].count('reminder_due'),1)
if __name__=='__main__':unittest.main()
