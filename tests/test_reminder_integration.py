import datetime as dt
import json
from pathlib import Path
import sys
import tempfile
import unittest

sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'server'))
from personal_os import Store, Worker, task_kind

ANCHOR=dt.datetime.fromisoformat('2026-10-27T13:24:00+08:00').timestamp()
CONTEXT={'date':'2026-10-27','timeZone':'Asia/Hong_Kong','truncated':False,
         'range_start':'2026-10-26T16:00:00Z','range_end':'2026-10-27T16:00:00Z',
         'events':[{'title':'class one','start':'2026-10-27T04:30:00Z','end':'2026-10-27T06:20:00Z','isAllDay':False},
                   {'title':'class two','start':'2026-10-27T08:00:00Z','end':'2026-10-27T09:50:00Z','isAllDay':False}]}

class ReminderIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.store=Store(Path(self.temp.name))
        with self.store.db() as c:c.execute("INSERT INTO devices VALUES(?,?)",("test-owner","Test"))

    def run_task(self,text,now=ANCHOR+30,**values):
        body={'text':text,'time_zone':'Asia/Hong_Kong',**values}
        ident=self.store.submit(body,"test-owner")
        with self.store.db() as c: c.execute('UPDATE tasks SET created=? WHERE id=?',(ANCHOR,ident))
        self.store.process_reminders(now=now)
        if self.store.task(ident)["state"]=="queued" and task_kind(text)=="plan":Worker(self.store).execute(self.store.task(ident))
        return self.store.task(ident)

    def test_unspecified_clock_is_todo_and_never_default_interval(self):
        for text in ['提醒我下午要拿快递','下午提醒我拿快递','明天提醒继续项目']:
            row=self.run_task(text,calendar_context=json.dumps(CONTEXT))
            self.assertEqual(row['state'],'completed');self.assertIsNone(row['due'])
            with self.store.db() as c:
                self.assertTrue(c.execute('SELECT 1 FROM todos WHERE source_task=?',(row['id'],)).fetchone())
                self.assertFalse(c.execute('SELECT 1 FROM calendar_entries WHERE id=?',(row['id'],)).fetchone())

    def test_exact_natural_time_and_relative_time_do_not_need_calendar(self):
        for text,expected in [('明天下午三点提醒我拿快递','2026-10-28T15:00:00+08:00'),
                              ('下午三点提醒我拿快递','2026-10-27T15:00:00+08:00'),
                              ('十五分钟后提醒我拿快递','2026-10-27T13:39:00+08:00')]:
            with self.subTest(text=text):
                row=self.run_task(text)
                self.assertEqual(row['state'],'scheduled')
                self.assertEqual(row['due'],dt.datetime.fromisoformat(expected).timestamp())

    def test_explicit_picker_time_and_due_delivery_are_preserved(self):
        due='2026-10-27T14:40:00+08:00'
        row=self.run_task('提醒拿快递',due=due)
        self.assertEqual(row['state'],'scheduled')
        self.store.process_reminders(now=dt.datetime.fromisoformat(due).timestamp()+1)
        self.assertEqual(self.store.task(row['id'])['state'],'completed')
        self.assertEqual([a['event'] for a in self.store.audits(row['id'])].count('reminder_due'),1)

    def test_past_time_does_not_fall_back_to_another_afternoon_slot(self):
        row=self.run_task('今天上午九点提醒拿快递',calendar_context=json.dumps(CONTEXT))
        self.assertEqual(row['state'],'failed');self.assertIsNone(row['due'])

    def test_reminder_intent_cannot_execute_embedded_file_tool(self):
        for text in ['明天下午三点提醒我整理文件','Remind me tomorrow at 3 pm to organize files',
                     '下午三点提醒我查找PROJECT资料']:
            with self.subTest(text=text):self.assertEqual(task_kind(text),'reminder')

    def test_incomplete_request_remains_cancellable(self):
        row=self.run_task('提醒继续项目',due='2026-10-28T15:00:00+08:00')
        self.assertEqual(row['state'],'scheduled')
        self.store.act(row['id'],'cancel')
        self.assertEqual(self.store.task(row['id'])['state'],'cancelled')

if __name__=='__main__':unittest.main()
