import io
import json
import pathlib
import sys
import tempfile
import unittest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / 'server'))
from assistant_brain import AssistantBrain
from personal_os import Store


class ConversationContextTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.store = Store(pathlib.Path(self.tmp.name) / 'state')
        self.owner, self.other = 'device-a', 'device-b'
        with self.store.db() as c:
            c.executemany('INSERT INTO devices VALUES(?,?)', [(self.owner, 'A'), (self.other, 'B')])
        self.session = self.store.sessions.create(self.owner)['id']

    def row(self, text, session=None, owner=None, complete=False):
        owner = owner or self.owner
        body = {'text': text, 'time_zone': 'Asia/Hong_Kong'}
        if session:
            state = self.store.sessions.state(owner, session)
            body.update(session_id=session, reply_to=state['task_id'])
        ident = self.store.submit(body, owner)
        if complete:
            self.store.update(ident, state='completed', result='回执：' + text)
            if session:
                with self.store.db() as c:
                    c.execute("UPDATE assistant_sessions SET status='completed' WHERE id=?", (session,))
        return self.store.task(ident)

    def data(self, value):
        with self.store.db() as c:
            c.execute('UPDATE assistant_sessions SET data=? WHERE id=?',
                      (json.dumps(value, ensure_ascii=False), self.session))

    def test_session_history_keeps_current_dialogue_and_excludes_other_sessions(self):
        first = self.row('本次餐厅候选', self.session, complete=True)
        other_session = self.store.sessions.create(self.owner)['id']
        self.row('同一手机的另一件私事', other_session, complete=True)
        foreign_session = self.store.sessions.create(self.other)['id']
        self.row('其他手机内容', foreign_session, owner=self.other, complete=True)
        self.row('旧版无会话任务', complete=True)
        current = self.row('第二家如何？', self.session)
        history = self.store.brain.history(current)
        self.assertEqual([item['id'] for item in history], [first['id']])
        self.assertEqual(self.store.brain.conversation_context(current)['recent_turns'][0]['text'], '本次餐厅候选')

    def test_legacy_history_still_reads_only_own_device(self):
        own = self.row('本机历史', complete=True)
        self.row('别人的历史', owner=self.other, complete=True)
        current = self.row('刚才说了什么？')
        self.assertEqual([item['id'] for item in self.store.brain.history(current)], [own['id']])
        self.assertEqual(self.store.brain.conversation_context(current), {'available': False})

    def test_explicit_task_history_lookup_retains_access_to_own_other_sessions(self):
        first = self.row('本次会话', self.session, complete=True)
        other_session = self.store.sessions.create(self.owner)['id']
        second = self.row('另一件待查看的历史任务', other_session, complete=True)
        self.row('别人的任务', owner=self.other, complete=True)
        current = self.row('查看历史任务', self.session)
        found = self.store.brain.execute_tool('task_history', {}, current, {})
        self.assertEqual({item['id'] for item in found['tasks']}, {first['id'], second['id']})
        self.assertEqual([item['id'] for item in self.store.brain.history(current)], [first['id']])

    def test_foreign_session_and_revoked_owner_cannot_supply_context(self):
        self.row('不应泄露的对话', self.session, complete=True)
        foreign = self.row('伪造会话指代', owner=self.other)
        body = json.loads(foreign['body'])
        foreign['body'] = json.dumps({**body, 'session_id': self.session})
        self.assertEqual(self.store.brain.history(foreign), [])
        self.assertEqual(self.store.brain.conversation_context(foreign), {'available': False})
        current = self.row('本机问题', self.session)
        with self.store.db() as c:
            c.execute('DELETE FROM devices WHERE token=?', (self.owner,))
        self.assertEqual(self.store.brain.history(current), [])
        self.assertEqual(self.store.brain.conversation_context(current), {'available': False})

    def test_context_exposes_verified_choices_not_private_action_metadata(self):
        current = self.row('哪家近一点？', self.session)
        self.data({'pending': {'type': 'places', 'query': '餐厅', 'nearby': True,
                               'choices': [{'id': 'private-place-id', 'name': '海湾小馆', 'detail': '约200米',
                                            'secret': 'hidden candidate property'},
                                           {'id': 'p2', 'name': '校园餐厅', 'detail': '约500米'}],
                               'selected': 1, 'mode': 'transit', 'digest': 'confirmation-private'},
                   'last_result': '上次已查到两家', 'shared_text': 'private shared message',
                   'draft': 'private reply draft', 'document_id': 'private document id',
                   'phone_action': {'access_token': 'private token'}, 'owner': 'private owner'})
        value = self.store.brain.conversation_context(current)
        self.assertEqual(value['choices'], [{'index': 1, 'name': '海湾小馆', 'detail': '约200米'},
                                           {'index': 2, 'name': '校园餐厅', 'detail': '约500米'}])
        self.assertEqual(value['selected_choice'], 2)
        self.assertEqual(value['mode'], 'transit')
        self.assertTrue(value['nearby'])
        encoded = json.dumps(value)
        self.assertNotIn('private', encoded)
        self.assertNotIn('digest', encoded)
        self.assertNotIn('last_result', value)

    def test_context_is_bounded_and_redacts_secret_looking_text(self):
        current = self.row('接着说', self.session)
        secret = 'api_key="sk-' + 'Z' * 28 + '"'
        self.data({'pending': {'type': 'places', 'choices': [{'name': 'A' * 1000, 'detail': '约200米'}] * 10,
                               'selected': True},
                   'last_result': secret,
                   'conversation_history': [{'text': 'text' * 1000, 'result': secret,
                                             'state': 'completed', 'owner': 'private owner'}] * 20})
        value = self.store.brain.conversation_context(current)
        self.assertEqual(len(value['choices']), 3)
        self.assertEqual(len(value['choices'][0]['name']), 100)
        self.assertNotIn('selected_choice', value)
        self.assertEqual(len(value['inherited_turns']), 6)
        self.assertEqual(len(value['inherited_turns'][0]['text']), 400)
        self.assertNotIn(secret, json.dumps(value))
        self.assertNotIn('private owner', json.dumps(value))
        self.assertIn('隐藏', value['last_result'])

    def test_general_model_receives_current_candidates_and_saved_preferences(self):
        prior = self.row('请找两家附近餐厅', self.session, complete=True)
        current = self.row('这两家哪家更适合我？', self.session)
        self.data({'pending': {'type': 'places', 'choices': [
            {'id': 'p1', 'name': '海湾小馆', 'detail': '约200米'},
            {'id': 'p2', 'name': '校园餐厅', 'detail': '约500米'}], 'mode': ''}})
        with self.store.db() as c:
            c.executemany('INSERT INTO assistant_preferences VALUES(?,?,?,?)',
                          [(self.owner, 'pref-a', '我喜欢安静的餐厅', 1),
                           (self.other, 'pref-b', '另一位用户的偏好', 1)])
        requests = []
        def transport(request, **kwargs):
            requests.append(json.loads(request.data))
            return io.BytesIO(json.dumps({'message': {'role': 'assistant',
                'content': '海湾小馆更近；安静程度还没有可靠资料。'}}).encode())
        brain = AssistantBrain(self.store, 'test-local:latest', transport)
        result = brain.run(current)
        context = json.loads(requests[0]['messages'][1]['content'])
        self.assertEqual(context['conversation_state']['choices'][0]['name'], '海湾小馆')
        self.assertNotIn('recent_turns', context['conversation_state'])
        self.assertEqual(context['preferences'][0]['fact'], '我喜欢安静的餐厅')
        payload = json.dumps(requests, ensure_ascii=False)
        self.assertNotIn('另一位用户的偏好', payload)
        self.assertIn(prior['id'], payload)
        self.assertIn('不能编造营业状态', requests[0]['messages'][0]['content'])
        self.assertEqual(result['state'], 'completed')
        self.assertEqual(len(brain.preferences(self.owner)), 1)

    def test_reply_refinement_receives_previous_draft_as_data_without_send_tools(self):
        row = self.row('再自然一点', self.session)
        body = json.loads(row['body'])
        previous = '收到，我会带上报告准时参加。'
        body['message_draft'] = json.dumps({'message': '明天15点带报告来开会', 'tone': '友好',
                                           'instruction': '再自然一点', 'previous_draft': previous})
        requests = []
        def transport(request, **kwargs):
            requests.append(json.loads(request.data))
            return io.BytesIO(json.dumps({'message': {'content': json.dumps({'draft': '好，明天三点见，我会带上报告。'})}}).encode())
        brain = AssistantBrain(self.store, 'test-local:latest', transport)
        result = brain.structured_reply({**row, 'body': json.dumps(body)})
        payload = requests[0]
        self.assertEqual(json.loads(payload['messages'][1]['content'])['previous_draft'], previous)
        self.assertNotIn(previous, payload['messages'][0]['content'])
        self.assertNotIn('tools', payload)
        self.assertFalse(json.loads(result['plan'])['sent'])
        self.assertEqual(self.store.todos.list(self.owner)['items'], [])
        with self.store.db() as c:
            self.assertEqual(c.execute('SELECT count(*) FROM calendar_entries').fetchone()[0], 0)

    def test_reply_refinement_rejects_extra_capabilities_and_unbounded_previous_draft(self):
        row = self.row('调整回复', self.session)
        body = json.loads(row['body'])
        base = {'message': '明天见', 'tone': '简洁', 'instruction': '再自然一点'}
        for changes in ({'previous_draft': 'x' * 3001}, {'previous_draft': {'send': True}},
                        {'previous_draft': '收到', 'send': 'yes'}):
            body['message_draft'] = json.dumps({**base, **changes})
            with self.subTest(changes=list(changes)), self.assertRaises(ValueError):
                self.store.brain.structured_reply({**row, 'body': json.dumps(body)})


if __name__ == '__main__':
    unittest.main()
