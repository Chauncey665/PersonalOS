"""Seven-day re-entry planning using verified availability, never model-invented slots."""
import datetime as dt
import itertools
import json
import time
from auto_planner import instant, free_slot, overlaps, stamp
from calendar_journal import validate_zone

class CalendarRefreshNeeded(ValueError):
    pass

TUTORIALS = {
 '腿举': ('视频 · Mayo Clinic', 'https://www.mayoclinic.org/healthy-lifestyle/fitness/multimedia/leg-press/vid-20084684'),
 '器械坐姿划船': ('图文 · ACE', 'https://www.acefitness.org/resources/everyone/exercise-library/168/seated-row/'),
 '哑铃卧推': ('视频 · Mayo Clinic', 'https://www.mayoclinic.org/healthy-lifestyle/fitness/multimedia/chest-press/vid-20084677'),
 '臀桥': ('图文 · ACE', 'https://www.acefitness.org/resources/everyone/exercise-library/49/glute-bridge/'),
 '哑铃侧平举': ('图文 · ACE', 'https://www.acefitness.org/resources/everyone/exercise-library/26/lateral-raise/'),
}


def select_week(store, owner, health, now=None):
    now = time.time() if now is None else now
    with store.db() as c:
        row = c.execute('SELECT payload,received FROM planning_context WHERE owner=?',(owner,)).fetchone()
        todos = [dict(r) for r in c.execute('SELECT id,title,deadline,completed FROM todos WHERE owner=?',(owner,))]
        slots = [dict(r) for r in c.execute("SELECT e.start,e.end FROM auto_schedule a JOIN calendar_entries e ON e.id=a.entry_id AND e.owner=a.owner WHERE a.owner=? AND e.status='ready'",(owner,))]
    if not row or not 0 <= now-row['received'] <= 900:
        raise CalendarRefreshNeeded('请打开 PersonalOS 刷新完整日历；训练计划需要当前课表和空闲时间，不会按旧空档写入。')
    ctx=json.loads(row['payload']);zone=validate_zone(ctx['time_zone']);settings=store.planner.settings(owner)
    if not settings['enabled']:raise ValueError('请先开启自动排程，再安排训练日历。')
    today=dt.datetime.fromtimestamp(now,zone).date()
    end=dt.datetime.combine(today+dt.timedelta(days=7),dt.time(),zone).timestamp()
    if ctx['truncated'] or instant(ctx['range_end'])<end:raise ValueError('日历范围不足未来七天，请打开 PersonalOS 刷新。')
    busy=[(instant(e['start'])-1800,instant(e['end'])+1800) for e in ctx['events']]
    for e in slots:
        if e['start'] and e['end']:busy.append((instant(e['start'])-1800,instant(e['end'])+1800))
    deadlines={}
    for todo in todos:
        if todo['completed'] or not todo['deadline']:continue
        value=todo['deadline'];day=(dt.date.fromisoformat(value) if len(value)==10 else dt.datetime.fromtimestamp(instant(value),zone).date())
        deadlines.setdefault(day,[]).append(todo['title'])
    meals=[];windows=[];pressures={}
    for n in range(7):
        day=today+dt.timedelta(days=n);base=dt.datetime.combine(day,dt.time(),zone)
        at=lambda minutes:(base+dt.timedelta(minutes=minutes)).timestamp()
        # Gym hours have not been supplied: planning preference ends at 22, not a claim it is open.
        low=max(at(max(660,settings['start_minute'])),now+1800);high=min(at(min(1320,settings['end_minute'])),end)
        for lo,hi,pref in [(660,900,780),(1080,1260,1140)]:
            p=free_slot(at(max(lo,settings['start_minute'])),at(min(hi,settings['end_minute'])),settings['meal_minutes'],busy,at(pref))
            if p is not None:
                pair=(p,p+settings['meal_minutes']*60);meals.append(pair);busy.append((pair[0]-900,pair[1]+900))
            else:busy.append((at(lo),at(hi)))
        # Reserve study time on unfinished deadline days, even when its old work block has elapsed.
        if day in deadlines:
            p=free_slot(max(low,at(840)),high,120,busy,at(900))
            if p is not None:busy.append((p,p+7200))
        pressure=sum(max(0,min(b,high)-max(a,low)) for a,b in busy)/3600
        pressures[n]=pressure+4*len(deadlines.get(day,[]))+0.5*len(deadlines.get(day+dt.timedelta(days=1),[]))
        windows.append((n,day,low,high,at(960)))
    candidates=[]
    sleep=health['metrics'].get('sleep_hours')
    today_workouts=[w for w in health['workouts'] if dt.datetime.fromtimestamp(w['end'],zone).date()==today]
    for n,day,low,high,pref in windows:
        if n==0 and ((sleep and sleep['value']<6) or today_workouts):continue
        point=free_slot(low,high,55,busy,pref)
        if point is not None:candidates.append((n,day,point,point+3300))
    pairs=[(a,b) for a,b in itertools.combinations(candidates,2) if 48*3600<=b[2]-a[3]<=5*86400]
    if not pairs:raise ValueError('未来七天找不到两次至少间隔48小时的完整力量空档；没有挤占课程或自动缩短恢复，请调整可用时段。')
    a,b=min(pairs,key=lambda pair:(sum(pressures[x[0]] for x in pair)+abs(pair[1][0]-pair[0][0]-3),pair[0][0],pair[1][0]))
    sessions=[]
    for idx,x in enumerate((a,b)):
        n,day,start,finish=x
        exercises=['热身8分钟：轻松走动、活动关节；前三个力量动作先做一组轻重量练习。',
          '腿举：2组 × 8–12次；器械不熟悉时请现场教练协助调整。',
          '器械坐姿划船：2组 × 8–12次。',
          '轻哑铃卧推：2组 × 8–12次；需要时请人协助起落哑铃。',
          '臀桥：2组 × 10–15次。',
          '哑铃侧平举：1–2组 × 10–15次；不甩动、不耸肩，舒适范围内完成。',
          '收尾5分钟轻松走动。全程保留约3次余力；组间休息90–120秒。不测试最大重量。']
        if idx:exercises.append('复用上次动作和重量；若上次酸痛仍明显或疲劳，先减为每个动作1组。只有全部动作稳定且余力足够，后续再逐步加量。')
        tutorials=[{'name':k,'type':v[0],'url':v[1]} for k,v in TUTORIALS.items()]
        sessions.append({'key':'strength-'+str(idx),'day':day.isoformat(),'title':'全身力量 '+('A' if idx==0 else 'B')+' · 恢复增肌','minutes':55,'exercises':exercises,'tutorials':tutorials,'slot_start':stamp(start,zone),'slot_end':stamp(finish,zone),'reason':'避开已知日历、吃饭与通勤；两次力量至少间隔48小时，优先避开截止压力大的日子。'})
    strength_days={a[0],b[0]};targets=[(a[0]+b[0])/2,min(6,b[0]+2)];used=set(strength_days)
    for i,target in enumerate(targets):
        options=[]
        for n,day,low,high,pref in windows:
            if n in used:continue
            point=free_slot(low,high,30,busy,pref)
            if point is not None:options.append((abs(n-target)+pressures[n]*.3,n,day,point))
        if not options:continue
        _,n,day,point=min(options);used.add(n)
        sessions.append({'key':'cardio-'+str(i),'day':day.isoformat(),'title':'轻有氧 · 快走或轻松游泳','minutes':30,'exercises':['5分钟慢走热身；20分钟快走，或熟悉泳池中的轻松游泳；5分钟缓和。','维持可以完整说话的轻松强度；不安排间歇冲刺，不追求热量数字。','明显疲劳或酸痛时改休息。若当天临时打球或跑步，不额外叠加这次有氧。'],'tutorials':[],'slot_start':stamp(point,zone),'slot_end':stamp(point+1800,zone),'reason':'非力量日的低强度体能恢复，不安排连续高强度。'})
    sessions.sort(key=lambda x:x['slot_start'])
    return {'sessions':sessions,'period_start':today.isoformat(),'period_end':(today+dt.timedelta(days=6)).isoformat(),'calendar_captured_at':ctx['captured_at'],'health_captured_at':health['captured_at'],'unresolved_deadlines':[{'day':d.isoformat(),'titles':v} for d,v in deadlines.items() if today<=d<today+dt.timedelta(days=7)],'rest_days':[(today+dt.timedelta(days=i)).isoformat() for i in range(7) if i not in used]}
