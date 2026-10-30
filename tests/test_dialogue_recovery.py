import contextlib
import io
import json
import unittest

import test_dialogue_engine as fixtures
from personal_os import Store, Worker
from mac_actions import MacActions
from todos import TodoError


class SimulatedPowerLoss(BaseException):
    pass


class DialogueRecoveryTests(unittest.TestCase):
    # Reuse fixture helpers without inheriting/re-running its test methods.
    setUp = fixtures.DialogueTests.setUp
    turn = fixtures.DialogueTests.turn
    app = fixtures.DialogueTests.app
    delegate = fixtures.DialogueTests.delegate
    search = fixtures.DialogueTests.search
    places = fixtures.DialogueTests.places

    def restart(self):
        self.s = Store(self.root / 'state')
        self.s.mac = MacActions(self.s, lambda x: self.calls.append(x) or {'accepted': True}, self.root / 'Inbox')

    def workflow(self):
        with self.s.db() as c:
            row = c.execute('SELECT id FROM assistant_workflows ORDER BY updated DESC LIMIT 1').fetchone()
        return self.s.sessions.engine.load(row['id'])

    def tick(self):
        self.s.sessions.engine.tick(Worker(self.s).execute_legacy)
        return self.s.sessions.state(self.owner, self.session)

    def organize(self, name):
        folder = self.root / name
        folder.mkdir()
        path = folder / 'sample.txt'
        path.write_text('fixture')
        with self.s.db() as c:
            c.execute('INSERT INTO roots VALUES(?)', (str(folder),))
        self.s.sessions.classify = lambda *a: {'kind': 'organize', 'device': 'mini', 'folder': 'home',
                                              'query': name, 'instruction': ''}
        text = '整理' + name + '文件夹'
        ident, state = self.turn(text, [self.delegate(text)])
        return ident, state, path

    def test_power_loss_before_claim_resumes_existing_queued_step_once(self):
        original = self.s.db
        class Connection:
            def __init__(self, connection): self.connection = connection
            def __getattr__(self, name): return getattr(self.connection, name)
            def execute(self, statement, *args):
                if statement.startswith("UPDATE tasks SET state='running' WHERE id=? AND state='queued'"):
                    raise SimulatedPowerLoss()
                return self.connection.execute(statement, *args)
        @contextlib.contextmanager
        def crash_before_claim():
            with original() as connection:
                yield Connection(connection)
        self.s.db = crash_before_claim
        with self.assertRaises(SimulatedPowerLoss):
            self.turn('在Mini打开Safari', [self.app('safari', '在Mini打开Safari')])
        self.s.db = original
        workflow = self.workflow()
        child_task = workflow['data']['steps'][0]['task']
        self.assertEqual(workflow['data']['steps'][0]['state'], 'queued')
        self.assertEqual(self.s.task(child_task)['state'], 'queued')
        self.assertEqual(self.calls, [])
        self.restart()
        state = self.tick()
        self.assertEqual(state['status'], 'completed')
        self.assertEqual([call['app'] for call in self.calls], ['safari'])
        self.assertEqual(self.workflow()['data']['steps'][0]['task'], child_task)
        self.tick()
        with self.s.db() as c:
            self.assertEqual(c.execute('SELECT count(*) FROM tasks').fetchone()[0], 2)
            self.assertEqual(c.execute('SELECT count(*) FROM mac_receipts').fetchone()[0], 1)
        self.assertEqual(len(self.calls), 1)

    def test_interrupted_running_step_is_not_replayed(self):
        self.s.sessions.engine.execute_step = lambda *a: (_ for _ in ()).throw(SimulatedPowerLoss())
        with self.assertRaises(SimulatedPowerLoss):
            self.turn('在Mini打开Safari，然后打开备忘录',
                      [self.app('safari', '在Mini打开Safari'), self.app('notes', '然后打开备忘录')])
        workflow = self.workflow()
        child_task = workflow['data']['steps'][0]['task']
        self.assertEqual(self.s.task(child_task)['state'], 'running')
        self.restart()
        state = self.tick()
        self.assertEqual(state['status'], 'failed')
        self.assertEqual(self.s.task(child_task)['state'], 'needs_review')
        self.assertIn('没有自动重做', self.s.task(child_task)['result'])
        self.assertEqual(self.calls, [])
        self.tick()
        self.assertEqual(self.calls, [])

    def test_old_workflow_ui_approval_cannot_approve_new_target(self):
        old, state, old_path = self.organize('first-box')
        self.assertEqual(state['status'], 'confirmation')
        new, state, new_path = self.organize('second-box')
        self.assertEqual(state['status'], 'confirmation')
        with self.assertRaises(TodoError):
            self.s.act(old, 'approve')
        with self.assertRaises(TodoError):
            self.s.act(old, 'undo')
        self.assertTrue(old_path.exists())
        self.assertTrue(new_path.exists())
        self.s.act(new, 'approve')
        state = self.tick()
        self.assertEqual(state['status'], 'completed')
        self.assertTrue(old_path.exists())
        self.assertFalse(new_path.exists())

    def test_phone_receipt_stays_processing_until_queued_continuation_advances(self):
        _, state = self.turn('找附近餐厅，去第二家，公交过去', [self.search('找附近餐厅'),
            {'kind': 'select_place', 'choice': 2, 'mode': 'transit', 'source_quote': '去第二家，公交过去'}])
        self.s.sessions.receipt(self.owner, self.session,
                               {'action_id': state['phone_action']['id'], 'result': self.places()})
        state = self.s.sessions.state(self.owner, self.session)
        self.assertEqual(state['status'], 'processing')
        self.assertFalse(state['can_continue'])
        self.assertIsNone(state['phone_action'])
        state = self.tick()
        self.assertEqual(state['phone_action']['place_id'], 'place-b')
        self.s.sessions.receipt(self.owner, self.session,
                               {'action_id': state['phone_action']['id'], 'result': {'accepted': True}})
        state = self.s.sessions.state(self.owner, self.session)
        self.assertEqual(state['status'], 'processing')
        self.assertFalse(state['can_continue'])
        state = self.tick()
        self.assertEqual(state['status'], 'completed')
        self.assertIn('2/2', state['speech'])

    def test_single_search_only_asks_for_choice_after_worker_consumes_receipt(self):
        _, state = self.turn('找附近餐厅', [self.search('找附近餐厅')])
        self.s.sessions.receipt(self.owner, self.session,
                               {'action_id': state['phone_action']['id'], 'result': self.places()})
        self.assertEqual(self.s.sessions.state(self.owner, self.session)['status'], 'processing')
        state = self.tick()
        self.assertEqual(state['status'], 'needs_input')
        self.assertTrue(state['can_continue'])

    def test_parent_preserves_organize_preview_and_waits_after_ui_confirmation(self):
        ident, state, path = self.organize('preview-box')
        plan = json.loads(self.s.task(ident)['plan'])
        self.assertIn('root', plan)
        self.assertEqual(len(plan['moves']), 1)
        self.assertTrue(plan['workflow'])
        self.s.act(ident, 'approve')
        state = self.s.sessions.state(self.owner, self.session)
        self.assertEqual(state['status'], 'processing')
        self.assertFalse(state['can_continue'])
        self.assertFalse(path.exists())
        self.assertEqual(self.tick()['status'], 'completed')

    def test_parent_preserves_mac_write_diff_for_existing_client(self):
        path = self.root / 'report.md'
        path.write_text('Original ending.')
        self.s.file_access.local.home = self.root
        self.s.sessions.classify = lambda *a: {'kind': 'file_search', 'device': 'mini', 'folder': 'home',
                                              'query': 'report.md', 'instruction': ''}
        self.s.brain.transport = lambda *a, **kw: io.BytesIO(json.dumps({'message': {'content': json.dumps({
            'changes': [{'index': 0, 'text': 'Short ending.'}]})}}).encode())
        ident, state = self.turn('找到report.md，然后把它结尾改简短',
                                 [self.delegate('找到report.md'), self.delegate('然后把它结尾改简短')])
        self.assertEqual(state['status'], 'confirmation')
        plan = json.loads(self.s.task(ident)['plan'])
        self.assertEqual(plan['kind'], 'mac_action')
        self.assertEqual(plan['action']['operation'], 'write_document')
        self.assertIn('Short ending.', plan['action']['preview'])
        self.assertTrue(plan['undo_available'])
        self.assertTrue(plan['action_task_id'])
        self.assertEqual(path.read_text(), 'Original ending.')


if __name__ == '__main__':
    unittest.main()
