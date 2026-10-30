"""Business progress from owned durable receipts, independent of worker completion."""
import json
import re
import time

def unpack(value):
    try:return json.loads(value or '{}')
    except (ValueError,TypeError):return {}

def outcome(status,label,detail='',pending=False):
    return {'status':status,'label':label,'detail':detail or label,'pending':pending}

def describe(store,row,depth=0):
    state=row['state'];p=unpack(row['plan']);owner=row.get('owner','')
    if state in ('queued','running'):return outcome('processing','正在处理',pending=True)
    if state=='awaiting_confirmation':return outcome('waiting_confirmation','等你确认',row['result'],True)
    if state=='needs_details' and p.get('kind')=='daily_rollover':return outcome('waiting_calendar','自动重排待同步',row['result'],True)
    if state=='needs_details' and p.get('fitness_waiting_context'):return outcome('waiting_calendar_context','等待日历刷新','已保存训练安排请求；手机刷新后自动继续，尚未写入日历。',True)
    if state=='needs_details':return outcome('waiting_phone' if p.get('status')=='phone_action' else 'waiting_input','等待手机操作' if p.get('status')=='phone_action' else '待补充',row['result'],True)
    if state in ('failed','needs_review'):return outcome('failed','未能完成',row['result'],True)
    # Aggregate only real child tasks owned by this device. A completed worker
    # step may still be waiting for EventKit and must not become "all done".
    if p.get('workflow') and depth<2:
        with store.db() as c:w=c.execute('SELECT data FROM assistant_workflows WHERE id=? AND owner=?',(p['workflow'],owner)).fetchone()
        if w:
            results=[]
            for step in unpack(w['data']).get('steps',[]):
                if not step.get('task'):continue
                with store.db() as c:t=c.execute('SELECT * FROM tasks WHERE id=? AND owner=?',(step['task'],owner)).fetchone()
                if t:results.append(describe(store,dict(t),depth+1))
            waiting=[x for x in results if x['pending']]
            if waiting:return outcome(waiting[0]['status'],waiting[0]['label'],'；'.join(dict.fromkeys(x['detail'] for x in waiting)),True)
            if len(results)==1:return results[0]
            if results:return outcome('processed','已处理','各步骤已有处理结果；待办本身是否完成以你的勾选为准。')
    with store.db() as c:
        entry_id=p.get('calendar_task_id') or p.get('calendar_entry',{}).get('id') or row['id']
        entries=[dict(x) for x in c.execute('SELECT * FROM calendar_entries WHERE owner=? AND id=?',(owner,entry_id))]
        ids=p.get('edited_ids') or p.get('todo_ids') or []
        if p.get('kind')=='schedule_adjustment':
            audit=c.execute("SELECT detail FROM audit WHERE task=? AND event='schedule_adjustment_applied' ORDER BY id DESC LIMIT 1",(row['id'],)).fetchone()
            if audit:
                a=unpack(audit['detail']);ids=a.get('focus',[])+a.get('deferred',[])
        slots=[];todos=[];deadline_pending=False;deadline_synced=False
        for ident in ids[:100]:
            t=c.execute('SELECT * FROM todos WHERE id=? AND owner=?',(ident,owner)).fetchone()
            if not t:continue
            item=store.todos.public(t,store.todos._progress(c,owner));todos.append(item)
            receipt=c.execute('SELECT * FROM todo_calendar_receipts WHERE owner=? AND todo_id=?',(owner,ident)).fetchone()
            if item['deadline'] or receipt:
                valid_receipt=receipt and receipt['revision']==item['revision'] and receipt['fingerprint']==store.todos.calendar_fingerprint(item) and receipt['status'] in (('written',) if item['deadline'] else ('removed','not_required'))
                if not valid_receipt:deadline_pending=True
                elif item['deadline']:deadline_synced=True
            children=[x['id'] for x in c.execute('SELECT id FROM todos WHERE parent_id=? AND owner=?',(ident,owner))]
            for tid in [ident]+children:
                slot=c.execute('SELECT * FROM auto_schedule WHERE todo_id=? AND owner=?',(tid,owner)).fetchone()
                if slot:
                    slots.append(dict(slot));entries.extend(dict(x) for x in c.execute('SELECT * FROM calendar_entries WHERE id=? AND owner=?',(slot['entry_id'],owner)))
    if any(e['device_sync']=='error' for e in entries):return outcome('sync_error','同步需检查','Mac 已保存，但手机报告日历同步失败；尚未确认写入。',True)
    if any(e['status']=='needs_details' for e in entries):return outcome('waiting_input','待补充','日程仍缺少必要信息，尚未写入日历。',True)
    blocked=[s for s in slots if s['state'] in ('unscheduled','reschedule','manual')]
    if blocked:return outcome('unscheduled','尚未排定','待办已记录，尚未排定：'+'；'.join(dict.fromkeys(s['reason'] for s in blocked)),True)
    if deadline_pending:return outcome('waiting_calendar','待截止日历同步','待办已保存，尚未收到手机对当前截止标记和完成状态的同步回执。',True)
    if entries:
        valid=lambda e:e['device_sync']==('removed' if e['status']=='cancelled' else 'written')
        if not all(valid(e) for e in entries):return outcome('waiting_calendar','待日历同步','Mac 已保存变更，尚未收到手机对当前版本的日历同步回执。',True)
        if all(e['status']=='cancelled' for e in entries):return outcome('calendar_removed','已移除安排','手机已确认移除对应的日历安排。')
        if todos and all(t['completed'] for t in todos):return outcome('calendar_synced','完成标记已同步','已按你的要求标记完成，手机已确认日历同步。')
        return outcome('calendar_synced','已同步日历','手机已确认当前版本写入 Apple 日历；日程已安排不代表事情已经做完。')
    if deadline_synced:return outcome('calendar_synced','截止已同步','手机已确认当前截止标记；截止日期不代表执行时段或任务已完成。')
    if todos:return outcome('recorded','已记录待办','待办修改已保存；没有已确认写入手机日历的执行时段。')
    if state=='scheduled':return outcome('reminder_set','已设置提醒','提醒时间已设置，尚未触发。',True)
    if state in ('cancelled','undone'):return outcome(state,'已取消' if state=='cancelled' else '已撤销')
    if p.get('kind')=='reminder' and state=='completed':return outcome('reminder_due','提醒已到时','提醒已触发，不代表你已完成这件事。')
    if p.get('live_query'):
        if p.get('live_error'):return outcome('query_failed','未查到实时数据',row['result'])
        if time.time()-p.get('fetched_at_epoch',0)>900:return outcome('historical_answer','历史查询','这是之前的查询结果，不能代表现在；请重新查询。')
        return outcome('answered','已核对资料')
    return outcome('processed','已处理','处理结果已返回；不代表待办已勾选完成。')

def public_task(store,row):
    from spoken_reply import task_speech
    result={k:v for k,v in row.items() if k!='owner'};p=unpack(row['plan']);o=describe(store,row)
    if p.get('calendar_entry',{}).get('id'):
        with store.db() as c:e=c.execute('SELECT * FROM calendar_entries WHERE id=? AND owner=?',(p['calendar_entry']['id'],row.get('owner',''))).fetchone()
        if e:p['calendar_entry']=store.calendar.public(e)
    p['outcome']=o;result['plan']=json.dumps(p,ensure_ascii=False)
    result['speech']=o['detail'] if o['status'] in ('waiting_calendar','unscheduled','sync_error','calendar_synced','historical_answer','query_failed','calendar_removed') else task_speech(row)
    return result

def status_requested(text):
    return bool(re.search(r'完成了吗|做好了吗|处理好了吗|办好了吗|同步了吗|写入了吗|同步成功了吗|进度怎么样|任务状态|刚才那条怎么样',text)) and not re.search(r'改成|取消|解释|什么意思',text)

def status_query(store,row):
    text=unpack(row['body']).get('text','')
    if not status_requested(text):return None
    owner=row.get('owner','');sid=unpack(row['body']).get('session_id')
    if not owner:return None
    if re.search(r'完成了吗|做好了吗',text) and '日历' not in text:
        from task_entities import candidates
        query=re.sub(r'(?:已经|已|标记|勾选)?(?:完成了吗|做好了吗)[？?。！! ]*$','',text).strip()
        matches=candidates(store.todos.list(owner)['items'],query)
        if len(matches)==1:
            item=matches[0];answer=item['title']+('已标记完成。' if item['completed'] else '还没有标记完成；排入日历或提醒到时都不算做完。')
            return {'state':'completed','result':answer,'plan':json.dumps({'status_query':True,'spoken_reply':answer},ensure_ascii=False)}
        if len(matches)>1:return {'state':'needs_details','result':'同一课程有多项待办，请说明任务名称：'+'、'.join(x['title'] for x in matches[:5]),'plan':'{}'}
        if re.search(r'[零〇一二两三四五六七八九\d]{4}',query):return {'state':'needs_details','result':'没有找到对应待办，请说完整任务名。','plan':'{}'}
    with store.db() as c:
        if sid:rows=c.execute('SELECT t.* FROM tasks t JOIN assistant_turns a ON a.task=t.id WHERE a.session=? AND a.owner=? AND t.id<>? ORDER BY a.created DESC,a.rowid DESC LIMIT 15',(sid,owner,row['id'])).fetchall()
        else:rows=c.execute("SELECT * FROM tasks WHERE owner=? AND id<>? AND NOT EXISTS(SELECT 1 FROM assistant_turns a JOIN assistant_internal_sessions i ON i.session=a.session WHERE a.task=tasks.id) ORDER BY created DESC LIMIT 15",(owner,row['id'])).fetchall()
    for candidate in rows:
        candidate=dict(candidate);p=unpack(candidate['plan'])
        if p.get('status_query'):continue
        o=describe(store,candidate)
        if '日历' in text and o['status'] not in ('waiting_calendar','unscheduled','sync_error','calendar_synced','calendar_removed'):continue
        return {'state':'completed','result':o['detail'],'plan':json.dumps({'status_query':True,'referenced_task':candidate['id'],'spoken_reply':o['detail']},ensure_ascii=False)}
    return {'state':'needs_details','result':'当前对话里没有可核对的对应记录，请说明是哪项任务。','plan':'{}'}
