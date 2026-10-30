import datetime
import io
import json
import pathlib
import sys
import tempfile
import unittest
from zoneinfo import ZoneInfo

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / 'server'))
from deadline_intent import deadline_capture_requested
from personal_os import Store, Worker


class DeadlineCaptureTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.s = Store(self.temp.name)
        self.owner = 'deadline-test-owner'
        self.now = datetime.datetime(2026, 10, 28, 10, tzinfo=ZoneInfo('Asia/Hong_Kong')).timestamp()
        with self.s.db() as c:
            c.execute('INSERT INTO devices VALUES(?,?)', (self.owner, 'test'))

    def row(self, text):
        ident = self.s.submit({'text': text, 'time_zone': 'Asia/Hong_Kong'}, self.owner)
        with self.s.db() as c:
            c.execute('UPDATE tasks SET created=? WHERE id=?', (self.now, ident))
        return self.s.task(ident)

    def proposal(self, deadline='2026-10-30T23:00:00+08:00'):
        return {'action': 'create', 'target_id': '', 'completed': False, 'question': '', 'items': [
            {'title': '提交升级验收报告', 'kind': 'task', 'notes': '', 'deadline': deadline,
             'deadline_quote': '本周五晚上十一点之前', 'children': []}]}

    def transport(self, proposal):
        return lambda *a, **kw: io.BytesIO(json.dumps({'message': {'content': json.dumps(proposal)}}).encode())

    def test_direct_deadline_task_does_not_require_record_keyword(self):
        for text in ('周五晚上十一点之前提交升级验收报告', '本周五晚上十一点之前提交升级验收报告',
                     '明天之前完成报告', '今晚之前做完作业', '今天23:59前完成复习',
                     'Submit the report before tomorrow', '请记录报告任务，11月15日截止'):
            with self.subTest(text=text):
                self.assertTrue(deadline_capture_requested(text))

    def test_questions_negation_explanations_and_quotes_are_not_capture_requests(self):
        cases = ['周五晚上十一点之前提交报告吗？', '要不要在周五晚上十一点之前提交报告',
                 '为什么要在周五之前完成报告', '不要在周五之前提交报告', '先别记录周五之前提交报告',
                 '解释“周五之前完成报告”的意思', '翻译：周五之前提交报告',
                 '比如周五之前完成报告', '如果周五之前完成报告就好了',
                 '帮我回复老师：周五之前提交报告', '老师说周五之前提交报告',
                 '邮件里写着周五之前提交报告', '“周五之前提交报告”',
                 '我已经在周五之前提交了报告', '记住，我喜欢在截止前完成任务',
                 'Should I submit the report before tomorrow?', 'Do not submit the report before tomorrow']
        for text in cases:
            with self.subTest(text=text):
                self.assertFalse(deadline_capture_requested(text))

    def test_explicit_record_can_capture_quoted_assignment(self):
        self.assertTrue(deadline_capture_requested('请记录老师说的任务：“本周五之前提交报告”'))

    def test_plain_instruction_without_deadline_does_not_expand_implicit_write_scope(self):
        for text in ('提交报告', '我通常在截止之前完成报告', '明天11点到12点参加会议', '截止时间是什么'):
            self.assertFalse(deadline_capture_requested(text), text)

    def test_structured_path_saves_real_todo_and_no_execution_interval(self):
        row = self.row('本周五晚上十一点之前提交升级验收报告')
        self.s.brain.transport = self.transport(self.proposal())
        Worker(self.s).execute(row)
        items = self.s.todos.list(self.owner)['items']
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]['deadline'], '2026-10-30T23:00:00+08:00')
        self.assertEqual(items[0]['title'], '提交升级验收报告')
        self.assertFalse(items[0]['completed'])
        self.assertEqual(self.s.task(row['id'])['state'], 'completed')
        with self.s.db() as c:
            self.assertEqual(c.execute('SELECT count(*) FROM calendar_entries').fetchone()[0], 0)
            self.assertEqual(c.execute("SELECT count(*) FROM tasks WHERE state='scheduled'").fetchone()[0], 0)
        Worker(self.s).execute(self.s.task(row['id']))
        self.assertEqual(len(self.s.todos.list(self.owner)['items']), 1)

    def test_tool_authorization_accepts_implicit_deadline_for_create_and_child(self):
        row = self.row('本周五晚上十一点之前提交升级验收报告')
        created = self.s.brain.execute_tool('record_todos', {'items': self.proposal()['items']}, row, {})
        self.assertEqual(created['items'][0]['deadline'], '2026-10-30T23:00:00+08:00')
        parent = self.s.todos.create(self.owner, {'request_id': 'parent', 'items': [
            {'title': '报告项目', 'kind': 'project'}]})['items'][0]
        child_row = self.row('本周五晚上十一点之前提交升级验收报告')
        child = self.s.brain.execute_tool('add_todo_children',
            {'parent_id': parent['id'], 'items': self.proposal()['items']}, child_row, {})
        self.assertEqual(child['items'][0]['parent_id'], parent['id'])

    def test_model_cannot_bypass_denied_deadline_intent_by_calling_tool(self):
        for text in ('为什么要记录本周五晚上十一点之前提交报告？',
                     '不要记录本周五晚上十一点之前提交报告',
                     '解释老师说的记录任务：本周五晚上十一点之前提交报告',
                     '帮我回复这条消息：本周五晚上十一点之前提交报告'):
            row = self.row(text)
            self.assertIsNone(self.s.brain.structured_todos(row))
            with self.assertRaises(ValueError):
                self.s.brain.execute_tool('record_todos', {'items': self.proposal()['items']}, row, {})
        self.assertEqual(self.s.todos.list(self.owner)['items'], [])

    def test_existing_deadline_quote_and_exact_time_validation_remain_enforced(self):
        row = self.row('本周五晚上十一点之前提交升级验收报告')
        self.s.brain.transport = self.transport(self.proposal('2026-10-30T09:00:00+08:00'))
        with self.assertRaises(ValueError):
            self.s.brain.execute_tool('record_todos', {'items':self.proposal('2026-10-30T09:00:00+08:00')['items']}, row, {})
        self.assertEqual(self.s.todos.list(self.owner)['items'], [])

    def test_single_explicit_deadline_uses_checked_date_without_model_arithmetic(self):
        row=self.row('周五晚上十一点之前提交升级验收报告')
        self.s.brain.transport=lambda *a,**k:(_ for _ in ()).throw(AssertionError('date arithmetic must not call model'))
        result=self.s.brain.structured_todos(row)
        self.assertEqual(result['state'],'completed')
        self.assertEqual(self.s.todos.list(self.owner)['items'][0]['deadline'],'2026-10-30T23:00:00+08:00')


if __name__ == '__main__':
    unittest.main()
