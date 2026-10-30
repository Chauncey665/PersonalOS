"""Opt-in daily rollover with durable runs, conservative training shifts and receipt-backed digests."""
import datetime as dt
import json
import re
import time
import uuid
from calendar_journal import DEFAULT_ZONE, validate_zone
from auto_planner import instant

class DailyRollover:
    def __init__(self,store):self.s=store
    @staticmethod
    def initialize(c):
        c.executescript('''CREATE TABLE IF NOT EXISTS daily_rollover_policy(owner TEXT PRIMARY KEY,enabled INTEGER NOT NULL,next_run REAL NOT NULL,time_zone TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS daily_rollover_runs(owner TEXT NOT NULL,day TEXT NOT NULL,task TEXT NOT NULL,payload TEXT NOT NULL,notified INTEGER NOT NULL DEFAULT 0,PRIMARY KEY(owner,day));
        CREATE TABLE IF NOT EXISTS daily_rollover_items(owner TEXT NOT NULL,todo_id TEXT NOT NULL,day TEXT NOT NULL,PRIMARY KEY(owner,todo_id));''')
    def configure(self,owner,enabled,now=None):
        if type(enabled) is not bool:raise ValueError('自动重排开关无效')
        now=time.time() if now is None else now
        with self.s.db() as c:
            if not c.execute('SELECT 1 FROM devices WHERE token=?',(owner,)).fetchone():raise ValueError('需要设备认证')
            snap=c.execute('SELECT payload FROM planning_context WHERE owner=?',(owner,)).fetchone()
            zone_name=json.loads(snap['payload'])['time_zone'] if snap else DEFAULT_ZONE;zone=validate_zone(zone_name)
            today=dt.datetime.fromtimestamp(now,zone).date();next_run=dt.datetime.combine(today+dt.timedelta(days=1),dt.time(1,5),zone).timestamp()
            c.execute('INSERT INTO daily_rollover_policy VALUES(?,?,?,?) ON CONFLICT(owner) DO UPDATE SET enabled=excluded.enabled,next_run=CASE WHEN daily_rollover_policy.enabled=excluded.enabled THEN daily_rollover_policy.next_run ELSE excluded.next_run END,time_zone=excluded.time_zone',(owner,int(enabled),next_run,zone_name))
            if not enabled:
                c.execute("UPDATE notification_outbox SET state='cancelled' WHERE device=? AND state IN ('pending','retry') AND task IN (SELECT task FROM daily_rollover_runs WHERE owner=?)",(owner,owner))
            self.s._audit(c,'','daily_rollover_policy_changed',{'enabled':enabled,'owner_scoped':True})
        return self.status(owner)
    def status(self,owner):
        with self.s.db() as c:
            p=c.execute('SELECT enabled,next_run,time_zone FROM daily_rollover_policy WHERE owner=?',(owner,)).fetchone()
            last=c.execute('SELECT day,task,notified FROM daily_rollover_runs WHERE owner=? ORDER BY day DESC LIMIT 1',(owner,)).fetchone()
        return {'enabled':bool(p and p['enabled']),'next_run':p['next_run'] if p and p['enabled'] else None,'check_time':'01:05','notification_not_before':'10:30','time_zone':p['time_zone'] if p else DEFAULT_ZONE,'latest_run':dict(last) if last else None,'requires_fresh_calendar':True,'phone_write_requires_receipt':True}
    def command(self,row):
        text=json.loads(row['body'])['text']
        if not re.search(r'自动.{0,5}重排|每天.{0,8}(?:重排|重新安排)',text):return None
        if re.search(r'为什么|怎么|如何|假设|翻译',text):return None
        if re.search(r'关闭|取消|停止|不要|先别',text):enabled=False
        elif re.search(r'开启|打开|启用',text):enabled=True
        else:return None
        status=self.configure(row['owner'],enabled)
        answer='已开启每天自动重排；01:05检查，10:30后按手机同步结果发送一次摘要。' if enabled else '已关闭每天自动重排，已有日历安排保留。'
        return {'state':'completed','result':answer,'plan':json.dumps({'daily_rollover_policy':status,'spoken_reply':answer},ensure_ascii=False)}
    def prepare(self,owner,now):
        settings=self.s.planner.settings(owner)
        if not settings['enabled']:return
        with self.s.db() as c:
            c.execute('BEGIN IMMEDIATE')
            p=c.execute('SELECT * FROM daily_rollover_policy WHERE owner=?',(owner,)).fetchone()
            if not p or not p['enabled'] or now<p['next_run']:return
            if not c.execute('SELECT 1 FROM devices WHERE token=?',(owner,)).fetchone():return
            zone=validate_zone(p['time_zone']);local=dt.datetime.fromtimestamp(now,zone)
            day=local.date() if local.time()>=dt.time(1,5) else local.date()-dt.timedelta(days=1)
            cutoff=dt.datetime.combine(day,dt.time(1),zone).timestamp()
            next_run=dt.datetime.combine(day+dt.timedelta(days=1),dt.time(1,5),zone).timestamp()
            c.execute('UPDATE daily_rollover_policy SET next_run=? WHERE owner=?',(next_run,owner))
            if c.execute('SELECT 1 FROM daily_rollover_runs WHERE owner=? AND day=?',(owner,day.isoformat())).fetchone():return
            rows=[dict(x) for x in c.execute("SELECT t.id,t.title,t.notes,t.deadline,t.completed,a.entry_id,a.state,e.start,e.end,e.status,e.last_op,COALESCE(q.paused,0) AS paused,COALESCE(pq.paused,0) AS parent_paused FROM todos t JOIN auto_schedule a ON a.owner=t.owner AND a.todo_id=t.id LEFT JOIN calendar_entries e ON e.id=a.entry_id AND e.owner=t.owner LEFT JOIN todo_priorities q ON q.owner=t.owner AND q.todo_id=t.id LEFT JOIN todo_priorities pq ON pq.owner=t.owner AND pq.todo_id=t.parent_id WHERE t.owner=? AND t.kind='task' AND t.completed=0 ORDER BY e.start LIMIT 501",(owner,))]
            if len(rows)>500:raise ValueError('待办超过500项，未进行不完整的每日重排')
            eligible=[x for x in rows if x['end'] and instant(x['end'])<=cutoff and not x['paused'] and not x['parent_paused'] and x['state'] not in ('manual','cancelled','paused') and x['last_op']!='resolve' and (x['status']=='ready' or x['last_op']=='auto_unscheduled')]
            changed=[];training_notes=[];plan_row=c.execute('SELECT payload FROM fitness_plans WHERE owner=?',(owner,)).fetchone()
            plan=json.loads(plan_row['payload']) if plan_row else None
            fitness_ids={x.get('todo_id') for x in plan['sessions']} if plan and plan.get('committed') else set()
            # An incomplete training session shifts the remaining series together, never adds make-up volume.
            missed=[x for x in eligible if x['id'] in fitness_ids]
            if missed:
                first=min(x['start'] for x in missed);first_day=dt.datetime.fromtimestamp(instant(first),zone).date();delta=max(1,(day-first_day).days)
                chain=[x for x in rows if x['id'] in fitness_ids and x['start'] and instant(x['start'])>=instant(first)]
                if any(x['paused'] or x['parent_paused'] or x['state'] in ('manual','cancelled','paused') or x['last_op']=='resolve' for x in chain):
                    training_notes.append('训练序列含手动固定或暂停事项，保留原计划，未强行补练。')
                else:
                    byid={x['id']:x for x in chain}
                    for session in plan['sessions']:
                        x=byid.get(session.get('todo_id'))
                        if not x:continue
                        new_day=dt.date.fromisoformat(session['day'])+dt.timedelta(days=delta);session['day']=new_day.isoformat()
                        if session.get('slot_start'):
                            for key in ('slot_start','slot_end'):session[key]=(dt.datetime.fromisoformat(session[key])+dt.timedelta(days=delta)).isoformat()
                            low,high=instant(session['slot_start']),instant(session['slot_end'])
                        else:
                            low=dt.datetime.combine(new_day,dt.time(11),zone).timestamp();high=dt.datetime.combine(new_day,dt.time(22),zone).timestamp()
                        self.move(c,owner,x,low,high,day,now);changed.append({**x,'training_shift_days':delta})
                    if changed:
                        # Recompute recovery-day labels from the shifted schedule.
                        plan['rest_days']=[]
                        c.execute('UPDATE fitness_plans SET payload=?,updated=? WHERE owner=?',(json.dumps(plan,ensure_ascii=False),now,owner))
                        training_notes.append('未完成训练及后续训练顺延'+str(delta)+'天，保留间隔、不增加补练量。')
            for x in eligible:
                if x['id'] in fitness_ids or x['notes'].startswith('PersonalOS 恢复训练\n'):continue
                low=dt.datetime.combine(day,dt.time(),zone).timestamp()+settings['start_minute']*60
                high=dt.datetime.combine(day,dt.time(),zone).timestamp()+settings['end_minute']*60
                self.move(c,owner,x,low,high,day,now);changed.append(x)
            task=str(uuid.uuid5(uuid.NAMESPACE_URL,owner+':daily-rollover:'+day.isoformat()))
            c.execute("UPDATE notification_outbox SET state='cancelled' WHERE device=? AND state IN ('pending','retry') AND task IN (SELECT task FROM daily_rollover_runs WHERE owner=? AND day<?)",(owner,owner,day.isoformat()))
            payload={'items':[{k:x.get(k) for k in ('id','title','entry_id','start','end','deadline','training_shift_days')} for x in changed],'notes':training_notes,'day':day.isoformat()}
            c.execute('INSERT INTO daily_rollover_runs VALUES(?,?,?,?,0)',(owner,day.isoformat(),task,json.dumps(payload,ensure_ascii=False)))
            if not changed:return
            result='每天自动重排：'+str(len(changed))+'项执行安排等待最新日历排程和手机同步。'
            meta={'kind':'daily_rollover','todo_ids':[x['id'] for x in changed],'rollover_day':day.isoformat(),'spoken_reply':result}
            c.execute('INSERT INTO tasks(id,body,state,result,plan,created,due,owner) VALUES(?,?,?,?,?,?,NULL,?)',(task,json.dumps({'text':day.isoformat()+' 自动重排摘要','time_zone':p['time_zone']},ensure_ascii=False),'needs_details',result,json.dumps(meta,ensure_ascii=False),now,owner))
            self.s._audit(c,task,'daily_rollover_prepared',{'count':len(changed),'deadlines_changed':False,'day':day.isoformat()})
    @staticmethod
    def move(c,owner,x,low,high,day,now):
        c.execute('INSERT INTO todo_schedule_windows VALUES(?,?,?,?,0,?) ON CONFLICT(owner,todo_id) DO UPDATE SET not_before=excluded.not_before,not_after=excluded.not_after,updated=excluded.updated',(owner,x['id'],low,high,now))
        c.execute("UPDATE auto_schedule SET state='reschedule',reason='每日未完成任务重排，等待最新空档',updated=? WHERE owner=? AND todo_id=?",(now,owner,x['id']))
        c.execute('UPDATE todos SET revision=revision+1,updated=? WHERE owner=? AND id=?',(now,owner,x['id']))
        c.execute('INSERT INTO daily_rollover_items VALUES(?,?,?) ON CONFLICT(owner,todo_id) DO UPDATE SET day=excluded.day',(owner,x['id'],day.isoformat()))
    def finalize(self,owner,now):
        with self.s.db() as c:
            c.execute('BEGIN IMMEDIATE')
            runs=c.execute('SELECT * FROM daily_rollover_runs WHERE owner=? AND notified=0 ORDER BY day',(owner,)).fetchall()
            for run in runs:
                data=json.loads(run['payload']);items=data['items']
                if not items:
                    c.execute('UPDATE daily_rollover_runs SET notified=1 WHERE owner=? AND day=?',(owner,run['day']));continue
                rows=[]
                for item in items:
                    x=c.execute('SELECT t.completed,t.deadline,t.title,a.state,a.reason,e.start,e.end,e.status,e.device_sync,e.updated AS event_updated FROM todos t LEFT JOIN auto_schedule a ON a.todo_id=t.id AND a.owner=t.owner LEFT JOIN calendar_entries e ON e.id=a.entry_id AND e.owner=t.owner WHERE t.owner=? AND t.id=?',(owner,item['id'])).fetchone()
                    if x:rows.append(dict(x))
                snapshot=c.execute('SELECT received FROM planning_context WHERE owner=?',(owner,)).fetchone()
                if any(x['state']=='manual' and snapshot and snapshot['received']<=x['event_updated'] for x in rows if x['event_updated']):continue
                if any(x['state']=='reschedule' for x in rows):continue
                # No success notification before every relevant current event has a real phone receipt.
                if any(x['device_sync'] not in ('written','removed') for x in rows if x['start']):continue
                moved=[x for x in rows if not x['completed'] and x['state'] in ('scheduled','started') and x['device_sync']=='written']
                blocked=[x for x in rows if not x['completed'] and x['state'] in ('unscheduled','manual','paused','cancelled')]
                lines=['自动重排：'+str(len(moved))+'项已同步日历'+('，'+str(len(blocked))+'项未排定。' if blocked else '。')]
                for x in moved:lines.append(x['title']+' → '+x['start'][5:10]+' '+x['start'][11:16]+'–'+x['end'][11:16])
                for x in blocked:lines.append(x['title']+'：'+x['reason'])
                lines.extend(data['notes']);lines.append('原截止日期保留；已逾期事项按补做安排，不代表延长提交期限。')
                summary=lines[0]
                if moved:summary+=' '+ '；'.join(x['title'][:24]+' '+x['start'][11:16] for x in moved[:2])+('等。' if len(moved)>2 else '。')
                task=c.execute('SELECT state,plan FROM tasks WHERE id=? AND owner=?',(run['task'],owner)).fetchone()
                if not task:continue
                if task['state'] in ('cancelled','undone'):
                    c.execute('UPDATE daily_rollover_runs SET notified=1 WHERE owner=? AND day=?',(owner,run['day']));continue
                meta=json.loads(task['plan']);meta.update(spoken_reply=summary,daily_rollover_summary=summary[:220])
                c.execute("UPDATE tasks SET state='completed',result=?,plan=? WHERE id=? AND owner=?",('\n'.join(lines),json.dumps(meta,ensure_ascii=False),run['task'],owner))
                p=c.execute('SELECT time_zone,enabled FROM daily_rollover_policy WHERE owner=?',(owner,)).fetchone();zone=validate_zone(p['time_zone'] if p else DEFAULT_ZONE)
                day=dt.datetime.fromtimestamp(now,zone).date();notify_at=max(now,dt.datetime.combine(day,dt.time(10,30),zone).timestamp())
                # Coalesce stale offline runs: only the newest outstanding day's digest is pushed.
                if p and p['enabled'] and run['day']==runs[-1]['day']:
                    key=run['task']+':completed';c.execute('INSERT OR IGNORE INTO notification_events VALUES(?,?,?,?)',(key,run['task'],'completed',now))
                    if c.execute('SELECT 1 FROM push_devices p JOIN devices d ON p.device=d.token WHERE p.device=? AND p.active=1',(owner,)).fetchone():
                        ident=str(uuid.uuid5(uuid.NAMESPACE_URL,key+':'+owner))
                        c.execute('INSERT OR IGNORE INTO notification_outbox VALUES(?,?,?,?,?,?,?,?,?,?,?)',(ident,key,run['task'],'completed',owner,'pending',0,notify_at,now,notify_at+86400,''))
                c.execute('UPDATE daily_rollover_runs SET notified=1 WHERE owner=? AND day=?',(owner,run['day']))
                self.s._audit(c,run['task'],'daily_rollover_digest_ready',{'moved':len(moved),'blocked':len(blocked),'current_phone_receipts':True})

    def current_summary(self,owner,meta):
        """Recheck at send time; a delayed morning push must not use stale evening slots."""
        rows=[]
        with self.s.db() as c:
            for ident in meta.get('todo_ids',[]):
                r=c.execute('SELECT t.title,t.completed,a.state,a.reason,e.start,e.end,e.device_sync,e.updated AS event_updated FROM todos t LEFT JOIN auto_schedule a ON a.owner=t.owner AND a.todo_id=t.id LEFT JOIN calendar_entries e ON e.owner=t.owner AND e.id=a.entry_id WHERE t.owner=? AND t.id=?',(owner,ident)).fetchone()
                if r:rows.append(dict(r))
            snapshot=c.execute('SELECT received FROM planning_context WHERE owner=?',(owner,)).fetchone()
        if any(x['state']=='manual' and snapshot and snapshot['received']<=x['event_updated'] for x in rows if x['event_updated']):return None
        if any(x['state']=='reschedule' or (x['start'] and x['device_sync'] not in ('written','removed')) for x in rows):return None
        moved=[x for x in rows if not x['completed'] and x['state'] in ('scheduled','started') and x['device_sync']=='written']
        blocked=[x for x in rows if not x['completed'] and x['state'] not in ('scheduled','started','completed')]
        summary=meta['rollover_day'][5:]+' 自动重排：'+str(len(moved))+'项已同步日历'+('，'+str(len(blocked))+'项未排定。' if blocked else '。')
        if moved:summary+=' '+'；'.join(x['title'][:24]+' '+x['start'][11:16] for x in moved[:2])+('等。' if len(moved)>2 else '。')
        return summary[:220]
