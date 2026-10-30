"""Move explicitly selected unfinished scheduled tasks; dates and IDs come from durable records."""
import datetime as dt
import json
import re
import time
from calendar_journal import validate_zone, DEFAULT_ZONE
from auto_planner import instant


def requested(text):
    if re.search(r'为什么|怎么|如何|是否|假如|假设|翻译|解释|不要|不用|先别|不需要|[“”「」]',text):return False
    return bool(re.search(r'昨天|昨日|之前|过去',text) and
                re.search(r'没(?:有)?做(?:完)?|未完成|没完成|没(?:有)?打勾|未勾选',text) and
                re.search(r'东西|任务|待办|事项|安排|计划|那些|这些|打勾|勾选',text) and
                re.search(r'挪|移|推迟|延期|重(?:新)?排|重新安排',text))


class BacklogReschedule:
    def __init__(self,store):self.s=store
    @staticmethod
    def initialize(c):
        c.execute('CREATE TABLE IF NOT EXISTS backlog_receipts(owner TEXT NOT NULL,task TEXT NOT NULL,payload TEXT NOT NULL,PRIMARY KEY(owner,task))')
    def spec(self,row):
        b=json.loads(row['body']);text=b['text']
        if b.get('document_ids') or not requested(text):return None
        zone=validate_zone(b.get('time_zone',DEFAULT_ZONE));today=dt.datetime.fromtimestamp(row['created'],zone).date()
        source=today-dt.timedelta(days=1) if re.search(r'昨天|昨日',text) else None
        target=today+dt.timedelta(days=1) if re.search(r'(?:挪|移|放|推|排|到).{0,8}明天',text) else today
        # An explicit destination or scoped clarification inherits the previous destination.
        explicit=bool(re.search(r'(?:挪|移|放|推|排|到).{0,8}(?:今天|明天)',text))
        if not explicit and b.get('session_id') and re.search(r'我的意思|就是|重新排|重新安排',text):
            with self.s.db() as c:
                prior=c.execute('SELECT t.body,t.created FROM tasks t JOIN assistant_turns a ON a.task=t.id WHERE a.owner=? AND a.session=? AND t.id<>? AND t.created<? AND t.created>? ORDER BY t.created DESC LIMIT 10',(row['owner'],b['session_id'],row['id'],row['created'],row['created']-1800)).fetchall()
            for previous in prior:
                p=json.loads(previous['body'])['text']
                if requested(p) and re.search(r'(?:挪|移|放|推|排|到).{0,8}(?:今天|明天)',p):
                    d=dt.datetime.fromtimestamp(previous['created'],zone).date();target=d+dt.timedelta(days=1 if '明天' in p else 0);explicit=True;break
        return {'source':source,'target':target,'zone':zone,'one_day':explicit,'cutoff':today}
    def candidates(self,owner,spec):
        with self.s.db() as c:
            if not c.execute('SELECT 1 FROM devices WHERE token=?',(owner,)).fetchone():raise ValueError('需要当前设备认证')
            rows=c.execute("SELECT t.id,t.title,t.deadline,t.revision,e.id AS entry_id,e.start,e.end,e.revision AS entry_revision,a.state,e.last_op,e.status,COALESCE(p.paused,0) AS paused FROM todos t JOIN auto_schedule a ON a.todo_id=t.id AND a.owner=t.owner JOIN calendar_entries e ON e.id=a.entry_id AND e.owner=t.owner LEFT JOIN todo_priorities p ON p.todo_id=t.id AND p.owner=t.owner WHERE t.owner=? AND t.completed=0 AND t.kind='task' AND e.kind='plan' AND e.start IS NOT NULL ORDER BY e.start LIMIT 501",(owner,)).fetchall()
        if len(rows)>500:raise ValueError('排程事项过多，未使用截断清单批量移动')
        selected=[];skipped=[]
        for raw in rows:
            x=dict(raw);day=dt.datetime.fromtimestamp(instant(x['start']),spec['zone']).date()
            matches=day==spec['source'] if spec['source'] else day<spec['cutoff']
            if not matches:continue
            if x['paused'] or x['status']!='ready' or x['state'] in ('manual','cancelled','paused') or x['last_op']=='resolve':skipped.append(x);continue
            selected.append(x)
        return selected,skipped
    def run(self,row):
        spec=self.spec(row)
        if not spec:return None
        owner=row['owner']
        with self.s.db() as c:
            prior=c.execute('SELECT payload FROM backlog_receipts WHERE owner=? AND task=?',(owner,row['id'])).fetchone()
        if prior:return json.loads(prior['payload'])
        selected,skipped=self.candidates(owner,spec)
        if not selected:
            answer='没有找到对应日期里尚未勾选完成、可以自动重排的安排。没有改动已完成或未来事项。'
            if skipped:answer+='有'+str(len(skipped))+'项暂停、取消或手动安排，需要单独指定。'
            return {'state':'completed','result':answer,'plan':json.dumps({'kind':'backlog_reschedule','todo_ids':[],'spoken_reply':answer},ensure_ascii=False)}
        settings=self.s.planner.settings(owner);zone=spec['zone'];base=dt.datetime.combine(spec['target'],dt.time(),zone)
        low=(base+dt.timedelta(minutes=settings['start_minute'])).timestamp()
        high=(base+dt.timedelta(minutes=settings['end_minute'])).timestamp() if spec['one_day'] else None
        if high and high<=time.time():raise ValueError('目标日期已过去，请指定新的执行日期；未移动任务。')
        now=time.time();ids=[x['id'] for x in selected]
        answer='已将'+str(len(ids))+'项未完成安排设为'+spec['target'].isoformat()+('当天重新排程。' if spec['one_day'] else '起重新排程。')+'原截止日期保留，已完成事项不变。'
        answer+='\n'+'\n'.join('· '+x['title'] for x in selected)
        if skipped:answer+='\n另有'+str(len(skipped))+'项手动、暂停或取消的安排未改。'
        answer+='\n具体时段按最新日历空档计算；放不下的会保留为未排定，不偷偷改截止日期。Apple 日历写入以手机回执为准。'
        result={'state':'completed','result':answer,'plan':json.dumps({'kind':'backlog_reschedule','todo_ids':ids,'source_day':str(spec['source']) if spec['source'] else 'past','target_day':str(spec['target']),'spoken_reply':'已记录'+str(len(ids))+'项重排要求，日历写入还需核对手机回执。'},ensure_ascii=False)}
        with self.s.db() as c:
            c.execute('BEGIN IMMEDIATE')
            previous=c.execute('SELECT payload FROM backlog_receipts WHERE owner=? AND task=?',(owner,row['id'])).fetchone()
            if previous:return json.loads(previous['payload'])
            for x in selected:
                current=c.execute('SELECT t.completed,t.revision,e.revision AS entry_revision FROM todos t JOIN calendar_entries e ON e.id=? AND e.owner=t.owner WHERE t.owner=? AND t.id=?',(x['entry_id'],owner,x['id'])).fetchone()
                if not current or current['completed'] or current['revision']!=x['revision'] or current['entry_revision']!=x['entry_revision']:raise ValueError('待办或日历刚发生变化，请重试；未部分移动。')
                c.execute('INSERT INTO todo_schedule_windows VALUES(?,?,?,?,0,?) ON CONFLICT(owner,todo_id) DO UPDATE SET not_before=excluded.not_before,not_after=excluded.not_after,updated=excluded.updated',(owner,x['id'],low,high,now))
                c.execute("UPDATE auto_schedule SET state='reschedule',reason='已授权重排未完成安排，等待最新空档',updated=? WHERE owner=? AND todo_id=?",(now,owner,x['id']))
                c.execute('UPDATE todos SET revision=revision+1,updated=? WHERE owner=? AND id=?',(now,owner,x['id']))
            c.execute('INSERT INTO backlog_receipts VALUES(?,?,?)',(owner,row['id'],json.dumps(result,ensure_ascii=False)))
            self.s._audit(c,row['id'],'backlog_reschedule_applied',{'before':selected,'target_day':str(spec['target']),'deadlines_changed':False,'todo_ids':ids})
        self.s.planner.reconcile(owner)
        return result
