"""Owner-scoped, deterministic free-time allocation. Phone validates/writes EventKit."""
import datetime as dt
import hashlib
import json
import math
import re
import time
import uuid
from calendar_journal import validate_zone, _localize
from todos import TodoError
from deadline_intent import requested_window

DEFAULTS={'enabled':False,'start_minute':660,'end_minute':1500,'meal_minutes':45,'duration_minutes':45,'buffer_minutes':15,'daily_minutes':240}

def instant(value):
    if not isinstance(value,str) or len(value)>80: raise TodoError('排程时间格式无效')
    try:
        date=dt.datetime.fromisoformat(value.replace('Z','+00:00'))
        if date.tzinfo is None: raise ValueError()
        return date.timestamp()
    except (ValueError,OverflowError): raise TodoError('排程时间必须包含时区') from None

def stamp(value,zone): return dt.datetime.fromtimestamp(value,zone).isoformat()
def overlaps(a,b): return a[0]<b[1] and b[0]<a[1]
def free_slot(start,end,minutes,busy,preferred=None):
    duration=minutes*60
    candidates=[math.ceil(start/900)*900]
    if preferred is not None:candidates.append(max(candidates[0],preferred))
    candidates.extend(math.ceil(b[1]/900)*900 for b in busy if start<=b[1]<end)
    candidates.extend(math.floor((b[0]-duration)/900)*900 for b in busy if start<b[0]<=end)
    candidates.append(math.floor((end-duration)/900)*900)
    options=[x for x in set(candidates) if x>=start and x+duration<=end and not any(overlaps((x,x+duration),b) for b in busy)]
    return min(options,key=lambda x:(abs(x-preferred),x)) if options and preferred is not None else min(options) if options else None

class AutoPlanner:
    def __init__(self,store):self.store=store
    @staticmethod
    def initialize(c):
        c.executescript('''CREATE TABLE IF NOT EXISTS planning_settings(owner TEXT PRIMARY KEY,payload TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS planning_context(owner TEXT PRIMARY KEY,payload TEXT NOT NULL,received REAL NOT NULL);
        CREATE TABLE IF NOT EXISTS todo_estimates(owner TEXT NOT NULL,todo_id TEXT NOT NULL,minutes INTEGER NOT NULL,PRIMARY KEY(owner,todo_id));
        CREATE TABLE IF NOT EXISTS auto_schedule(owner TEXT NOT NULL,todo_id TEXT NOT NULL,entry_id TEXT UNIQUE NOT NULL,
        state TEXT NOT NULL,reason TEXT NOT NULL,updated REAL NOT NULL,PRIMARY KEY(owner,todo_id));
        CREATE TABLE IF NOT EXISTS planning_runs(owner TEXT PRIMARY KEY,payload TEXT NOT NULL);''')
        c.executescript('''CREATE TABLE IF NOT EXISTS todo_schedule_windows(owner TEXT NOT NULL,todo_id TEXT NOT NULL,not_before REAL NOT NULL,not_after REAL,remind INTEGER NOT NULL DEFAULT 0,updated REAL NOT NULL,PRIMARY KEY(owner,todo_id));
        CREATE TABLE IF NOT EXISTS schedule_adjustments(owner TEXT NOT NULL,task TEXT NOT NULL,result TEXT NOT NULL,PRIMARY KEY(owner,task));''')
    def settings(self,owner):
        with self.store.db() as c:r=c.execute('SELECT payload FROM planning_settings WHERE owner=?',(owner,)).fetchone()
        return {**DEFAULTS,**(json.loads(r['payload']) if r else {})}
    def configure(self,owner,body):
        if not isinstance(body,dict) or set(body)-set(DEFAULTS):raise TodoError('排程设置字段无效')
        value={**self.settings(owner),**body}
        if type(value['enabled']) is not bool or any(type(value[k]) is not int for k in DEFAULTS if k!='enabled'):raise TodoError('排程设置格式无效')
        if not(0<=value['start_minute']<1440 and value['start_minute']<value['end_minute']<=value['start_minute']+1440 and 15<=value['meal_minutes']<=90 and 15<=value['duration_minutes']<=180 and 0<=value['buffer_minutes']<=60 and 60<=value['daily_minutes']<=480):raise TodoError('排程时段或时长无效')
        with self.store.db() as c:c.execute('INSERT INTO planning_settings VALUES(?,?) ON CONFLICT(owner) DO UPDATE SET payload=excluded.payload',(owner,json.dumps(value)))
        self.store.audit('','planning_settings_changed',{'enabled':value['enabled'],'owner_scope':True})
        return self.status(owner)
    def context(self,owner,value,now=None):
        now=time.time() if now is None else now
        if not isinstance(value,dict) or set(value)!={'captured_at','time_zone','range_start','range_end','truncated','events'}:raise TodoError('排程日历快照字段不完整')
        if type(value['captured_at']) not in (int,float) or not math.isfinite(value['captured_at']) or not 0<=now-value['captured_at']<=120 or value['truncated'] is not False:raise TodoError('请刷新完整日历，过期或截断快照不会用于自动排程')
        zone=validate_zone(value['time_zone']);start,end=instant(value['range_start']),instant(value['range_end'])
        if not(start<=now<end and 6*86400<=end-start<=9*86400):raise TodoError('日历快照需要完整覆盖未来至少六天')
        events=value['events']
        if not isinstance(events,list) or len(events)>500:raise TodoError('排程日历事件过多，未假定剩余时间为空闲')
        for event in events:
            if not isinstance(event,dict) or set(event)-{'start','end','all_day','auto_id'} or not {'start','end','all_day'}<=set(event):raise TodoError('排程日历事件无效')
            if type(event['all_day']) is not bool or not start-86400<=instant(event['start'])<instant(event['end'])<=end+86400:raise TodoError('排程日历时间范围无效')
            if event.get('auto_id') and not re.fullmatch(r'[0-9a-f-]{36}',event['auto_id']):raise TodoError('日历关联编号无效')
        with self.store.db() as c:
            old=c.execute('SELECT payload FROM planning_context WHERE owner=?',(owner,)).fetchone()
            if old and json.loads(old['payload'])['captured_at']>value['captured_at']:raise TodoError('较旧的日历快照未覆盖新快照',409)
            c.execute('INSERT INTO planning_context VALUES(?,?,?) ON CONFLICT(owner) DO UPDATE SET payload=excluded.payload,received=excluded.received',(owner,json.dumps(value),now))
        return self.reconcile(owner,now)
    def estimate(self,owner,ident,minutes,reschedule=False):
        if type(minutes) is not int or not 15<=minutes<=180:raise TodoError('预计用时须为15到180分钟')
        with self.store.db() as c:
            if not c.execute('SELECT 1 FROM todos WHERE owner=? AND id=?',(owner,ident)).fetchone():raise TodoError('待办不存在',404)
            c.execute('INSERT INTO todo_estimates VALUES(?,?,?) ON CONFLICT(owner,todo_id) DO UPDATE SET minutes=excluded.minutes',(owner,ident,minutes))
            if reschedule:c.execute("UPDATE auto_schedule SET state='reschedule' WHERE owner=? AND todo_id=?",(owner,ident))
        return self.reconcile(owner)
    def links(self,owner):
        with self.store.db() as c:return {r['entry_id']:r['todo_id'] for r in c.execute('SELECT entry_id,todo_id FROM auto_schedule WHERE owner=?',(owner,))}
    def status(self,owner,now=None):
        now=time.time() if now is None else now
        with self.store.db() as c:
            context=c.execute('SELECT received FROM planning_context WHERE owner=?',(owner,)).fetchone()
            slots=[dict(r) for r in c.execute('SELECT a.todo_id,a.entry_id,a.state,a.reason,e.start,e.end,e.device_sync,e.revision FROM auto_schedule a LEFT JOIN calendar_entries e ON e.id=a.entry_id AND e.owner=a.owner WHERE a.owner=? ORDER BY e.start,a.todo_id',(owner,))]
            for slot in slots:
                if slot['state'] in ('unscheduled','cancelled','reschedule','paused'):slot['start']=slot['end']=None
            estimates={r['todo_id']:r['minutes'] for r in c.execute('SELECT todo_id,minutes FROM todo_estimates WHERE owner=?',(owner,))}
            run=c.execute('SELECT payload FROM planning_runs WHERE owner=?',(owner,)).fetchone()
        return {'settings':self.settings(owner),'context_fresh':bool(context and 0<=now-context['received']<=900),'slots':slots,'estimates':estimates,'calendar_write':'phone_foreground','details':json.loads(run['payload']) if run else {}}
    def reconcile(self,owner,now=None):
        now=time.time() if now is None else now
        from daily_rollover import DailyRollover
        daily=DailyRollover(self.store);daily.prepare(owner,now)
        # Completion is an explicit todo fact and does not require a fresh free-
        # time snapshot. It changes only kind/color, never inventing a new slot.
        with self.store.db() as c:
            pairs=c.execute('SELECT a.entry_id,a.state,t.completed,t.title AS todo_title,e.title AS event_title,e.kind,e.start FROM auto_schedule a JOIN todos t ON t.id=a.todo_id AND t.owner=a.owner JOIN calendar_entries e ON e.id=a.entry_id AND e.owner=a.owner WHERE a.owner=?',(owner,)).fetchall()
            for pair in pairs:
                if pair['todo_title']!=pair['event_title']:c.execute("UPDATE calendar_entries SET title=?,revision=revision+1,device_sync='pending',updated=? WHERE id=? AND owner=?",(pair['todo_title'],now,pair['entry_id'],owner))
                kind='done' if pair['completed'] else 'plan'
                if pair['kind']!=kind:c.execute("UPDATE calendar_entries SET kind=?,revision=revision+1,device_sync='pending',updated=? WHERE id=? AND owner=?",(kind,now,pair['entry_id'],owner))
                if pair['completed']:c.execute("UPDATE auto_schedule SET state='completed',reason='',updated=? WHERE entry_id=? AND owner=?",(now,pair['entry_id'],owner))
                elif pair['kind']=='done':c.execute("UPDATE auto_schedule SET state=?,reason='',updated=? WHERE entry_id=? AND owner=?",('started' if pair['start'] and instant(pair['start'])<=now else 'scheduled',now,pair['entry_id'],owner))
        settings=self.settings(owner)
        with self.store.db() as c:
            snap=c.execute('SELECT payload,received FROM planning_context WHERE owner=?',(owner,)).fetchone()
        if not settings['enabled'] or not snap or not 0<=now-snap['received']<=900:
            daily.finalize(owner,now)
            return self.status(owner,now)
        context=json.loads(snap['payload']);zone=validate_zone(context['time_zone']);limit=instant(context['range_end'])
        with self.store.db() as c:
            c.execute('BEGIN IMMEDIATE')
            todos=[dict(r) for r in c.execute('SELECT * FROM todos WHERE owner=? ORDER BY seq LIMIT 501',(owner,))]
            if len(todos)>500:raise TodoError('待办超过500项，请先整理；未用不完整列表排程')
            parents={r['id']:r for r in todos};parent_ids={r['parent_id'] for r in todos if r['parent_id']}
            old={r['todo_id']:dict(r) for r in c.execute('SELECT a.*,e.start,e.end,e.status,e.kind,e.device_sync,e.sync_error,e.title,e.revision,e.last_op FROM auto_schedule a LEFT JOIN calendar_entries e ON e.id=a.entry_id AND e.owner=a.owner WHERE a.owner=?',(owner,))}
            estimates={r['todo_id']:r['minutes'] for r in c.execute('SELECT * FROM todo_estimates WHERE owner=?',(owner,))}
            constraints={r['todo_id']:dict(r) for r in c.execute('SELECT * FROM todo_schedule_windows WHERE owner=?',(owner,))}
            rollover_ids={r['todo_id'] for r in c.execute('SELECT todo_id FROM daily_rollover_items WHERE owner=?',(owner,))}
            priorities={r['todo_id']:dict(r) for r in c.execute('SELECT * FROM todo_priorities WHERE owner=?',(owner,))}
            byentry={v['entry_id']:v for v in old.values()}
            busy=[];present=set();locked=set()
            counts={ident:sum(1 for e in context['events'] if e.get('auto_id')==ident) for ident in byentry}
            for event in context['events']:
                pair=(instant(event['start']),instant(event['end']));slot=byentry.get(event.get('auto_id'))
                if slot and counts[slot['entry_id']]==1 and slot['start'] and abs(instant(slot['start'])-pair[0])<1 and abs(instant(slot['end'])-pair[1])<1:
                    present.add(slot['todo_id']);continue
                if slot:locked.add(slot['todo_id'])
                busy.append((pair[0]-settings['buffer_minutes']*60,pair[1]+settings['buffer_minutes']*60))
            day=dt.datetime.fromtimestamp(now,zone).date()
            if dt.datetime.fromtimestamp(now,zone).hour<max(0,settings['end_minute']-1440)/60:day-=dt.timedelta(days=1)
            windows=[];daily_used={};meals=[]
            for n in range(8):
                naive=dt.datetime.combine(day+dt.timedelta(days=n),dt.time())
                minute=lambda m:_localize(naive+dt.timedelta(minutes=m),zone).timestamp()
                low,high=minute(settings['start_minute']),min(limit,minute(settings['end_minute']))
                if high<=now or low>=limit:continue
                for a,b,preferred in [(11,15,13),(18,21,19)]:
                    meal=free_slot(max(minute(a*60),low),min(minute(b*60),high),settings['meal_minutes'],busy,minute(preferred*60))
                    if meal is not None:
                        pair=(meal,meal+settings['meal_minutes']*60);busy.append(pair);meals.append({'start':stamp(pair[0],zone),'end':stamp(pair[1],zone),'kind':'lunch' if a==11 else 'dinner'})
                    elif minute(b*60)>now:
                        # A fully occupied meal window must not silently become task time.
                        busy.append((max(minute(a*60),low),min(minute(b*60),high)))
                windows.append((low,high,n));daily_used[n]=0
            def due(todo):
                values=[todo['deadline']]
                if todo['parent_id'] in parents:values.append(parents[todo['parent_id']]['deadline'])
                points=[]
                for value in values:
                    if value:
                        points.append(_localize(dt.datetime.combine(dt.date.fromisoformat(value)+dt.timedelta(days=1),dt.time()),zone).timestamp() if len(value)==10 else instant(value))
                return min(points) if points else float('inf')
            selected=[r for r in todos if r['id'] not in parent_ids and r['kind']!='project' and not priorities.get(r['id'],{}).get('paused') and not priorities.get(r['parent_id'],{}).get('paused')]
            selected.sort(key=lambda r:(-priorities.get(r['id'],priorities.get(r['parent_id'],{})).get('priority',0),due(r),r['created'],r['id']))
            for todo in selected:
                ident=todo['id'];prior=old.get(ident);entry_id=prior['entry_id'] if prior else str(uuid.uuid5(uuid.NAMESPACE_URL,owner+':auto-plan:'+ident))
                minutes=estimates.get(ident,settings['duration_minutes']);reason='';pair=None
                source=c.execute('SELECT created FROM tasks WHERE id=? AND owner=?',(todo['source_task'],owner)).fetchone()
                hint=requested_window(todo['notes'].removeprefix('原始要求：'),source['created'] if source else todo['created'],context['time_zone']) if todo['notes'].startswith('原始要求：') else None
                task_windows=[(max(low,hint[0]),min(high,hint[1]),n) for low,high,n in windows] if hint else windows
                rule=constraints.get(ident) or constraints.get(todo['parent_id'])
                if rule:task_windows=[(max(low,rule['not_before']),min(high,rule['not_after'] or float('inf')),n) for low,high,n in windows]
                if todo['completed']:
                    if prior and prior['start']:
                        if prior['kind']!='done':c.execute("UPDATE calendar_entries SET kind='done',revision=revision+1,device_sync='pending',updated=? WHERE id=? AND owner=?",(now,entry_id,owner))
                        c.execute("UPDATE auto_schedule SET state='completed',reason='',updated=? WHERE owner=? AND todo_id=?",(now,owner,ident))
                    continue
                if prior and prior['state']!='reschedule' and (ident in locked or (prior['device_sync']=='error' and not prior['sync_error'].startswith('自动排程冲突'))):
                    reason='日历记录有手动修改或同步问题，保留原记录';state='manual'
                elif prior and prior['last_op']=='resolve' and prior['state']!='reschedule':
                    pair=(instant(prior['start']),instant(prior['end']));reason='保留你手动指定的时段';state='manual'
                elif prior and prior['status']=='cancelled' and prior['last_op']!='auto_unscheduled' and prior['state']!='reschedule':reason='你已取消这条安排，保持取消';state='cancelled'
                elif prior and prior['start'] and (prior['device_sync']=='written' or prior['state']=='started') and instant(prior['start'])<=now and prior['state']!='reschedule':
                    pair=(instant(prior['start']),instant(prior['end']));reason='原安排已开始或结束；请勾选完成，或选择重新安排';state='started'
                else:
                    state='unscheduled';deadline=due(todo)
                    if ident in rollover_ids and deadline<now:deadline=float('inf') # overdue work, never a changed due date
                    if prior and prior['start'] and prior['state']!='reschedule':
                        candidate=(instant(prior['start']),instant(prior['end']))
                        if prior['device_sync']=='written' and ident not in present:reason='原日历记录未找到，保留记录以免重复';state='manual'
                        elif candidate[1]-candidate[0]==minutes*60 and candidate[1]<=deadline and candidate[0]>=now+300 and any(low<=candidate[0] and candidate[1]<=high and daily_used[n]+minutes<=settings['daily_minutes'] for low,high,n in task_windows) and not any(overlaps(candidate,b) for b in busy):pair=candidate
                    if pair is None and state!='manual':
                        for low,high,n in task_windows:
                            if daily_used[n]+minutes>settings['daily_minutes']:continue
                            point=free_slot(max(low,now+900),min(high,deadline),minutes,busy)
                            if point is not None:pair=(point,point+minutes*60);break
                    if pair is None and not reason:reason='所说时段内暂无足够空档' if hint else '截止前暂无足够空档' if deadline<float('inf') else '未来七天暂无足够空档'
                if pair is not None:
                    busy.append((pair[0]-600,pair[1]+600))
                    for low,high,n in windows:
                        if low<=pair[0]<high:daily_used[n]+=minutes
                    if state not in ('started','manual'):state='scheduled';reason='按截止日期优先，避开课程、其他日程和吃饭时间；预计'+str(minutes)+'分钟'
                    if ident in rollover_ids and due(todo)<now:reason+='；原截止已过，这是补做时段，截止日期未改变'
                    start,end=stamp(pair[0],zone),stamp(pair[1],zone)
                    event_notes=todo['notes'] if todo['notes'].startswith('PersonalOS 恢复训练\n') else '自动安排待办：'+todo['title']
                    if not prior or not prior['start']:
                        c.execute('INSERT INTO calendar_entries(id,owner,kind,title,start,end,time_zone,all_day,status,revision,question,device_sync,sync_error,original_text,created,updated,last_op,last_hash,previous_revision) VALUES(?,?,?,?,?,?,?,0,\'ready\',1,\'\',\'pending\',\'\',?,?,?,\'auto_plan\',\'\',0)',(entry_id,owner,'plan',todo['title'],start,end,context['time_zone'],event_notes,now,now))
                    elif (prior['start'],prior['end'],prior['title'],prior['kind'],prior['status'])!=(start,end,todo['title'],'plan','ready'):
                        c.execute("UPDATE calendar_entries SET start=?,end=?,title=?,kind='plan',status='ready',revision=revision+1,device_sync='pending',sync_error='',last_op=?,updated=? WHERE id=? AND owner=?",(start,end,todo['title'],'resolve' if state=='manual' else 'auto_plan',now,entry_id,owner))
                    if todo['notes'].startswith('PersonalOS 恢复训练\n'):
                        c.execute("UPDATE calendar_entries SET original_text=?,revision=revision+1,device_sync='pending',updated=? WHERE id=? AND owner=? AND original_text!=?",(event_notes,now,entry_id,owner,event_notes))
                    if rule and rule['remind'] and pair[0]>now:
                        reminder=str(uuid.uuid5(uuid.NAMESPACE_URL,owner+':schedule-reminder:'+ident))
                        existing=c.execute('SELECT state FROM tasks WHERE id=? AND owner=?',(reminder,owner)).fetchone()
                        if not existing:
                            c.execute('INSERT INTO tasks(id,body,state,result,plan,created,due,owner) VALUES(?,?,?,?,?,?,?,?)',(reminder,json.dumps({'text':'提醒完成：'+todo['title']},ensure_ascii=False),'scheduled','将在安排开始时提醒你完成这项任务。',json.dumps({'kind':'reminder','strategy':'user_reschedule','todo_id':ident}),now,pair[0],owner))
                        elif existing['state'] in ('scheduled','cancelled'):c.execute("UPDATE tasks SET state='scheduled',due=? WHERE id=? AND owner=?",(pair[0],reminder,owner))
                elif state=='unscheduled' and prior and prior['start'] and prior['status']=='ready':
                    # A new busy event may remove the only feasible future slot.
                    # Retract this owned automatic block rather than leaving a
                    # knowingly conflicting block in the phone's calendar.
                    c.execute("UPDATE calendar_entries SET status='cancelled',revision=revision+1,device_sync='pending',sync_error='',last_op='auto_unscheduled',updated=? WHERE id=? AND owner=?",(now,entry_id,owner))
                c.execute('INSERT INTO auto_schedule VALUES(?,?,?,?,?,?) ON CONFLICT(owner,todo_id) DO UPDATE SET state=excluded.state,reason=excluded.reason,updated=excluded.updated',(owner,ident,entry_id,state,reason,now))
            self.store._audit(c,'','automatic_planning',{'items_considered':len(selected),'meal_reservations':len(meals),'calendar_write':'pending_phone'})
            c.execute('INSERT INTO planning_runs VALUES(?,?) ON CONFLICT(owner) DO UPDATE SET payload=excluded.payload',(owner,json.dumps({'meals':meals,'last_run':now,'window':'11:00–次日01:00'})))
        daily.finalize(owner,now)
        return self.status(owner,now)
    def run(self,stop):
        while not stop.wait(30):
            with self.store.db() as c:owners=[r['owner'] for r in c.execute('SELECT p.owner FROM planning_settings p JOIN devices d ON d.token=p.owner')]
            for owner in owners:
                try:self.reconcile(owner)
                except Exception:pass  # No partial schedule is committed; next phone sync exposes status.
