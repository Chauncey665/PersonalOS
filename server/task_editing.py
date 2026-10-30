"""Grounded task edits, revision checks, durable receipts and bounded follow-ups."""
import datetime as dt
import json
import re
import time
import urllib.request
import uuid
from calendar_journal import DATE_RE, _date, _localize, validate_zone, DEFAULT_ZONE
from task_entities import candidates
from todos import TodoError, TodoStore

def source_span(value, text):
    """Allow model-added spaces only; dates, digits and negation stay literal."""
    if value in text:return value
    indices=[i for i,ch in enumerate(text) if not ch.isspace()]
    compact=''.join(text[i] for i in indices);needle=re.sub(r'\s','',value)
    start=compact.find(needle)
    if not needle or start<0:raise TodoError('修改必须来自本轮原话')
    return text[indices[start]:indices[start+len(needle)-1]+1]

VERBS = {
    'postpone': r'推迟|延期|推到|挪到|移到|延到|改到|改成|明天再做|postpone|reschedule',
    'duration': r'用时|时长|预计|花|留|分钟|小时|duration|minutes?|hours?',
    'priority': r'优先|紧急|重要|priority',
    'deadline': r'截止|ddl|deadline',
    'rename': r'改名|名称|标题|rename',
    'pause': r'暂停|暂不安排|先不安排|取消安排|pause',
    'resume': r'恢复安排|重新安排|继续安排|resume',
    'complete': r'标记完成|已完成|做完了|勾掉|勾选|complete|finished',
    'reopen': r'取消完成|取消勾选|还没完成|重开|reopen',
    'split': r'拆分|拆成|分解|break down',
}

def requested(text):
    if re.search(r'解释|什么意思|翻译|举例|假设|如果|是否|要不要|怎么|如何|不要|不用|先别|只给建议|[“”「」]|don.t|do not', text, re.I): return False
    if re.search(r'新增|新建|添加|记录',text) and not re.search(r'已有|已经记录',text): return False
    return bool(re.search('|'.join(VERBS.values()), text, re.I) and
                re.search(r'任务|待办|作业|复习|cheat\s*sheet|homework|assignment|lab\s*\d|[零〇一二两三四五六七八九\d]{4}|项目', text, re.I))

class TaskEditing:
    def __init__(self, store): self.s = store

    @staticmethod
    def initialize(c):
        c.executescript('''CREATE TABLE IF NOT EXISTS task_edit_receipts(owner TEXT NOT NULL,task TEXT NOT NULL,payload TEXT NOT NULL,PRIMARY KEY(owner,task));
        CREATE TABLE IF NOT EXISTS todo_priorities(owner TEXT NOT NULL,todo_id TEXT NOT NULL,priority INTEGER NOT NULL,paused INTEGER NOT NULL DEFAULT 0,PRIMARY KEY(owner,todo_id));''')

    def parse(self, row):
        text = json.loads(row['body'])['text']
        schema = {'type':'object','properties':{'edits':{'type':'array','minItems':1,'maxItems':6,'items':{
            'type':'object','properties':{'target':{'type':'string'},'operation':{'enum':list(VERBS)},
            'value':{'type':'string'},'quote':{'type':'string'}},'required':['target','operation','value','quote'],'additionalProperties':False}}},'required':['edits'],'additionalProperties':False}
        payload = {'model':self.s.brain.model,'stream':False,'think':False,'keep_alive':self.s.brain.keep_alive,'format':schema,
            'messages':[{'role':'system','content':
            '提取用户修改已有待办的请求，返回JSON edits。每项target是用户原文中的任务名称片段，quote是含目标及修改要求的原文连续片段。不要生成任务ID。'
            'value必须逐字摘自quote：postpone用日期和时段（明天下午），duration用时长（一个半小时），priority用优先程度，deadline用新截止日期时间，rename用新标题，split用用户要求拆分的原话。pause/resume/complete/reopen用空value。'
            '延期执行用postpone，只有明确修改截止日期才用deadline。改变用时用duration，不能因此改变截止。一次对同一任务修改日期和用时要两项。保留否定，不猜目标。'},
            {'role':'user','content':text}], 'options':{'num_ctx':4096,'num_predict':900,'temperature':0}}
        req = urllib.request.Request('http://127.0.0.1:11434/api/chat', json.dumps(payload,ensure_ascii=False).encode(), {'Content-Type':'application/json'})
        with (self.s.brain.transport or urllib.request.urlopen)(req,timeout=45) as r:
            return json.loads(json.loads(r.read(100000))['message']['content'])['edits']

    def run(self, row):
        b = json.loads(row['body'])
        from schedule_adjustment import requested as focus_requested
        if focus_requested(b['text']): return None
        if b.get('document_ids') or b.get('message_draft'): return None
        # Keep existing model-assisted decomposition when the user asks PersonalOS
        # to propose the steps; this editor handles explicitly enumerated steps.
        if re.search(r'拆分|拆成|分解|break down',b['text'],re.I) and not re.search(r'[、；;]',b['text']):return None
        if not requested(b['text']):return self.contextual(row)
        with self.s.db() as c:
            prior = c.execute('SELECT payload FROM task_edit_receipts WHERE owner=? AND task=?',(row['owner'],row['id'])).fetchone()
        if prior: return json.loads(prior['payload'])
        if not self.s.todos.list(row['owner'])['items']: return None
        try:
            return self.prepare(row, self.parse(row))
        except TodoError as error:
            return {'state':'needs_details','result':str(error),'plan':json.dumps({'kind':'task_edit_input','request':b['text']},ensure_ascii=False)}

    def contextual(self,row):
        b=json.loads(row['body']);text=b['text'].strip(' 。！!')
        m=re.fullmatch(r'(?:那|它|这项|这个任务|再|再把它)?(?:改成|改为|改到|挪到)(.+)',text)
        if not b.get('session_id') or not m:return None
        value=m[1]
        op='duration' if re.fullmatch(r'[\d一二两三四五六七八九十百半个.]+(?:分钟|小时)(?:半)?',value) else 'postpone' if DATE_RE.search(value) else None
        if not op:return None
        with self.s.db() as c:
            previous=c.execute("SELECT t.plan,t.created FROM tasks t JOIN assistant_turns a ON a.task=t.id WHERE a.owner=? AND a.session=? AND t.id<>? AND t.state='completed' ORDER BY a.created DESC,a.rowid DESC LIMIT 1",(row['owner'],b['session_id'],row['id'])).fetchone()
            if not previous or time.time()-previous['created']>900:return None
            plan=json.loads(previous['plan']);ids=plan.get('edited_ids',[])
            if plan.get('kind')!='task_edit' or len(ids)!=1:return None
            todo=c.execute('SELECT * FROM todos WHERE owner=? AND id=?',(row['owner'],ids[0])).fetchone()
        if not todo or plan.get('revisions',{}).get(ids[0])!=todo['revision']:
            return {'state':'needs_details','result':'刚才的待办已经更新，请说完整任务名再修改。','plan':'{}'}
        try:return self.apply(row,[{'id':todo['id'],'revision':todo['revision'],'target':todo['title'],'operation':op,'value':value,'quote':text}])
        except TodoError as error:return {'state':'needs_details','result':str(error),'plan':'{}'}

    def prepare(self, row, edits):
        text = json.loads(row['body'])['text']
        if not requested(text) or not isinstance(edits,list) or not 1<=len(edits)<=6: raise TodoError('请明确要修改的待办和内容')
        page = self.s.todos.list(row['owner'])
        if page['next_cursor']: raise TodoError('待办过多，请在 App 选择具体事项后修改')
        resolved = []
        edits=[dict(x) if isinstance(x,dict) else x for x in edits]
        # Validate the entire batch before asking about any one target.
        for spec in edits:
            if not isinstance(spec,dict) or set(spec)!={'target','operation','value','quote'} or any(not isinstance(v,str) for v in spec.values()): raise TodoError('待办修改格式无效')
            spec['quote']=source_span(spec['quote'],text)
            spec['target']=source_span(spec['target'],spec['quote'])
            spec['value']=source_span(spec['value'],spec['quote'])
            if spec['operation'] in ('complete','reopen') and re.search(r'取消完成|取消勾选|还没完成|重开|reopen',spec['quote'],re.I):
                spec['operation']='reopen';spec['value']=''
            if spec['operation']=='split':
                split=re.search(r'(?:拆成|拆分为|分解为).+',spec['quote'])
                if split:spec['value']=split[0]
            if not spec['quote'] or spec['quote'] not in text or not spec['target'] or spec['target'] not in spec['quote'] or spec['value'] not in spec['quote']: raise TodoError('修改必须来自本轮原话')
            if spec['operation'] not in VERBS or not re.search(VERBS[spec['operation']],spec['quote'],re.I): raise TodoError('本轮没有授权这项修改')
        for spec in edits:
            if not isinstance(spec,dict) or set(spec)!={'target','operation','value','quote'} or any(not isinstance(v,str) for v in spec.values()): raise TodoError('待办修改格式无效')
            if not spec['quote'] or spec['quote'] not in text or not spec['target'] or spec['target'] not in spec['quote'] or spec['value'] not in spec['quote']: raise TodoError('修改必须来自本轮原话')
            op = spec['operation']
            if op not in VERBS or not re.search(VERBS[op],spec['quote'],re.I): raise TodoError('本轮没有授权这项修改')
            if op=='complete' and re.search(r'未完成|没完成|取消',spec['quote']): raise TodoError('未完成不能标记为完成')
            matches = candidates(page['items'],spec['target'])
            if op not in ('reopen','rename'): matches=[x for x in matches if not x['completed']]
            if len(matches)!=1:
                labels = '；'.join(str(i+1)+'，'+x['title'] for i,x in enumerate(matches[:8]))
                question = ('你指哪一项：'+labels+'？') if matches else '没有找到“'+spec['target']+'”，请说已有待办的名称。'
                return {'state':'needs_details','result':question,'plan':json.dumps({'kind':'task_edit_choice','edits':edits,'index':len(resolved),'resolved':resolved,'choices':[{'id':x['id'],'title':x['title'],'revision':x['revision']} for x in matches[:8]]},ensure_ascii=False)}
            resolved.append({**spec,'id':matches[0]['id'],'revision':matches[0]['revision']})
        return self.apply(row,resolved)

    def followup(self, row, target):
        plan = json.loads(target['plan'])
        if plan.get('kind') not in ('task_edit_choice','task_edit_input'): return None
        b=json.loads(row['body']); original=json.loads(target['body'])
        if b.get('session_id')!=original.get('session_id') or row['owner']!=target['owner'] or time.time()-target['created']>900:
            raise TodoError('这次选择已过期，请重新说要修改的事项')
        text=b['text'].strip(' 。！!')
        if plan['kind']=='task_edit_input':
            combined={**b,'text':plan['request']+'，补充：'+text}
            return self.run({**row,'body':json.dumps(combined,ensure_ascii=False)})
        match=re.fullmatch(r'(?:就|选)?第?([一二三四五六七八1-8])(?:个|项)?',text)
        options=plan['choices']; selected=None
        if not options:
            options=candidates(self.s.todos.list(row['owner'])['items'],text)
        if match:
            index=int(match[1].translate(str.maketrans('一二三四五六七八','12345678')))-1
            if index<len(options): selected=options[index]
        else:
            found=candidates(options,text)
            if len(found)==1:selected=found[0]
        if not selected:return {'state':'needs_details','result':'请说候选编号或完整任务名；说“取消”可放弃修改。','plan':target['plan']}
        # Re-validate all fields against the original authorized request, then
        # use the chosen identity/revision, never a model generated UUID.
        page=self.s.todos.list(row['owner'])['items']
        item=next((x for x in page if x['id']==selected['id']),None)
        if not item or item['revision']!=selected['revision']:raise TodoError('待办已更新，请重新提出修改')
        edits=plan['edits'];index=plan['index'];resolved=plan['resolved']+[dict(edits[index],id=item['id'],revision=item['revision'])]
        for spec in edits[index+1:]:
            found=candidates(page,spec['target'])
            if len(found)!=1:raise TodoError('还有其他不明确的任务，请分别修改')
            resolved.append(dict(spec,id=found[0]['id'],revision=found[0]['revision']))
        return self.apply({**row,'body':target['body'],'created':target['created']},resolved)

    @staticmethod
    def minutes(value):
        if value in ('半小时','半个小时'): return 30
        if value in ('一个半小时','一小时半','1.5小时'): return 90
        from calendar_journal import _number as number
        m=re.fullmatch(r'([\d一二两三四五六七八九十百]+)(?:个)?(分钟|小时)',value)
        if not m: raise TodoError('请说具体预计用时，例如45分钟或一个半小时')
        n=number(m[1])*(60 if m[2]=='小时' else 1)
        if not 15<=n<=180:raise TodoError('单次用时支持15到180分钟；更长的任务请先拆分')
        return n

    @staticmethod
    def window(value, row):
        from deadline_intent import normalized_time
        value=normalized_time(value)
        b=json.loads(row['body']);zone=validate_zone(b.get('time_zone',DEFAULT_ZONE));now=dt.datetime.fromtimestamp(row['created'],zone)
        dates=list(DATE_RE.finditer(value))
        if len(dates)!=1:raise TodoError('请明确改到哪一天，例如明天下午；没有改变原安排')
        day=_date(dates[0][0],now.date())
        if day<now.date():raise TodoError('执行安排不能延期到过去')
        if re.search(r'\d[:：]|点|时\d',value):raise TodoError('改执行安排请说日期和时段；精确日程请提供开始及结束时间')
        low,high=(12,18) if '下午' in value else (11,12) if '上午' in value else (18,25) if re.search(r'晚上|晚间|今晚',value) else (11,25)
        base=dt.datetime.combine(day,dt.time())
        return tuple(_localize(base+dt.timedelta(hours=h),zone).timestamp() for h in (low,high))

    def apply(self, row, edits):
        owner=row['owner'];now=time.time();summary=[]
        with self.s.db() as c:
            c.execute('BEGIN IMMEDIATE')
            if not c.execute('SELECT 1 FROM devices WHERE token=?',(owner,)).fetchone():raise TodoError('配对已失效')
            prior=c.execute('SELECT payload FROM task_edit_receipts WHERE owner=? AND task=?',(owner,row['id'])).fetchone()
            if prior:return json.loads(prior['payload'])
            touched=set()
            for spec in edits:
                ident=spec['id'];op=spec['operation'];value=spec['value']
                t=c.execute('SELECT * FROM todos WHERE owner=? AND id=?',(owner,ident)).fetchone()
                if not t or (ident not in touched and t['revision']!=spec['revision']):raise TodoError('待办已更新，请重新提出修改')
                if op=='duration':
                    if t['kind']=='project':raise TodoError('请修改具体子任务的预计用时，避免把总用时误当成每项用时')
                    minutes=self.minutes(value)
                    c.execute('INSERT INTO todo_estimates VALUES(?,?,?) ON CONFLICT(owner,todo_id) DO UPDATE SET minutes=excluded.minutes',(owner,ident,minutes));detail='预计'+str(minutes)+'分钟'
                elif op=='postpone':
                    low,high=self.window(value,row)
                    c.execute('INSERT INTO todo_schedule_windows VALUES(?,?,?,?,0,?) ON CONFLICT(owner,todo_id) DO UPDATE SET not_before=excluded.not_before,not_after=excluded.not_after,updated=excluded.updated',(owner,ident,low,high,now));detail='改为'+value+'安排，截止日期不变'
                elif op in ('priority','pause','resume'):
                    c.execute('INSERT OR IGNORE INTO todo_priorities VALUES(?,?,0,0)',(owner,ident))
                    if op=='priority':
                        levels={'高':2,'紧急':2,'低':-1,'普通':0,'正常':0,'中':0}
                        found=[v for k,v in levels.items() if k in value]
                        if not found and value in ('优先','优先做'):found=[2]
                        if not found or len(set(found))!=1:raise TodoError('请指定高、普通或低优先级')
                        c.execute('UPDATE todo_priorities SET priority=? WHERE owner=? AND todo_id=?',(found[0],owner,ident));detail='优先级设为'+{2:'高',0:'普通',-1:'低'}[found[0]]
                    else:
                        c.execute('UPDATE todo_priorities SET paused=? WHERE owner=? AND todo_id=?',(int(op=='pause'),owner,ident));detail='暂停自动安排，待办保留' if op=='pause' else '恢复自动安排'
                elif op in ('complete','reopen'):
                    if c.execute('SELECT 1 FROM todos WHERE owner=? AND parent_id=?',(owner,ident)).fetchone():raise TodoError('项目进度按子任务计算，请逐项完成')
                    c.execute('UPDATE todos SET completed=? WHERE owner=? AND id=?',(int(op=='complete'),owner,ident));detail='标记完成' if op=='complete' else '恢复未完成'
                elif op in ('rename','deadline'):
                    if op=='rename':
                        value=TodoStore._clean({'title':value})['title'];field='title';detail='改名为'+value
                    else:
                        if not re.search(r'改|修改|更改|设为|调整|change|set',spec['quote'],re.I):raise TodoError('请明确要求修改截止日期')
                        b=json.loads(row['body']);zone=b.get('time_zone',DEFAULT_ZONE)
                        from deadline_intent import normalized_time
                        value=normalized_time(value)
                        dates=list(DATE_RE.finditer(value))
                        if len(dates)!=1:raise TodoError('请明确新的截止日期')
                        day=_date(dates[0][0],dt.datetime.fromtimestamp(row['created'],validate_zone(zone)).date())
                        if re.search(r'\d[:：]|点',value):
                            from reminder_time import parse_reminder_time
                            value=dt.datetime.fromtimestamp(parse_reminder_time(value,row['created'],zone),validate_zone(zone)).isoformat()
                        else:value=day.isoformat()
                        field='deadline';detail='截止改为'+value.replace('T',' ')[:16]
                        if c.execute('SELECT 1 FROM todo_sources WHERE owner=? AND todo_id=?',(owner,ident)).fetchone():detail+='（仅修改 PersonalOS，Canvas 原始截止保留）'
                    c.execute('UPDATE todos SET '+field+'=? WHERE owner=? AND id=?',(value,owner,ident))
                elif op=='split':
                    # Children are explicit labels, not unreviewed model inventions.
                    parts=re.split(r'拆成|拆分为|分解为',value,maxsplit=1)
                    if len(parts)!=2:raise TodoError('请说拆成哪些步骤，例如拆成查资料、写初稿、检查')
                    names=[x.strip(' ，。') for x in re.split(r'[、，,；;]|然后|再',parts[1]) if x.strip(' ，。')]
                    if not 2<=len(names)<=8:raise TodoError('请列出2到8个子任务')
                    if t['parent_id']:raise TodoError('当前只支持一层子任务')
                    for n,name in enumerate(names):
                        clean=TodoStore._clean({'title':name});child=str(uuid.uuid5(uuid.NAMESPACE_URL,owner+':edit:'+row['id']+':'+ident+':'+str(n)))
                        notes=t['notes'].split('\n')[0]+'\n所属项目：'+t['title']
                        c.execute("INSERT INTO todos(id,owner,parent_id,kind,title,notes,deadline,completed,revision,created,updated,source_task) VALUES(?,?,?,'task',?,?,NULL,0,1,?,?,?)",(child,owner,ident,clean['title'],notes,now,now,row['id']))
                    c.execute("UPDATE todos SET kind='project' WHERE owner=? AND id=?",(owner,ident));detail='拆为'+str(len(names))+'个子任务，保留项目截止'
                else:raise TodoError('不支持这项修改')
                c.execute('UPDATE todos SET revision=revision+1,updated=? WHERE owner=? AND id=?',(now,owner,ident));touched.add(ident)
                if op not in ('complete','reopen'):
                    c.execute("UPDATE auto_schedule SET state='reschedule',reason='待办已修改，等待重新核对空闲时间',updated=? WHERE owner=? AND todo_id=?",(now,owner,ident))
                    c.execute("UPDATE calendar_entries SET status='cancelled',revision=revision+1,device_sync='pending',last_op='auto_unscheduled',updated=? WHERE owner=? AND kind='plan' AND status='ready' AND id IN (SELECT entry_id FROM auto_schedule WHERE owner=? AND todo_id=?)",(now,owner,owner,ident))
                    if op in ('pause','split'):
                        c.execute("UPDATE auto_schedule SET state='paused',reason=?,updated=? WHERE owner=? AND todo_id=?",('已暂停安排' if op=='pause' else '已拆分，改为安排子任务',now,owner,ident))
                if op in ('postpone','duration','pause','complete','split'):
                    reminder=str(uuid.uuid5(uuid.NAMESPACE_URL,owner+':schedule-reminder:'+ident))
                    c.execute("UPDATE tasks SET state='cancelled',result='原安排已修改，旧提醒已取消' WHERE owner=? AND id=? AND state='scheduled'",(owner,reminder))
                if t['kind']=='project' and op in ('postpone','priority','pause','resume','deadline'):
                    for child in c.execute('SELECT id FROM todos WHERE owner=? AND parent_id=? AND completed=0',(owner,ident)).fetchall():
                        cid=child['id']
                        c.execute("UPDATE auto_schedule SET state=?,reason='项目安排已修改',updated=? WHERE owner=? AND todo_id=?",('paused' if op=='pause' else 'reschedule',now,owner,cid))
                        c.execute("UPDATE calendar_entries SET status='cancelled',revision=revision+1,device_sync='pending',last_op='auto_unscheduled',updated=? WHERE owner=? AND kind='plan' AND status='ready' AND id IN (SELECT entry_id FROM auto_schedule WHERE owner=? AND todo_id=?)",(now,owner,owner,cid))
                        if op=='postpone':
                            c.execute('INSERT INTO todo_schedule_windows VALUES(?,?,?,?,0,?) ON CONFLICT(owner,todo_id) DO UPDATE SET not_before=excluded.not_before,not_after=excluded.not_after,updated=excluded.updated',(owner,cid,low,high,now))
                        reminder=str(uuid.uuid5(uuid.NAMESPACE_URL,owner+':schedule-reminder:'+cid))
                        c.execute("UPDATE tasks SET state='cancelled',result='项目安排已修改，旧提醒已取消' WHERE owner=? AND id=? AND state='scheduled'",(owner,reminder))
                summary.append(t['title']+'：'+detail)
            result='已更新。'+'；'.join(summary)+'。日历变更等待手机同步。'
            revisions={ident:c.execute('SELECT revision FROM todos WHERE owner=? AND id=?',(owner,ident)).fetchone()[0] for ident in touched}
            outcome={'state':'completed','result':result,'plan':json.dumps({'kind':'task_edit','edited_ids':sorted(touched),'revisions':revisions,'calendar_sync':'pending_phone','spoken_reply':result},ensure_ascii=False)}
            c.execute('INSERT INTO task_edit_receipts VALUES(?,?,?)',(owner,row['id'],json.dumps(outcome,ensure_ascii=False)))
            self.s._audit(c,row['id'],'task_edit_applied',{'ids':sorted(touched),'operations':[x['operation'] for x in edits]})
        planning=self.s.planner.reconcile(owner)
        affected=set(touched)
        with self.s.db() as c:
            for ident in touched:
                affected.update(r['id'] for r in c.execute('SELECT id FROM todos WHERE owner=? AND parent_id=?',(owner,ident)))
        blocked=[x for x in planning['slots'] if x['todo_id'] in affected and x['state']=='unscheduled']
        plan=json.loads(outcome['plan'])
        if blocked:
            warning='修改已保存，但有'+str(len(blocked))+'项未排上：'+'；'.join(dict.fromkeys(x['reason'] for x in blocked))+'。'
            outcome['result']=warning+outcome['result'];plan['calendar_sync']='unscheduled'
            plan['spoken_reply']=warning
        elif not planning['settings']['enabled']:
            outcome['result']=outcome['result'].replace('日历变更等待手机同步。','自动排程未开启，待办修改已保存。')
            plan['calendar_sync']='not_requested';plan['spoken_reply']=outcome['result']
        elif not planning['context_fresh']:
            outcome['result']=outcome['result'].replace('日历变更等待手机同步。','等待手机刷新完整日历后核对空闲时间；日历变更仍需手机同步。')
            plan['calendar_sync']='waiting_context';plan['spoken_reply']=outcome['result']
        outcome['plan']=json.dumps(plan,ensure_ascii=False)
        with self.s.db() as c:c.execute('UPDATE task_edit_receipts SET payload=? WHERE owner=? AND task=?',(json.dumps(outcome,ensure_ascii=False),owner,row['id']))
        return outcome
