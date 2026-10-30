"""Persistent conversational plans. Models propose; existing executors authorize.

Each step has its own durable task and receipt. Internal sessions reuse the
existing phone/file confirmation protocol without replacing the public session.
An interrupted running step is never silently replayed.
"""
import json
import hashlib
import pathlib
import re
import threading
import time
import uuid

from todos import TodoError


def pack(value):
    return json.dumps(value, ensure_ascii=False, separators=(',', ':'))


class DialogueEngine:
    def __init__(self, store):
        self.s = store
        self.lock = threading.RLock()
        self.router = None

    @staticmethod
    def initialize(c):
        c.executescript('''
        CREATE TABLE IF NOT EXISTS assistant_workflows(
          id TEXT PRIMARY KEY,session TEXT NOT NULL,owner TEXT NOT NULL,
          task TEXT NOT NULL UNIQUE,status TEXT NOT NULL,data TEXT NOT NULL,updated REAL NOT NULL);
        CREATE TABLE IF NOT EXISTS assistant_internal_sessions(session TEXT PRIMARY KEY);
        CREATE INDEX IF NOT EXISTS workflow_status ON assistant_workflows(status,updated);
        ''')

    def enabled(self):
        try:
            return json.loads((self.s.home/'dialogue-engine.json').read_text()).get('enabled') is True
        except (OSError, ValueError, AttributeError):
            return False

    def internal(self, session):
        with self.s.db() as c:
            return bool(c.execute('SELECT 1 FROM assistant_internal_sessions WHERE session=?', (session,)).fetchone())

    def load(self, ident, owner=None):
        with self.s.db() as c:
            r = c.execute('SELECT * FROM assistant_workflows WHERE id=?', (ident,)).fetchone()
        if not r or owner is not None and r['owner'] != owner:
            raise TodoError('执行计划不存在', 404)
        w = dict(r)
        w['data'] = json.loads(w['data'])
        return w

    def persist(self, w):
        with self.s.db() as c:
            c.execute('UPDATE assistant_workflows SET status=?,data=?,updated=? WHERE id=?',
                      (w['status'], pack(w['data']), time.time(), w['id']))

    def session_data(self, owner, ident):
        with self.s.db() as c:
            r = self.s.sessions.owned(c, owner, ident)
        return r, json.loads(r['data'])

    def run(self, row, fallback):
        from semantic_router import SemanticRouter
        with self.lock:
            body = json.loads(row['body'])
            owner, session = row['owner'], body['session_id']
            with self.s.db() as c:
                previous = c.execute('SELECT id FROM assistant_workflows WHERE task=?', (row['id'],)).fetchone()
            if previous:
                self.advance(self.load(previous['id'], owner), fallback)
                return
            r, data = self.session_data(owner, session)
            if r['latest'] != row['id']:
                raise TodoError('会话已有新进展', 409)
            resume_navigation=False
            last=data.get('last_navigation',{})
            if not data.get('pending') and last.get('choices') and time.time()-last.get('at',0)<540:
                if re.search(r'改成|改为|换成|换为|刚才那家|刚才那个餐厅|same place|change.*(?:walk|bus|transit|driv)',body['text'],re.I) and re.search(r'步行|走路|走过去|公交|巴士|地铁|开车|驾车|walk|bus|transit|driv|刚才那家|刚才那个餐厅',body['text'],re.I):
                    data['pending']=dict(last);resume_navigation=True
            context = self.s.brain.conversation_context(row)
            context['pending'] = data.get('pending', {})
            if resume_navigation:context['choices']=data['pending']['choices']
            context['preferences'] = self.s.brain.preferences(owner)[:16]
            context['last_result'] = data.get('last_result', '')[:1800]
            # Model sees no sharing payload or credentials; executors retain refs.
            self.router = self.router or SemanticRouter(self.s)
            proposal = self.router.plan(row, context)
            old = self.load(data['engine_workflow'], owner) if data.get('engine_workflow') else None
            tail = []
            if old and old['status'] in ('waiting', 'ready'):
                child = old['data']['child_session']
                if proposal['relation'] == 'continue':
                    tail = old['data']['steps'][old['data']['cursor'] + 1:]
                else:
                    self.cancel_pending(owner, child)
                old['status'] = 'superseded'
                self.persist(old)
            elif old:
                child = old['data']['child_session']
                if proposal['relation'] != 'continue':
                    self.cancel_pending(owner, child)
            else:
                child = self.s.sessions.create(owner)['id']
                inherited = {k: v for k, v in data.items() if k not in ('engine_workflow', 'speech', 'phone_action')}
                inherited['conversation_history'] = [
                    {k: x.get(k, '') for k in ('text', 'result', 'state')}
                    for x in self.s.brain.history(row, 6)]
                with self.s.db() as c:
                    c.execute('INSERT INTO assistant_internal_sessions VALUES(?)', (child,))
                    c.execute('UPDATE assistant_sessions SET data=? WHERE id=?', (pack(inherited), child))
            steps = [{**s, 'state': 'pending'} for s in proposal['steps']] + [
                {k: v for k, v in s.items() if k not in ('task', 'state', 'result')} | {'state': 'pending'} for s in tail]
            if len(steps) > 6:
                raise TodoError('本次步骤较多，请先完成当前事项，再继续下一项')
            # Authorized sharing references belong to the conversation, not to
            # whichever action happens to be first (e.g. open an app then edit).
            _, child_data = self.session_data(owner, child)
            if resume_navigation and proposal['relation']=='continue':child_data['pending']=data['pending']
            refs=json.loads(body.get('input_refs','{}'))
            child_data.update({k:v for k,v in refs.items() if k in ('shared_text','document_ids','transfer_task')})
            if body.get('document_ids'):child_data['document_ids']=json.loads(body['document_ids'])
            with self.s.db() as c:c.execute('UPDATE assistant_sessions SET data=? WHERE id=?',(pack(child_data),child))
            ident = str(uuid.uuid4())
            w = {'id': ident, 'session': session, 'owner': owner, 'task': row['id'], 'status': 'ready',
                 'data': {'steps': steps, 'cursor': 0, 'child_session': child,
                          'body': body, 'source': proposal['source'], 'results': []}}
            with self.s.db() as c:
                c.execute('INSERT INTO assistant_workflows VALUES(?,?,?,?,?,?,?)',
                          (ident, session, owner, row['id'], 'ready', pack(w['data']), time.time()))
                data['engine_workflow'] = ident
                c.execute('UPDATE assistant_sessions SET data=? WHERE id=?', (pack(data), session))
            self.s.audit(row['id'], 'dialogue_plan', {'steps': [x['kind'] for x in steps], 'source': proposal['source'], 'relation': proposal['relation']})
            self.advance(w, fallback)

    def cancel_pending(self, owner, session):
        r, d = self.session_data(owner, session)
        target = d.get('pending', {}).get('target')
        if target:
            t = self.s.task(target, owner)
            if t['state'] in ('queued', 'awaiting_confirmation', 'needs_details', 'scheduled'):
                self.s.act(target, 'cancel')
        d.pop('pending', None)
        d.pop('phone_action', None)
        with self.s.db() as c:
            c.execute("UPDATE assistant_sessions SET data=?,status='completed' WHERE id=?", (pack(d), session))

    def project(self, w, state=None, error=None):
        state = state or self.s.sessions.state(w['owner'], w['data']['child_session'])
        child, d = self.session_data(w['owner'], w['data']['child_session'])
        status, speech = state['status'], state['speech']
        if error:
            status, speech = 'failed', error
        finished = [s for s in w['data']['steps'] if s['state'] == 'completed']
        detail=speech
        if len(w['data']['steps']) > 1:
            prefix = '已处理 ' + str(len(finished)) + '/' + str(len(w['data']['steps'])) + ' 步；'
            if status == 'completed':
                last=finished[-1]
                lastplan=json.loads(self.s.task(last['task'])['plan'])
                speech=prefix+self.s.sessions.summary(last.get('result',''),lastplan)
            else:
                speech = prefix + speech
            detail='\n'.join(str(i+1)+'. '+s['source_quote']+'\n'+s.get('result',s['state']) for i,s in enumerate(w['data']['steps']))+'\n'+speech
        elif child['latest'] and status=='completed':
            detail=self.s.task(child['latest'],w['owner'])['result']
        d['speech'], d['engine_workflow'] = speech, w['id']
        d['calendar_task_id'] = state.get('calendar_task_id', '')
        with self.s.db() as c:
            parent = self.s.sessions.owned(c, w['owner'], w['session'])
            if parent['latest'] != w['task']:
                return
            c.execute('UPDATE assistant_sessions SET data=?,status=?,updated=? WHERE id=?',
                      (pack(d), status, time.time(), w['session']))
        tstate = {'processing': 'running', 'confirmation': 'awaiting_confirmation', 'needs_input': 'needs_details',
                  'phone_action': 'needs_details', 'failed': 'failed', 'completed': 'completed'}.get(status, 'needs_details')
        target = d.get('pending', {}).get('target') if status == 'confirmation' else child['latest']
        concrete = json.loads(self.s.task(target,w['owner'])['plan']) if target else {}
        plan = {**concrete, 'workflow': w['id'], 'spoken_reply':speech, 'steps': [
            {k: x[k] for k in ('kind', 'state', 'task') if k in x} for x in w['data']['steps']],
                'action_task_id': d.get('pending', {}).get('target', ''), 'calendar_task_id': d.get('calendar_task_id', '')}
        self.s.update(w['task'], state=tstate, result=detail, plan=pack(plan))

    def advance(self, w, fallback):
        if w['status'] not in ('ready', 'running', 'waiting'):
            return
        steps = w['data']['steps']
        while w['data']['cursor'] < len(steps):
            step = steps[w['data']['cursor']]
            executed_now = False
            if step['state'] == 'pending':
                child = self.s.sessions.state(w['owner'], w['data']['child_session'])
                _, child_data = self.session_data(w['owner'], child['id'])
                if child_data.get('pending',{}).get('type')=='confirm':
                    step['confirmation_target']=child_data['pending']['target']
                body = {k: v for k, v in w['data']['body'].items() if k in
                        ('calendar_context', 'time_zone', 'root', 'include_snippets')}
                # Sharing payload belongs to this request's first step only.
                # Later steps use validated references already in child state.
                if w['data']['cursor'] == 0:
                    for k in ('input_refs','document_ids','message_draft'):
                        if k in w['data']['body']:body[k]=w['data']['body'][k]
                    if 'due' in w['data']['body'] and step['kind']=='delegate':
                        body['due']=w['data']['body']['due']
                body.update(text=step.get('text') or step['source_quote'], session_id=child['id'], reply_to=child['task_id'],
                            request_id='wf-' + w['id'] + '-' + str(w['data']['cursor']))
                # Allocate task, attach child turn and save the step in ONE commit.
                # Restart cannot leave an orphan queued task or change reply_to.
                from personal_os import validate_task_body
                body = validate_task_body(body)
                ident = str(uuid.uuid4())
                step.update(task=ident, state='queued')
                w['status'] = 'ready'
                with self.s.db() as c:
                    c.execute('BEGIN IMMEDIATE')
                    self.s.sessions.attach(c, w['owner'], body, ident)
                    c.execute('INSERT INTO tasks(id,body,state,result,plan,created,due,owner) VALUES(?,?,?,?,?,?,NULL,?)',
                              (ident, pack(body), 'queued', '', '{}', time.time(), w['owner']))
                    canonical = {k:v for k,v in body.items() if k != 'request_id'}
                    digest = hashlib.sha256(json.dumps(canonical,ensure_ascii=False,sort_keys=True,separators=(',',':')).encode()).hexdigest()
                    c.execute('INSERT INTO task_requests VALUES(?,?,?,?)', (w['owner'],body['request_id'],ident,digest))
                    c.execute('UPDATE assistant_workflows SET data=?,status=?,updated=? WHERE id=?',
                              (pack(w['data']),w['status'],time.time(),w['id']))
                    self.s._audit(c, ident, 'dialogue_step_queued', {'workflow':w['id'],'index':w['data']['cursor']})
            if step['state'] == 'queued':
                ident = step['task']
                step['state'] = 'running'
                w['status'] = 'running'
                with self.s.db() as c:
                    c.execute('BEGIN IMMEDIATE')
                    if not c.execute("UPDATE tasks SET state='running' WHERE id=? AND state='queued'",(ident,)).rowcount:
                        raise TodoError('步骤已有执行进展，请检查回执')
                    c.execute('UPDATE assistant_workflows SET status=?,data=?,updated=? WHERE id=?',
                              (w['status'],pack(w['data']),time.time(),w['id']))
                try:
                    self.execute_step(self.s.task(ident), step, fallback)
                except Exception as error:
                    self.s.update(ident, state='failed', result=str(error))
                    self.s.audit(ident, 'dialogue_step_failed', type(error).__name__)
                executed_now = True
            state = self.s.sessions.state(w['owner'], w['data']['child_session'])
            if state['status'] == 'processing' and not executed_now:
                self.s.update(step['task'],state='needs_review',result='此步骤执行被中断，尚无成功回执；没有自动重做。')
                state = self.s.sessions.state(w['owner'],w['data']['child_session'])
            if state['status'] == 'completed':
                step.update(state='completed', result=self.s.task(step['task'])['result'])
                _,child_data=self.session_data(w['owner'],w['data']['child_session'])
                last_action=child_data.get('last_action')
                if last_action and last_action in (step['task'],step.get('confirmation_target')):
                    w['data']['undo_target']=last_action
                w['data']['cursor'] += 1
                w['status'] = 'ready'
                self.persist(w)
                continue
            if state['status'] == 'needs_input' and step['kind'] == 'search_places' and w['data']['cursor'] + 1 < len(steps):
                _, d = self.session_data(w['owner'], w['data']['child_session'])
                if d.get('pending', {}).get('type') == 'places':
                    step.update(state='completed', result=state['speech'])
                    w['data']['cursor'] += 1
                    w['status'] = 'ready'
                    self.persist(w)
                    continue
            w['status'] = 'failed' if state['status'] == 'failed' else 'waiting'
            if state['status'] == 'failed':
                step['state'] = 'failed'
            self.persist(w)
            self.project(w, state)
            return
        w['status'] = 'completed'
        self.persist(w)
        self.project(w)

    def execute_step(self, row, step, fallback):
        v = self.s.sessions
        _, d = self.session_data(row['owner'], json.loads(row['body'])['session_id'])
        kind = step['kind']
        if kind == 'launch_app':
            return v.mac_open(row, d, {'operation': 'launch_app', 'app': step['app']})
        if kind == 'search_places':
            return v.phone(row, d, 'search_places', **{k: step[k] for k in ('query', 'nearby', 'mode', 'navigation')})
        if kind == 'select_place':
            p = d.get('pending', {})
            choices = p.get('choices', [])
            index = step['choice']
            if index == 'nearest':
                import re
                distances = []
                for x in choices:
                    m = re.fullmatch(r'约(\d+(?:\.\d+)?)(米|公里)', x['detail'])
                    if not m:
                        raise TodoError('地图没有提供完整距离，请说要选择哪家')
                    distances.append(float(m[1]) * (1000 if m[2] == '公里' else 1))
                if not distances:
                    raise TodoError('尚未收到地图候选')
                if distances.count(min(distances)) != 1:
                    raise TodoError('多个地点距离相同，请说名称或编号')
                index = distances.index(min(distances)) + 1
            if type(index) is not int or not 1 <= index <= len(choices):
                raise TodoError('请选择本次地图返回的地点')
            mode = step['mode'] or p.get('mode', '')
            if not mode:
                p['selected'] = index - 1
                return v.save(row, d, 'needs_input', '去' + choices[index-1]['name'] + '，步行、公交还是驾车？')
            if mode not in ('walking', 'transit', 'driving'):
                raise TodoError('交通方式无效')
            v.phone(row, d, 'navigate', place_id=choices[index-1]['id'], mode=mode)
            return v.save(row, d, 'phone_action', '去' + choices[index-1]['name'] + '，正在打开' + {'walking':'步行','transit':'公交','driving':'驾车'}[mode] + '导航。')
        if kind == 'cancel_current':
            self.cancel_pending(row['owner'], json.loads(row['body'])['session_id'])
            _, d = self.session_data(row['owner'], json.loads(row['body'])['session_id'])
            return v.done(row, d, '已取消当前待执行操作，已经完成的步骤保留。')
        if kind == 'clarify':
            d['pending'] = {'type': 'semantic_clarification'}
            return v.save(row, d, 'needs_input', step['question'])
        if kind == 'delegate':
            body=json.loads(row['body'])
            # Automatic legacy keyword extraction is not a semantic approval.
            body.pop('mac_action',None)
            row={**row,'body':pack(body)}
            # A semantic recommendation about current candidates must reach the
            # conversation model, not the old numeric-choice branch. Keep the
            # pending candidates so the next answer can still choose a place.
            if d.get('pending',{}).get('type') in ('places','place_query'):
                outcome=self.s.brain.run(row)
                self.s.update(row['id'],**outcome)
                return v.save(row,d,'needs_input' if outcome['state']=='completed' else 'failed',v.summary(outcome['result']))
            from personal_os import Worker
            # Explicit explanations/quoted commands must remain conversation.
            import re
            if re.search(r'解释|什么意思|含义|举例|\b(?:explain|meaning)\b',body['text'],re.I):
                outcome=self.s.brain.run(row);self.s.update(row['id'],**outcome)
                return v.done(row,d,outcome['result']) if outcome['state']=='completed' else v.save(row,d,'failed',outcome['result'])
            return v.run_legacy(row, lambda r:Worker(self.s).execute_legacy(r,semantic=True), semantic=True)
        raise TodoError('计划含不支持的步骤')

    def receipt(self, owner, session, value):
        with self.lock:
            if not isinstance(value, dict) or set(value) != {'action_id','result'} or not isinstance(value['result'], dict):
                raise TodoError('手机回执格式无效')
            _, data = self.session_data(owner, session)
            if not data.get('engine_workflow'):
                return None
            w = self.load(data['engine_workflow'], owner)
            from voice_sessions import signature
            digest = signature(value)
            with self.s.db() as c:
                old = c.execute('SELECT digest,result FROM assistant_receipts WHERE session=? AND action_id=? AND owner=?',
                                (session, value.get('action_id'), owner)).fetchone()
            if old:
                if old['digest'] != digest:
                    raise TodoError('此操作已收到不同回执', 409)
                return json.loads(old['result'])
            if w['status'] not in ('waiting', 'ready'):
                raise TodoError('手机操作已过期', 409)
            result = self.s.sessions.receipt_legacy(owner, w['data']['child_session'], value)
            with self.s.db() as c:
                c.execute('INSERT INTO assistant_receipts VALUES(?,?,?,?,?)', (session, value['action_id'], owner, digest, pack(result)))
            w['status'] = 'ready'
            self.persist(w)
            # No model work inside the HTTP acknowledgement; worker advances.
            child = self.s.sessions.state(owner, w['data']['child_session'])
            self.project(w, {**child, 'status':'processing', 'speech':'已收到执行回执，正在继续处理。'})
            return result

    def tick(self, fallback):
        if not self.enabled():
            return
        with self.lock:
            with self.s.db() as c:
                rows = c.execute("SELECT id FROM assistant_workflows WHERE status IN ('ready','running') ORDER BY updated LIMIT 1").fetchall()
            for r in rows:
                w = self.load(r['id'])
                try:
                    self.advance(w, fallback)
                except Exception as error:
                    w['status'] = 'failed'
                    self.persist(w)
                    self.project(w, error='执行暂停，已完成步骤保留。' + str(error)[:200])
                    self.s.audit(w['task'], 'dialogue_interrupted', type(error).__name__)

    def task_action(self, ident, action):
        with self.lock:
            with self.s.db() as c:
                row = c.execute('SELECT id FROM assistant_workflows WHERE task=?', (ident,)).fetchone()
            if not row:
                return False
            w = self.load(row['id'])
            parent, _ = self.session_data(w['owner'], w['session'])
            if parent['latest'] != ident or w['status'] in ('superseded','cancelled'):
                raise TodoError('这条操作已有后续进展，请打开当前任务再操作',409)
            _, d = self.session_data(w['owner'], w['data']['child_session'])
            if action == 'cancel':
                self.cancel_pending(w['owner'], w['data']['child_session'])
                w['status'] = 'cancelled'
                self.persist(w)
                self.project(w, {'status':'completed','speech':'已取消剩余步骤，已完成的操作保留。'})
                self.s.update(ident, state='cancelled')
                return True
            if action == 'undo':
                if w['status'] != 'completed':
                    raise TodoError('当前流程尚未完成，不能从这里撤销',409)
                target = w['data'].get('undo_target')
                if not target:
                    raise TodoError('没有可撤销的操作')
                self.s.act(target, 'undo')
                self.s.update(ident, state='undone', result=self.s.task(target)['result'])
                return True
            p = d.get('pending', {})
            if action != 'approve' or p.get('type') != 'confirm' or w['status'] not in ('waiting','ready'):
                raise TodoError('当前没有待确认操作')
            # UI approval uses the same expiry, plan digest and one-use checks.
            child = self.s.sessions.state(w['owner'], w['data']['child_session'])
            childrow = self.s.task(child['task_id'])
            body = json.loads(childrow['body'])
            body['reply_to'] = p['prompt_task']
            self.s.sessions.consume_confirmation({**childrow, 'body':pack(body)}, d, '确认')
            w['status'] = 'ready'
            self.persist(w)
            self.project(w, {**self.s.sessions.state(w['owner'],w['data']['child_session']),
                             'status':'processing','speech':'已收到确认回执，正在继续处理。'})
            return True
