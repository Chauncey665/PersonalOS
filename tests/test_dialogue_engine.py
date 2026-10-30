import io
import json
import pathlib
import sys
import tempfile
import unittest
import uuid

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]/'server'))
from personal_os import Store, Worker
from mac_actions import MacActions
from todos import TodoError


class Router:
    def __init__(self): self.steps = []; self.relation = 'replace'; self.contexts = []
    def plan(self, row, context):
        self.contexts.append(context)
        return {'steps': self.steps, 'relation': self.relation, 'source': 'model'}


class DialogueTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.root = pathlib.Path(self.temp.name).resolve(); self.s = Store(self.root/'state')
        self.owner = 'paired-device'; self.calls = []
        with self.s.db() as c: c.execute('INSERT INTO devices VALUES(?,?)', (self.owner, 'Test'))
        (self.s.home/'dialogue-engine.json').write_text('{"enabled":true}')
        (self.s.home/'files-access.json').write_text('{"enabled":true}'); (self.s.home/'files-access.json').chmod(0o600)
        self.s.mac = MacActions(self.s, lambda x: self.calls.append(x) or {'accepted': True}, self.root/'Inbox')
        self.session = self.s.sessions.create(self.owner)['id']; self.router = Router(); self.s.sessions.engine.router = self.router

    def turn(self, text, steps, relation='replace', **extra):
        self.router.steps, self.router.relation = steps, relation
        st = self.s.sessions.state(self.owner, self.session)
        body = {'text':text, 'session_id':self.session, 'reply_to':st['task_id'], 'request_id':str(uuid.uuid4()), **extra}
        ident = self.s.submit(body, self.owner); Worker(self.s).execute(self.s.task(ident))
        return ident, self.s.sessions.state(self.owner, self.session)

    def app(self, app, quote): return {'kind':'launch_app','app':app,'source_quote':quote}
    def delegate(self, text): return {'kind':'delegate','text':text,'source_quote':text}
    def search(self, text='找附近的餐厅'): return {'kind':'search_places','query':'餐厅','nearby':True,'navigation':False,'mode':'','source_quote':text}
    def receipt(self, state, result):
        value = {'action_id':state['phone_action']['id'],'result':result}
        self.s.sessions.receipt(self.owner,self.session,value)
        self.s.sessions.engine.tick(Worker(self.s).execute_legacy)
        return value, self.s.sessions.state(self.owner,self.session)
    def places(self): return {'choices':[{'id':'place-a','name':'A餐厅','detail':'约500米'}, {'id':'place-b','name':'B餐厅','detail':'约200米'}]}

    def test_multiple_apps_order_receipts_and_retry(self):
        ident, state = self.turn('先在Mini打开Safari，然后打开备忘录', [self.app('safari','先在Mini打开Safari'),self.app('notes','然后打开备忘录')])
        self.assertEqual([x['app'] for x in self.calls],['safari','notes']); self.assertEqual(state['status'],'completed'); self.assertIn('2/2',state['speech'])
        Worker(self.s).execute(self.s.task(ident)); self.assertEqual(len(self.calls),2)
        with self.s.db() as c: self.assertEqual(c.execute('SELECT count(*) FROM mac_receipts').fetchone()[0],2)
        self.assertEqual(self.s.sessions.current(self.owner)['id'],self.session)

    def test_search_followup_phone_receipt_then_remaining_step(self):
        _, st = self.turn('找附近餐厅，然后打开Mini的Safari',[self.search('找附近餐厅'),self.app('safari','然后打开Mini的Safari')])
        self.assertEqual(st['status'],'phone_action'); self.assertFalse(self.calls)
        _,st = self.receipt(st,self.places())
        # Search produced real candidates, then independent Mac step proceeds.
        self.assertEqual(self.calls[0]['app'],'safari')

    def test_search_select_navigate_then_mac_waits_for_receipt(self):
        _,st = self.turn('找附近餐厅，去最近的那家，公交过去，然后在Mini打开Safari',[
            self.search('找附近餐厅'),{'kind':'select_place','choice':'nearest','mode':'transit','source_quote':'去最近的那家，公交过去'},self.app('safari','然后在Mini打开Safari')])
        _,st = self.receipt(st,self.places())
        self.assertEqual(st['phone_action']['place_id'],'place-b');self.assertEqual(st['phone_action']['mode'],'transit');self.assertFalse(self.calls)
        value,st = self.receipt(st,{'accepted':True});self.assertEqual(st['status'],'completed');self.assertEqual(len(self.calls),1)
        self.s.sessions.receipt(self.owner,self.session,value);self.s.sessions.engine.tick(Worker(self.s).execute_legacy);self.assertEqual(len(self.calls),1)

    def test_natural_followup_current_choices_no_cross_owner(self):
        _,st = self.turn('我想找个吃饭的地方',[self.search('我想找个吃饭的地方')]);_,st=self.receipt(st,self.places())
        self.assertTrue(st['can_continue'])
        text='那就第二家吧，坐公交过去'
        _,st=self.turn(text,[{'kind':'select_place','choice':2,'mode':'transit','source_quote':text}],'continue')
        self.assertEqual(st['phone_action']['place_id'],'place-b');self.assertEqual(self.router.contexts[-1]['choices'][1]['name'],'B餐厅')
        with self.assertRaises(TodoError): self.s.sessions.receipt('other',self.session,{'action_id':st['phone_action']['id'],'result':{'accepted':True}})

    def test_mode_followup_retains_selected_choice(self):
        _,st=self.turn('找附近餐厅',[self.search('找附近餐厅')]);self.receipt(st,self.places())
        _,st=self.turn('那就第二家',[{'kind':'select_place','choice':2,'mode':'','source_quote':'那就第二家'}],'continue');self.assertEqual(st['status'],'needs_input')
        _,st=self.turn('坐公交',[{'kind':'select_place','choice':2,'mode':'transit','source_quote':'坐公交'}],'continue');self.assertEqual(st['phone_action']['place_id'],'place-b')

    def test_topic_switch_discards_old_pending_and_tail(self):
        _,st=self.turn('找附近餐厅，然后Mini打开Safari',[self.search('找附近餐厅'),self.app('safari','然后Mini打开Safari')])
        old=st['phone_action']['id']
        _,st=self.turn('先不吃了，设备状态',[self.delegate('设备状态')])
        self.assertEqual(st['status'],'completed');self.assertFalse(self.calls)
        with self.assertRaises(TodoError):self.s.sessions.receipt(self.owner,self.session,{'action_id':old,'result':self.places()})

    def test_cancel_drops_remaining_work(self):
        _,st=self.turn('找附近餐厅，然后Mini打开Safari',[self.search('找附近餐厅'),self.app('safari','然后Mini打开Safari')])
        _,st=self.turn('我取消，取消这个',[{'kind':'cancel_current','source_quote':'我取消，取消这个'}])
        self.assertEqual(st['status'],'completed');self.assertFalse(self.calls);self.assertIsNone(st['phone_action'])

    def test_failure_preserves_completed_receipt_no_later_execution(self):
        def runner(x):
            self.calls.append(x)
            if x['app']=='notes':raise ValueError('测试执行失败')
            return {'accepted':True}
        self.s.mac.runner=runner
        _,st=self.turn('Mini打开Safari，然后备忘录，然后访达',[self.app('safari','Mini打开Safari'),self.app('notes','然后备忘录'),self.app('finder','然后访达')])
        self.assertEqual(st['status'],'failed');self.assertIn('1/3',st['speech']);self.assertEqual(len(self.calls),2)

    def test_restart_keeps_phone_action_and_continues_without_replay(self):
        _,st=self.turn('Mini打开Safari，然后找附近餐厅，然后公交去第二家',[self.app('safari','Mini打开Safari'),self.search('然后找附近餐厅'),{'kind':'select_place','choice':2,'mode':'transit','source_quote':'然后公交去第二家'}])
        self.s=Store(self.root/'state');self.s.mac=MacActions(self.s,lambda x:self.calls.append(x) or {'accepted':True},self.root/'Inbox')
        self.assertEqual(self.s.sessions.state(self.owner,self.session)['phone_action']['id'],st['phone_action']['id'])
        _,st=self.receipt(st,self.places());self.assertEqual(st['phone_action']['place_id'],'place-b');self.assertEqual(len(self.calls),1)

    def test_file_search_edit_confirm_and_undo_use_real_bytes(self):
        path=self.root/'report.md';path.write_text('Original ending.')
        self.s.file_access.local.home=self.root
        self.s.sessions.classify=lambda *a:{'kind':'file_search','device':'mini','folder':'home','query':'report.md','instruction':''}
        self.s.brain.transport=lambda *a,**kw:io.BytesIO(json.dumps({'message':{'content':json.dumps({'changes':[{'index':0,'text':'Short ending.'}]})}}).encode())
        ident,st=self.turn('找到report.md，然后把它结尾改简短',[self.delegate('找到report.md'),self.delegate('然后把它结尾改简短')])
        self.assertEqual(st['status'],'confirmation');self.assertEqual(path.read_text(),'Original ending.')
        _,st=self.turn('确认写回',[self.delegate('确认写回')],'continue')
        self.assertEqual(st['status'],'completed');self.assertEqual(path.read_text(),'Short ending.')
        _,st=self.turn('撤销刚才的操作',[self.delegate('撤销刚才的操作')]);self.assertEqual(st['status'],'confirmation')
        self.turn('确认撤销',[self.delegate('确认撤销')],'continue');self.assertEqual(path.read_text(),'Original ending.')

    def test_ui_confirmation_preserves_existing_gate(self):
        folder=self.root/'box';folder.mkdir();f=folder/'sample.txt';f.write_text('fixture')
        with self.s.db() as c:c.execute('INSERT INTO roots VALUES(?)',(str(folder),))
        self.s.sessions.classify=lambda *a:{'kind':'organize','device':'mini','folder':'home','query':'box','instruction':''}
        ident,st=self.turn('整理box文件夹',[self.delegate('整理box文件夹')]);self.assertEqual(st['status'],'confirmation')
        self.s.act(ident,'approve');self.s.sessions.engine.tick(Worker(self.s).execute_legacy)
        self.assertFalse(f.exists());self.s.act(ident,'undo');self.assertEqual(f.read_text(),'fixture')

    def test_explaining_an_app_command_never_executes_it(self):
        self.s.brain.transport=lambda *a,**kw:io.BytesIO(json.dumps({'message':{'content':'这句话表示依次打开两个应用。'}}).encode())
        text='解释“在Mini打开Safari”是什么意思'
        _,st=self.turn(text,[self.delegate(text)])
        self.assertEqual(st['status'],'completed');self.assertFalse(self.calls)

    def test_candidate_advice_keeps_choices_and_uses_brain_context(self):
        _,st=self.turn('找附近餐厅',[self.search('找附近餐厅')]);self.receipt(st,self.places())
        payloads=[]
        def transport(req,**kw):
            payloads.append(json.loads(req.data));return io.BytesIO(json.dumps({'message':{'content':'B餐厅更近，但暂时没有口味和营业资料。'}}).encode())
        self.s.brain.transport=transport
        _,st=self.turn('这两家哪一家更适合我',[self.delegate('这两家哪一家更适合我')],'continue')
        self.assertEqual(st['status'],'needs_input');self.assertIn('B餐厅',st['speech'])
        self.assertIn('B餐厅',json.dumps(payloads,ensure_ascii=False))
        _,st=self.turn('那就第二家走过去',[{'kind':'select_place','choice':2,'mode':'walking','source_quote':'那就第二家走过去'}],'continue')
        self.assertEqual(st['phone_action']['place_id'],'place-b')

    def test_ui_only_parent_history_and_deadline_field_preserved(self):
        import datetime as dt
        due=(dt.datetime.now(dt.timezone.utc)+dt.timedelta(hours=2)).isoformat()
        ident,st=self.turn('提醒我喝水',[self.delegate('提醒我喝水')],due=due)
        with self.s.db() as c:
            children=[dict(r) for r in c.execute("SELECT state,due FROM tasks WHERE id<>?",(ident,))]
        self.assertEqual(children[0]['state'],'scheduled');self.assertIsNotNone(children[0]['due'])
        ident,st=self.turn('Mini打开Safari然后备忘录',[self.app('safari','Mini打开Safari'),self.app('notes','然后备忘录')])
        rows=self.s.tasks(self.owner)
        self.assertEqual(sum(1 for r in rows if r['id']==ident),1)
        self.assertFalse(any(json.loads(r['body']).get('request_id','').startswith('wf-') and r['state']!='scheduled' for r in rows))


if __name__=='__main__':unittest.main()
