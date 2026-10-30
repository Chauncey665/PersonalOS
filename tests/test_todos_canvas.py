import hashlib
import io
import json
import pathlib
import stat
import sys
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / 'server'))
from personal_os import Store, Handler, ThreadingHTTPServer, task_kind
from todos import TodoError, TodoStore
from canvas_sync import CanvasSync, BASE


class TodoTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.home = pathlib.Path(self.temp.name).resolve()
        self.store = Store(self.home)
        self.a, self.b = 'owner-a', 'owner-b'
        with self.store.db() as c:
            c.executemany('INSERT INTO devices VALUES(?,?)', [(self.a, 'A'), (self.b, 'B')])
    def tearDown(self):
        self.temp.cleanup()
    def create(self, items, request='one', parent=None, owner=None):
        body = {'items': items, 'request_id': request}
        if parent: body['parent_id'] = parent
        return self.store.todos.create(owner or self.a, body)
    def test_mixed_deadlines_are_not_execution_slots_and_survive_reopen(self):
        result = self.create([{'title': '项目', 'deadline': '2026-11-30', 'children': [
            {'title': '设计', 'deadline': '2026-11-10'},
            {'title': '实现'}, {'title': '提交', 'deadline': '2026-11-30T17:00:00+08:00'}]}])
        self.assertEqual(len(result['items']), 4)
        self.assertIsNone(result['items'][2]['deadline'])
        self.assertEqual(result['calendar_slots_created'], 0)
        with self.store.db() as c:
            self.assertEqual(c.execute('SELECT count(*) FROM calendar_entries').fetchone()[0], 0)
        reopened = Store(self.home)
        self.assertEqual(len(reopened.todos.list(self.a)['items']), 4)
        self.assertEqual(reopened.todos.list(self.b)['items'], [])
    def test_batch_retry_atomicity_and_changed_payload_conflict(self):
        first = self.create([{'title': '作业'}])
        second = self.create([{'title': '作业'}])
        self.assertEqual(first['created_ids'], second['created_ids'])
        with self.assertRaises(TodoError) as e: self.create([{'title': '另一个作业'}])
        self.assertEqual(e.exception.status, 409)
        with self.assertRaises(TodoError): self.create([{'title': '本来合法'}, {'title': ''}], request='bad')
        self.assertEqual(len(self.store.todos.list(self.a)['items']), 1)
    def test_completion_revision_owner_and_project_rollup(self):
        created = self.create([{'title': '项目', 'children': [{'title': '甲'}, {'title': '乙'}]}])['items']
        for child in created[1:]:
            with self.assertRaises(TodoError) as e: self.store.todos.update(self.b, child['id'], {'revision': 1, 'completed': True})
            self.assertEqual(e.exception.status, 404)
            saved = self.store.todos.update(self.a, child['id'], {'revision': 1, 'completed': True})
            self.assertTrue(saved['completed'])
            with self.assertRaises(TodoError): self.store.todos.update(self.a, child['id'], {'revision': 1, 'completed': False})
        parent = self.store.todos.list(self.a)['items'][0]
        self.assertEqual((parent['completed_count'], parent['total_count']), (2, 2))
        self.assertTrue(parent['completed'])
        with self.assertRaises(TodoError): self.store.todos.update(self.a, parent['id'], {'revision': 1, 'completed': False})
    def test_existing_task_can_be_split_without_duplicate_and_no_foreign_parent(self):
        parent = self.create([{'title': 'Canvas作业', 'deadline': '2026-11-01'}])['items'][0]
        with self.assertRaises(TodoError): self.create([{'title': '小任务'}], 'foreign', parent['id'], self.b)
        self.create([{'title': '准备'}, {'title': '实现'}], 'split', parent['id'])
        rows = self.store.todos.list(self.a)['items']
        self.assertEqual(len(rows), 3)
        self.assertEqual(rows[0]['id'], parent['id'])
        self.assertEqual(rows[0]['kind'], 'project')
        self.assertEqual(rows[0]['deadline'], '2026-11-01')
        self.assertTrue(all(x['deadline'] is None for x in rows[1:]))
    def test_paginated_reads_do_not_duplicate_or_drop_children(self):
        for n in range(11):
            self.create([{'title': str(n) + '-' + str(i)} for i in range(20)], 'page-' + str(n))
        page = self.store.todos.list(self.a)
        second = self.store.todos.list(self.a, page['next_cursor'])
        self.assertEqual(len(page['items']) + len(second['items']), 220)
        self.assertEqual(len({x['id'] for x in page['items'] + second['items']}), 220)
        self.assertIsNone(second['next_cursor'])
    def test_model_deadlines_require_matching_verbatim_quote_and_no_inheritance(self):
        now = __import__('datetime').datetime(2026, 10, 27, 12, tzinfo=__import__('datetime').timezone.utc).timestamp()
        text = '记录报告项目，11月10日截止；拆分阅读和写作，阅读没有ddl。'
        rows = [{'title': '报告', 'kind': 'project', 'deadline': '2026-11-10', 'deadline_quote': '11月10日截止',
                 'children': [{'title': '阅读'}, {'title': '写作'}]}]
        clean = TodoStore.model_items(rows, text, now, 'Asia/Hong_Kong')
        self.assertIsNone(clean[0]['children'][0]['deadline'])
        rows[0]['deadline'] = '2026-11-11'
        with self.assertRaises(ValueError): TodoStore.model_items(rows, text, now, 'Asia/Hong_Kong')
        self.assertEqual(task_kind(text), 'plan')
    @staticmethod
    def canvas_item(deadline='2026-10-30T15:59:59Z', completed=False):
        return {'external_id': '10:20', 'title': '作业', 'course': 'TEST1000', 'deadline': deadline, 'completed': completed,
                'url': BASE + '/courses/10/assignments/20'}
    def test_canvas_source_dedup_deadline_update_manual_edits_and_missing_not_done(self):
        item = self.canvas_item()
        one = self.store.todos.import_canvas(self.a, [item])
        self.assertEqual(one['inserted'], 1)
        self.store.todos.import_canvas(self.a, [item])
        row = self.store.todos.list(self.a)['items'][0]
        self.store.todos.update(self.a, row['id'], {'revision': row['revision'], 'title': '我的标题', 'completed': True})
        changed = {**item, 'title': '远端新标题', 'deadline': '2026-11-01T15:59:59Z'}
        result = self.store.todos.import_canvas(self.a, [changed])
        rows = self.store.todos.list(self.a)['items']
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]['title'], '我的标题')
        self.assertTrue(rows[0]['completed'])
        self.assertEqual(rows[0]['deadline'], changed['deadline'])
        self.assertGreater(result['manual_fields_preserved'], 0)
        self.store.todos.import_canvas(self.a, [])
        self.assertEqual(len(self.store.todos.list(self.a)['items']), 1)
        self.assertEqual(self.store.todos.list(self.b)['items'], [])
    def test_canvas_invalid_source_rolls_back_entire_batch(self):
        good = self.canvas_item()
        bad = {**good, 'external_id': '10:21', 'url': 'https://evil.invalid/'}
        with self.assertRaises(TodoError): self.store.todos.import_canvas(self.a, [good, bad])
        self.assertEqual(self.store.todos.list(self.a)['items'], [])


class CanvasTests(unittest.TestCase):
    tearDown = TodoTests.tearDown
    def setUp(self):
        TodoTests.setUp(self)
        self.requests = []
        self.submitted = False
        self.todo_visible = True
        def network(request, **kwargs):
            self.assertTrue(request.full_url.startswith(BASE + '/api/v1/'))
            self.assertEqual(request.get_method(), 'GET')
            self.assertEqual(request.get_header('Authorization'), 'Bearer synthetic-test-token')
            self.requests.append(request.full_url)
            path = request.full_url[len(BASE):]
            if path.endswith('/profile'): data = {'id': 1, 'secret_unused_field': 'not persisted'}
            elif '/todo?' in path:
                data = [{'type': 'submitting', 'course_id': 10, 'context_short_name': 'TEST1000',
                         'assignment': {'id': 20, 'name': '作业', 'due_at': '2026-10-30T15:59:59Z',
                                        'has_submitted_submissions': True, 'secure_params': 'NEVER_PERSIST_THIS'}}] if self.todo_visible else []
            elif path.endswith('/submissions/self'):
                data = {'submitted_at': '2026-10-27T10:00:00Z' if self.submitted else None,
                        'workflow_state': 'submitted' if self.submitted else 'unsubmitted',
                        'cached_due_date': '2026-11-01T15:59:59Z', 'grade': 'NEVER_PERSIST_GRADE'}
            else: self.fail(path)
            return io.BytesIO(json.dumps(data).encode())
        self.canvas = CanvasSync(self.store, network)
    def test_private_connection_read_only_sync_and_own_submission_truth(self):
        self.canvas.connect(self.a, 'synthetic-test-token')
        self.assertEqual(stat.S_IMODE(self.canvas.path.stat().st_mode), 0o600)
        result = self.canvas.sync(self.a)
        self.assertEqual(result['inserted'], 1)
        row = self.store.todos.list(self.a)['items'][0]
        self.assertFalse(row['completed'])  # Other students submitted != this student submitted.
        self.assertEqual(row['deadline'], '2026-11-01T15:59:59Z')
        with self.store.db() as c:
            dump = '\n'.join(c.iterdump())
        self.assertNotIn('NEVER_PERSIST', dump)
        self.assertNotIn('synthetic-test-token', dump)
        self.assertFalse(self.canvas.status(self.b)['configured'])
        self.assertNotIn('token', json.dumps(self.canvas.status(self.a)))
        with self.assertRaises(TodoError): self.canvas.sync(self.b)
    def test_verified_submission_can_finish_even_after_it_leaves_todo(self):
        self.canvas.connect(self.a, 'synthetic-test-token')
        self.canvas.sync(self.a)
        self.submitted, self.todo_visible = True, False
        self.canvas.sync(self.a)
        self.assertTrue(self.store.todos.list(self.a)['items'][0]['completed'])
    def test_config_permissions_disconnect_and_endpoint_boundaries(self):
        self.canvas.connect(self.a, 'synthetic-test-token')
        for path in ['/api/v1/courses/10/assignments/20/submissions', 'https://evil.invalid/', '/api/v1/users/self/todo?redirect=evil']:
            with self.assertRaises(TodoError): self.canvas.get(path, 'synthetic-test-token')
        self.canvas.path.chmod(0o644)
        self.assertIsNone(self.canvas.config())
        self.canvas.path.chmod(0o600)
        with self.assertRaises(TodoError): self.canvas.disconnect(self.b)
        self.canvas.disconnect(self.a)
        self.assertFalse(self.canvas.path.exists())


class TodoHTTPTests(unittest.TestCase):
    def test_auth_owner_revision_and_string_payload_use_real_http(self):
        with tempfile.TemporaryDirectory() as tmp:
            s = Store(pathlib.Path(tmp))
            token, other_token = 'test-token-A', 'test-token-B'
            owner = hashlib.sha256(token.encode()).hexdigest()
            other = hashlib.sha256(other_token.encode()).hexdigest()
            with s.db() as c: c.executemany('INSERT INTO devices VALUES(?,?)', [(owner, 'A'), (other, 'B')])
            server = ThreadingHTTPServer(('127.0.0.1', 0), Handler); server.store = s
            thread = threading.Thread(target=server.serve_forever, daemon=True); thread.start()
            def api(path, data=None, auth=token):
                request = urllib.request.Request('http://127.0.0.1:' + str(server.server_port) + path,
                    None if data is None else json.dumps(data).encode(), {'Authorization': 'Bearer ' + auth, 'Content-Type': 'application/json'})
                with urllib.request.urlopen(request, timeout=3) as response: return json.load(response)
            try:
                with self.assertRaises(urllib.error.HTTPError) as e: api('/todos', auth='')
                self.assertEqual(e.exception.code, 401)
                created = api('/todos', {'payload': json.dumps({'request_id': 'ui', 'items': [{'title': '手机任务'}]})})['items'][0]
                self.assertEqual(api('/todos', auth=other_token)['items'], [])
                with self.assertRaises(urllib.error.HTTPError) as e:
                    api('/todos/' + created['id'] + '/update', {'payload': json.dumps({'revision': 1, 'completed': True})}, auth=other_token)
                self.assertEqual(e.exception.code, 404)
                saved = api('/todos/' + created['id'] + '/update', {'payload': json.dumps({'revision': 1, 'completed': True})})
                self.assertTrue(saved['completed'])
                with self.assertRaises(urllib.error.HTTPError) as e:
                    api('/todos/' + created['id'] + '/update', {'payload': json.dumps({'revision': 1, 'completed': False})})
                self.assertEqual(e.exception.code, 409)
                self.assertFalse(api('/canvas/status')['configured'])
            finally:
                server.shutdown(); server.server_close(); thread.join()


if __name__ == '__main__':
    unittest.main()
