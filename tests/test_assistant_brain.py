import datetime as dt
import hashlib
import io
import json
import pathlib
import sys
import tempfile
import time
import unittest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / 'server'))
from assistant_brain import AssistantBrain, conversational_request
from personal_os import Store, Worker, task_kind


def message(content='', calls=None):
    result = {'message': {'role': 'assistant', 'content': content}}
    if calls is not None:
        result['message']['tool_calls'] = calls
    return result


def call(name, **args):
    return {'function': {'name': name, 'arguments': args}}


class BrainTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = pathlib.Path(self.temp.name).resolve()
        self.store = Store(self.root / 'state')
        self.owner, self.other = 'owned-device', 'other-device'
        with self.store.db() as c:
            c.executemany('INSERT INTO devices VALUES(?,?)', [(self.owner, 'A'), (self.other, 'B')])
        self.selected = self.root / 'selected'
        self.selected.mkdir()
        with self.store.db() as c:
            c.execute('INSERT INTO roots VALUES(?)', (str(self.selected),))
        self.requests = []

    def tearDown(self):
        self.temp.cleanup()

    def row(self, text='你好', owner=None, **body):
        ident = self.store.submit({'text': text, 'time_zone': 'Asia/Hong_Kong', **body},
                                  self.owner if owner is None else owner)
        return self.store.task(ident)

    def brain(self, replies):
        responses = iter(replies)
        def transport(request, **kwargs):
            self.assertEqual(request.full_url, 'http://127.0.0.1:11434/api/chat')
            payload = json.loads(request.data)
            self.requests.append(payload)
            return io.BytesIO(json.dumps(next(responses), ensure_ascii=False).encode())
        return AssistantBrain(self.store, 'test-local:latest', transport=transport)

    def test_multiturn_reads_actual_answers_and_never_other_owner_or_unknown_legacy(self):
        first = self.row('我们把项目命名为青岚')
        self.store.update(first['id'], state='completed', result='项目名称已确认：青岚，下一步是离线缓存')
        other = self.row('PRIVATE_OTHER_SENTINEL', owner=self.other)
        self.store.update(other['id'], state='completed', result='PRIVATE_OTHER_REPLY')
        legacy = self.row('PRIVATE_LEGACY_SENTINEL', owner='')
        self.store.update(legacy['id'], state='completed', result='private legacy')
        current = self.row('它下一步是什么？')
        outcome = self.brain([message('青岚下一步是离线缓存。')]).run(current)
        payload = json.dumps(self.requests, ensure_ascii=False)
        self.assertIn('下一步是离线缓存', payload)
        self.assertNotIn('PRIVATE_OTHER', payload)
        self.assertNotIn('PRIVATE_LEGACY', payload)
        self.assertEqual(self.requests[0]['messages'][-1]['content'], '它下一步是什么？')
        self.assertTrue(self.requests[0]['think'])
        self.assertGreater(self.requests[0]['options']['num_predict'], 500)
        self.assertEqual(outcome['state'], 'completed')

    def test_real_tool_then_feedback_in_next_model_turn_and_audit_without_thinking(self):
        brain = self.brain([message(calls=[call('device_status')]), message('已核对设备磁盘。')])
        result = brain.run(self.row('请核对磁盘可用空间'))
        self.assertEqual(len(self.requests), 2)
        feedback = self.requests[1]['messages'][-1]
        data = json.loads(feedback['content'])
        self.assertEqual(feedback['role'], 'tool')
        self.assertTrue(data['ok'])
        self.assertGreaterEqual(data['result']['free_disk_gb'], 0)
        self.assertIn('checked_at', data['result'])
        self.assertIn('brain_tool_result', [x['event'] for x in self.store.audits()])
        self.assertEqual(result['state'], 'completed')

    def test_memory_requires_explicit_verbatim_request_persists_and_forgets_only_own(self):
        brain = self.brain([])
        row = self.row('请记住：我喜欢先给结论再给步骤')
        session = {}
        saved = brain.execute_tool('remember_preference', {'fact': '我喜欢先给结论再给步骤'}, row, session)
        self.assertTrue(saved['saved'])
        reopened = Store(self.store.home)
        self.assertEqual(reopened.brain.preferences(self.owner)[0]['fact'], '我喜欢先给结论再给步骤')
        self.assertEqual(reopened.brain.preferences(self.other), [])
        for text, fact in [('你好', '你好'), ('请记住我叫青岚', '我叫别的名字')]:
            with self.assertRaises(ValueError):
                brain.execute_tool('remember_preference', {'fact': fact}, self.row(text), {})
        removed = brain.execute_tool('forget_preference', {'id': saved['id']}, self.row('忘记这个偏好', owner=self.other), {})
        self.assertFalse(removed['removed'])
        self.assertTrue(brain.preferences(self.owner))
        brain.execute_tool('forget_preference', {'id': saved['id']}, self.row('忘记这个偏好'), {})
        self.assertEqual(brain.preferences(self.owner), [])

    def test_secret_memory_is_rejected_and_file_snippet_is_hidden(self):
        brain = self.brain([])
        secret = 'api_key="sk-' + 'Z' * 28 + '"'
        with self.assertRaises(ValueError):
            brain.execute_tool('remember_preference', {'fact': secret}, self.row('请记住' + secret), {})
        (self.selected / 'project-notes.md').write_text(secret)
        found = brain.execute_tool('search_documents', {'root': str(self.selected), 'query': 'project'}, self.row('查项目'), {})
        self.assertNotIn(secret, json.dumps(found))
        self.assertIn('隐藏', found['hits'][0]['snippet'])

    def test_unknown_tools_extra_parameters_and_revoked_identity_cannot_execute(self):
        brain = self.brain([])
        row = self.row('执行命令')
        for name, args in [('run_shell', {'command': 'touch forbidden'}),
                           ('device_status', {'command': 'touch forbidden'}),
                           ('files', {'operation': 'download', 'device': 'mini', 'path': '/'})]:
            with self.assertRaises(ValueError):
                brain.execute_tool(name, args, row, {})
        with self.store.db() as c:
            c.execute('DELETE FROM devices WHERE token=?', (self.owner,))
        with self.assertRaises(ValueError):
            brain.execute_tool('task_history', {}, row, {})
        self.assertEqual(brain.history(row), [])
        self.assertFalse((self.root / 'forbidden').exists())

    def test_file_organization_is_preview_only_and_negation_cannot_preview(self):
        (self.selected / 'a.txt').write_text('keep me')
        row = self.row('帮我看看怎么整理这些文件', root=str(self.selected))
        brain = self.brain([message(calls=[call('prepare_organization')]), message('整理清单可供确认。')])
        result = brain.run(row)
        self.assertEqual(result['state'], 'awaiting_confirmation')
        self.assertEqual(len(json.loads(result['plan'])['moves']), 1)
        self.assertTrue((self.selected / 'a.txt').exists())
        self.assertFalse((self.selected / 'PersonalOS-txt').exists())
        with self.assertRaises(ValueError):
            brain.execute_tool('prepare_organization', {}, self.row('不要整理文件', root=str(self.selected)), {})

    def test_tool_failure_is_reported_back_and_fake_mutation_claim_is_not_receipt(self):
        row = self.row('帮我看看资料')
        brain = self.brain([message(calls=[call('search_documents', root='/not-authorized', query='notes')]),
                            message('已经删除所有文件，已安排明天的会议。')])
        result = brain.run(row)
        data = json.loads(self.requests[1]['messages'][-1]['content'])
        self.assertFalse(data['ok'])
        self.assertIn('尚未执行', result['result'])
        self.assertNotIn('已经删除', result['result'])
        self.assertNotIn('已安排', result['result'])

    def test_memory_success_does_not_authorize_unrelated_file_or_calendar_claim(self):
        row = self.row('请记住我喜欢简短回答')
        brain = self.brain([message(calls=[call('remember_preference', fact='我喜欢简短回答')]),
                            message('已记住。已删除文件，已安排课程。')])
        result = brain.run(row)
        self.assertIn('已记住', result['result'])
        self.assertNotIn('已删除', result['result'])
        self.assertNotIn('已安排', result['result'])

    def test_original_pending_reminder_can_only_use_current_explicit_time(self):
        original = self.row('明天提醒我继续项目')
        self.store.update(original['id'],state='needs_details',result='旧版待补充提醒')
        self.assertEqual(self.store.task(original['id'])['state'], 'needs_details')
        tomorrow = dt.datetime.now(dt.timezone(dt.timedelta(hours=8))).date() + dt.timedelta(days=1)
        due = dt.datetime.combine(tomorrow, dt.time(15, 50), dt.timezone(dt.timedelta(hours=8)))
        current = self.row('给刚才那个提醒补充时间：明天下午三点五十')
        brain = self.brain([])
        args = {'task_id': original['id'], 'due': due.isoformat(), 'time_zone': 'Asia/Hong_Kong'}
        wrong = {**args, 'due': (due + dt.timedelta(hours=1)).isoformat()}
        with self.assertRaises(ValueError):
            brain.execute_tool('fill_reminder_time', wrong, current, {})
        self.assertEqual(self.store.task(original['id'])['state'], 'needs_details')
        result = brain.execute_tool('fill_reminder_time', args, current, {})
        self.assertEqual(result['id'], original['id'])
        self.assertEqual(result['state'], 'scheduled')
        self.assertFalse(result['new_task_created'])
        self.assertEqual(self.store.task(original['id'])['due'], due.timestamp())
        self.assertEqual(len(self.store.tasks()), 2)

    def test_reminder_owner_ambiguity_and_guessed_time_fail_closed(self):
        original = self.row('明天提醒我继续项目', owner=self.other)
        self.store.update(original['id'],state='needs_details',result='旧版待补充提醒')
        current = self.row('补充：明天下午三点')
        with self.assertRaises(ValueError):
            self.brain([]).execute_tool('fill_reminder_time',
                {'task_id': original['id'], 'due': '2030-01-01T15:00:00+08:00', 'time_zone': 'Asia/Hong_Kong'}, current, {})
        self.assertEqual(self.store.task(original['id'])['state'], 'needs_details')

    def test_questions_and_followups_do_not_become_mutation_keywords(self):
        for text in ['提醒是什么？', '不要提醒我了', '帮我分析PROJECT下一步', '刚才那个提醒补成明天下午三点', '我要你解释日历同步']:
            self.assertEqual(task_kind(text), 'plan', text)
        for text, kind in [('明天下午三点提醒我整理文件', 'reminder'), ('设备状态', 'status'),
                           ('明天15:00到16:00，安排测试', 'calendar'), ('整理选定文件', 'organize')]:
            self.assertEqual(task_kind(text), kind, text)

    def test_tool_loop_is_bounded(self):
        row = self.row('请核对设备运行情况')
        repeated = message(calls=[call('device_status')])
        brain = self.brain([repeated, repeated, repeated, message('已核对设备状态。')])
        result = brain.run(row)
        self.assertEqual(len(self.requests), 4)
        self.assertFalse(self.requests[-1]['think'])
        self.assertNotIn('tools', self.requests[-1])
        self.assertEqual(result['state'], 'completed')

    def test_explicit_memory_receipt_is_fast_and_idempotent(self):
        brain=self.brain([])
        row=self.row('请记住我喜欢简短回答')
        for _ in range(2):self.assertIn('已记住',brain.run(row)['result'])
        self.assertEqual(len(brain.preferences(self.owner)),1)
        self.assertEqual(self.requests,[])

    def test_structured_project_is_persisted_with_mixed_deadlines(self):
        proposal={'action':'create','target_id':'','completed':False,'question':'','items':[
            {'title':'发布','kind':'project','deadline':'2026-11-15','deadline_quote':'11月15日截止','notes':'','children':[
                {'title':'设计','kind':'task','deadline':'2026-11-10','deadline_quote':'11月10日截止','notes':''},
                {'title':'测试','kind':'task','deadline':None,'deadline_quote':'','notes':''}]}]}
        result=self.brain([message(json.dumps(proposal))]).run(self.row('记录发布项目，11月15日截止，拆分设计11月10日截止，测试不定日期'))
        rows=self.store.todos.list(self.owner)['items']
        self.assertEqual(len(rows),3)
        self.assertIsNone(rows[2]['deadline'])
        self.assertIn('已记录',result['result'])
        self.assertIn('format',self.requests[0])
        with self.store.db() as c:self.assertEqual(c.execute('SELECT count(*) FROM calendar_entries').fetchone()[0],0)

    def test_named_existing_parent_prevents_model_duplicate_project(self):
        parent=self.store.todos.create(self.owner,{'request_id':'seed','items':[{'title':'青岚发布','kind':'project'}]})['items'][0]
        proposal={'action':'create','target_id':'','completed':False,'question':'','items':[
            {'title':'文档','kind':'task','deadline':None,'deadline_quote':'','notes':'','children':[]}]}
        self.brain([message(json.dumps(proposal))]).run(self.row('给青岚发布项目增加一个小任务：文档'))
        rows=self.store.todos.list(self.owner)['items']
        self.assertEqual(len(rows),2)
        self.assertEqual(rows[1]['parent_id'],parent['id'])
        self.assertEqual(rows[0]['total_count'],1)

    def test_structured_unproven_deadline_rolls_back_before_any_create(self):
        proposal={'action':'create','target_id':'','completed':False,'question':'','items':[
            {'title':'报告','kind':'task','deadline':'2026-11-11','deadline_quote':'11月10日截止','notes':'','children':[]}]}
        with self.assertRaises(ValueError):
            self.brain([message(json.dumps(proposal))]).run(self.row('记录报告任务，11月10日截止'))
        self.assertEqual(self.store.todos.list(self.owner)['items'],[])

    def test_negative_memory_request_cannot_persist_even_with_tool_call(self):
        brain=self.brain([message(calls=[call('remember_preference',fact='我喜欢晚起')]),message('没有保存偏好。')])
        result=brain.run(self.row('不要记住我喜欢晚起'))
        self.assertEqual(brain.preferences(self.owner),[])
        self.assertEqual(result['state'],'completed')

    def test_clarification_is_audited_as_needs_details_not_completed(self):
        row=self.row('记录项目任务，具体是哪一个稍后补充')
        proposal={'action':'clarify','target_id':'','completed':False,'items':[],'question':'要记录哪个项目？'}
        self.store.brain=self.brain([message(json.dumps(proposal))])
        Worker(self.store).execute(row)
        self.assertEqual(self.store.task(row['id'])['state'],'needs_details')
        events=[item['event'] for item in self.store.audits(row['id'])]
        self.assertIn('needs_details',events)
        self.assertNotIn('completed',events)
        self.assertEqual(task_kind('明天下午三点提醒我检查DDL'),'reminder')


if __name__ == '__main__':
    unittest.main()
