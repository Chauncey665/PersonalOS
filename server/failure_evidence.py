"""Explain execution failures from owned receipts, without speculative cloud/network causes."""
import json,re

def requested(text):
    return bool(re.search(r'为什么|为何|什么原因|怎么回事|原因是什么',text) and re.search(r'刚才|刚刚|上一|本地模型|上个|这个任务',text) and re.search(r'无法|失败|没.{0,8}(?:完成|成功|执行)|不能|超时',text))

def run(store,row):
    body=json.loads(row['body'])
    if not requested(body['text']) or body.get('document_ids'):return None
    owner=row.get('owner','');sid=body.get('session_id')
    with store.db() as c:
        if not c.execute('SELECT 1 FROM devices WHERE token=?',(owner,)).fetchone():return None
        candidate=c.execute('SELECT * FROM tasks WHERE id=? AND owner=? AND id<>?',(body.get('reply_to',''),owner,row['id'])).fetchone()
        if candidate is None and sid:
            candidate=c.execute("SELECT t.* FROM tasks t JOIN assistant_turns a ON a.task=t.id WHERE a.owner=? AND a.session=? AND t.id<>? AND t.created<? AND t.state NOT IN ('queued','running') ORDER BY t.created DESC LIMIT 1",(owner,sid,row['id'],row['created'])).fetchone()
        if candidate is None:
            answer='没有找到这轮对话对应的执行记录，不能判断失败原因。请说明是哪条任务。'
            return {'state':'needs_details','result':answer,'plan':json.dumps({'failure_diagnosis':True,'spoken_reply':answer},ensure_ascii=False)}
        candidate=dict(candidate);ids=[candidate['id']];p=json.loads(candidate['plan'] or '{}')
        if p.get('workflow'):
            w=c.execute('SELECT data FROM assistant_workflows WHERE id=? AND owner=?',(p['workflow'],owner)).fetchone()
            if w:
                for step in json.loads(w['data']).get('steps',[]):
                    ident=step.get('task')
                    if ident and c.execute('SELECT 1 FROM tasks WHERE id=? AND owner=?',(ident,owner)).fetchone():ids.append(ident)
        audits=[]
        for ident in ids:
            audits.extend(dict(x) for x in c.execute("SELECT event,detail FROM audit WHERE task=? AND event IN ('brain_model_error','brain_tool_result','backlog_reschedule_applied') ORDER BY id",(ident,)))
    failures=[];tool_fail=[];writes=False
    for item in audits:
        try:d=json.loads(item['detail'])
        except (ValueError,TypeError):continue
        if not isinstance(d,dict):continue
        if item['event']=='brain_model_error':failures.append(d)
        if item['event']=='brain_tool_result' and d.get('ok') is False:tool_fail.append(d.get('tool','工具'))
        if item['event']=='backlog_reschedule_applied':writes=True
    if failures:
        e=failures[-1]
        if 'timeout' in str(e.get('type','')).lower():answer='日志确认：本地模型等待超时，这轮处理约'+str(round(e.get('elapsed_seconds',0)))+'秒后停止。'
        else:answer='日志确认：本地模型请求失败，错误类型为'+str(e.get('type','未知'))+'。具体根因尚未证实。'
    elif tool_fail:answer='日志确认这轮有工具调用失败，不能把随后生成的文字当成实际执行。'
    else:answer='没有查到这条任务的模型超时证据，不能据此推断网络或模型故障。'
    if tool_fail:answer+='失败工具：'+'、'.join(dict.fromkeys(tool_fail))+'。'
    answer+='系统使用本机模型，本次没有切换到云端；日志也不足以认定是网络波动。'
    if writes:answer+='这条重排指令随后已修复并记录，最新日历状态请以同步回执为准。'
    elif not p.get('todo_ids') and not p.get('edited_ids'):answer+='该记录未提供待办修改回执，不能声称已经移动任务。'
    return {'state':'completed','result':answer,'plan':json.dumps({'failure_diagnosis':True,'referenced_task':candidate['id'],'spoken_reply':answer},ensure_ascii=False)}
