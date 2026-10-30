"""Owner-scoped health summaries. No raw samples, cloud calls or medical inference."""
import datetime as dt
import json
import math
import uuid
import hashlib
import re
import time
import threading
from calendar_journal import validate_zone, DEFAULT_ZONE

METRICS = {
 'weight_kg': (20,400,'kg',30*86400), 'height_cm':(80,250,'cm',366*86400),
 'steps':(0,200000,'steps',7200), 'active_kcal':(0,20000,'kcal',7200),
 'resting_hr':(20,220,'bpm',2*86400), 'sleep_hours':(0,24,'h',36*3600),
 'dietary_kcal':(0,30000,'kcal',86400), 'protein_g':(0,1000,'g',86400),
}
PROFILE_FIELDS={'goal','sport','experience','restrictions','equipment','training_schedule','adult'}

def requested(text):
    if re.search(r'代码|API|接口|翻译|文档|应用开发|天气|下雨|气温',text,re.I):return False
    return bool(re.search(r'健身|训练|练肩|练胸|练背|练腿|增肌|有氧|无氧|运动数据|身体数据|健康数据|蛋白质|能量摄入|热量摄入|吃够|长肌肉|腿举|臀桥|侧平举|俯卧撑|坐站|深蹲|硬拉|卧推|推举|划船|面拉|反向飞鸟|这个动作|今天不想练|今天不练',text))

DEMONSTRATIONS={
 'lateral_raise': {'name':'哑铃侧平举','aliases':['侧平举'], 'cues':['轻重量，肘微屈，手臂略在身体前方。','缓慢抬起至舒适范围，最高约肩高；不耸肩或甩动。','缓慢放下；肩部疼痛时停止。'], 'source':'https://www.acefitness.org/resources/everyone/exercise-library/26/lateral-raise/'},
 'wall_pushup': {'name':'墙壁俯卧撑','aliases':['墙壁俯卧撑','靠墙俯卧撑'], 'cues':['双手放在墙上，约肩宽；身体保持一条线。','屈肘，让胸部缓慢靠近墙；再推回。','不要塌腰；距离墙越远越费力。'], 'source':'https://www.nhs.uk/live-well/exercise/strength-exercises/'},
 'sit_to_stand': {'name':'椅子坐站','aliases':['椅子坐站','坐站'], 'cues':['使用稳固、不带轮子的椅子，双脚与髋同宽。','身体略向前，从脚跟发力站起。','控制速度坐回；需要时用手支撑。'], 'source':'https://www.nhs.uk/live-well/exercise/strength-exercises/'}
}

class Fitness:
    def __init__(self,store):self.store=store;self.resume_lock=threading.Lock()
    @staticmethod
    def initialize(c):
        c.execute('CREATE TABLE IF NOT EXISTS health_summaries(owner TEXT PRIMARY KEY,payload TEXT NOT NULL,received REAL NOT NULL)')
        c.execute('CREATE TABLE IF NOT EXISTS fitness_plans(owner TEXT PRIMARY KEY,id TEXT NOT NULL,payload TEXT NOT NULL,updated REAL NOT NULL)')
        c.execute('CREATE TABLE IF NOT EXISTS fitness_operations(owner TEXT NOT NULL,task TEXT NOT NULL,result TEXT NOT NULL,PRIMARY KEY(owner,task))')
        c.execute('CREATE TABLE IF NOT EXISTS fitness_profiles(owner TEXT PRIMARY KEY,payload TEXT NOT NULL,updated REAL NOT NULL)')
    def authorize(self,owner):
        with self.store.db() as c:
            if not c.execute('SELECT 1 FROM devices WHERE token=?',(owner,)).fetchone():raise ValueError('需要当前设备认证')
    def snapshot(self,owner,data):
        self.authorize(owner);now=time.time()
        if not isinstance(data,dict) or set(data)!={'captured_at','day','time_zone','metrics','workouts'}:raise ValueError('健康摘要字段无效')
        zone=validate_zone(data['time_zone'])
        if type(data['captured_at']) not in (int,float) or not math.isfinite(data['captured_at']) or not 0<=now-data['captured_at']<=600:raise ValueError('健康摘要过期，请重新同步')
        if data['day']!=dt.datetime.fromtimestamp(data['captured_at'],zone).date().isoformat():raise ValueError('健康摘要日期不匹配')
        if not isinstance(data['metrics'],dict) or set(data['metrics'])-METRICS.keys():raise ValueError('健康项目无效')
        for key,item in data['metrics'].items():
            low,high,unit,_=METRICS[key]
            if not isinstance(item,dict) or set(item)!={'value','unit','measured_at','source'}:raise ValueError('健康项目格式无效')
            if type(item['value']) not in (int,float) or not math.isfinite(item['value']) or not low<=item['value']<=high or item['unit']!=unit:raise ValueError('健康数值或单位无效')
            if type(item['measured_at']) not in (float,int) or not math.isfinite(item['measured_at']) or not 0<item['measured_at']<=now+60:raise ValueError('健康数据时间无效')
            if not isinstance(item['source'],str) or not 1<=len(item['source'])<=180:raise ValueError('健康数据来源无效')
        if not isinstance(data['workouts'],list) or len(data['workouts'])>50:raise ValueError('训练摘要过多')
        for w in data['workouts']:
            if not isinstance(w,dict) or set(w)!={'kind','start','end','source'}:raise ValueError('训练摘要格式无效')
            if not all(isinstance(w[k],str) and 0<len(w[k])<=180 for k in ('kind','source')):raise ValueError('训练来源无效')
            if not all(type(w[k]) in (int,float) and math.isfinite(w[k]) for k in ('start','end')) or not now-8*86400<=w['start']<w['end']<=now+60 or w['end']-w['start']>86400:raise ValueError('训练时间无效')
        with self.store.db() as c:
            prev=c.execute('SELECT payload FROM health_summaries WHERE owner=?',(owner,)).fetchone()
            if prev and json.loads(prev['payload'])['captured_at']>data['captured_at']:raise ValueError('较旧摘要不能覆盖较新数据')
            c.execute('INSERT OR REPLACE INTO health_summaries VALUES(?,?,?)',(owner,json.dumps(data,ensure_ascii=False),now))
        return {'saved':True,'metric_count':len(data['metrics']),'workout_count':len(data['workouts'])}
    def profile(self,owner,data=None):
        self.authorize(owner)
        if data is not None:
            if not isinstance(data,dict) or set(data)!=PROFILE_FIELDS or type(data['adult']) is not bool:raise ValueError('训练资料字段无效')
            for k,v in data.items():
                if k!='adult' and (not isinstance(v,str) or len(v)>600 or any(ord(ch)<32 and ch!='\n' for ch in v)):raise ValueError('训练资料文字无效')
            with self.store.db() as c:c.execute('INSERT OR REPLACE INTO fitness_profiles VALUES(?,?,?)',(owner,json.dumps(data,ensure_ascii=False),time.time()))
        with self.store.db() as c:r=c.execute('SELECT payload FROM fitness_profiles WHERE owner=?',(owner,)).fetchone()
        return json.loads(r['payload']) if r else {k:False if k=='adult' else '' for k in PROFILE_FIELDS}
    def clear(self,owner):
        self.authorize(owner)
        with self.store.db() as c:c.execute('DELETE FROM health_summaries WHERE owner=?',(owner,))
        return {'cleared':True}
    def status(self,owner,now=None):
        self.authorize(owner);now=time.time() if now is None else now
        with self.store.db() as c:r=c.execute('SELECT payload,received FROM health_summaries WHERE owner=?',(owner,)).fetchone()
        data=json.loads(r['payload']) if r else None;metrics={};stale=[]
        if data:
            today=dt.datetime.fromtimestamp(now,validate_zone(data['time_zone'])).date().isoformat()
            for key,item in data['metrics'].items():
                valid=0<=now-data['captured_at']<=7200 and -60<=now-item['measured_at']<=METRICS[key][3]
                if key in ('steps','active_kcal','dietary_kcal','protein_g'):valid=valid and data['day']==today
                if valid:metrics[key]=item
                else:stale.append(key)
        return {'connected':bool(data),'fresh':bool(data and 0<=now-data['captured_at']<=7200),'captured_at':data['captured_at'] if data else None,'day':data['day'] if data else None,'metrics':metrics,'stale_metrics':stale,'missing_metrics':[k for k in METRICS if k not in metrics], 'workouts':data['workouts'] if data and 0<=now-data['captured_at']<=7200 else [],'profile':self.profile(owner),'coverage':'仅读取已同步且被允许的项目；缺失不等于零或没有运动。饮食记录不代表全天完整摄入。'}
    def reply(self,text,spoken=None,**meta):
        return {'state':'completed','result':text,'plan':json.dumps({'fitness':True,'spoken_reply':spoken or text[:180],**meta},ensure_ascii=False)}
    def run(self,row):
        b=json.loads(row['body']);text=b['text']
        if not requested(text) or b.get('document_ids'):return None
        if re.search(r'标记完成|勾选|勾掉|取消.{0,8}任务|改.{0,10}截止',text):return None
        if re.search(r'记录|记下|安排|提醒',text) and re.search(r'\d{1,2}[:：点].{0,12}(?:到|至|-)\s*\d{1,2}',text) and not re.search(r'计划|建议|怎么',text):return None
        owner=row.get('owner','');self.authorize(owner);s=self.status(owner);p=s['profile'];m=s['metrics']
        if re.search(r'胸痛|晕厥|呼吸困难',text):return self.reply('先停止训练。若现在有胸痛、晕厥或明显呼吸困难，请立即寻求当地急救；不能根据手表数据判断可以继续训练。')
        weekly=bool(re.search(r'计划|排.{0,8}(?:一周|七天|7天)|(?:一周|七天|7天).{0,8}训练',text))
        from fitness_week import TUTORIALS
        if not weekly and re.search(r'怎么做|演示|教程|视频|动图',text):
            matches=[(name,info) for name,info in TUTORIALS.items() if name in text or (name=='器械坐姿划船' and '划船' in text) or (name=='哑铃卧推' and '卧推' in text)]
            if matches and (re.search(r'教程|视频|网上',text) or matches[0][0] not in ('哑铃侧平举',)):
                name,(kind,url)=matches[0]
                return self.reply(name+' · '+kind+'\n'+url+'\n这是已核对动作名称的教程，不是对你姿势的评估。',name+'的'+kind.split(' · ')[0]+'教程已放在详情。',tutorial_url=url)
        for key,demo in DEMONSTRATIONS.items():
            if not weekly and any(x in text for x in demo['aliases']) and not re.search(r'教程|视频',text) and re.search(r'怎么做|演示|动图|示范',text):
                return self.reply(demo['name']+'：\n'+'\n'.join(demo['cues'])+'\n动作示意在 PersonalOS「健康与训练 → 动作演示」中；示意不是姿势评估。\n来源：'+demo['source'],demo['name']+'的动作要点和可播放示意已放在详情。',exercise_id=key)
        if not weekly and re.search(r'这个动作|动图|演示|怎么做',text):
            return self.reply('请告诉我具体动作名称。目前已接通侧平举、墙壁俯卧撑和椅子坐站示意；未核对的动作不会生成看似正确的演示。')
        if re.search(r'摄入|吃够|长肌肉|蛋白质|营养',text):
            lines=[]
            if 'dietary_kcal' in m:lines.append('今天已记录摄入约'+str(round(m['dietary_kcal']['value']))+'千卡。')
            if 'protein_g' in m:lines.append('已记录蛋白质约'+str(round(m['protein_g']['value'],1))+'克。')
            if not lines:lines.append('还没有今天的饮食摄入记录。')
            lines.append('这些只能代表已记录的部分。手表的活动消耗不是摄入，也不是全天总消耗；目前不能确认你今天吃够了或能否增肌。')
            lines.append('需要全天餐食与份量、体重趋势和专项训练负荷，再评估持续摄入与恢复；单日数据不能保证长肌肉。')
            return self.reply('\n'.join(lines),lines[0]+'目前资料不足，不能确认今天吃够了；请补充全天餐食和份量。',health_captured_at=s['captured_at'])
        if re.search(r'今天不想练|今天不练|不想练|跳过今天|今天休息',text):
            return self.skip(row)
        if re.search(r'确认(?:安排|记录|采用|保存).{0,6}训练计划|安排这(?:份|个)训练计划',text):
            plan=self.plan(owner)
            if not plan:return self.reply('还没有训练草案。先说“给我下周的训练计划”，再确认安排。')
            try:
                result=self.commit(owner,plan['id'],row['id'])
                return self.reply(result['message'],todo_ids=result['todo_ids'])
            except ValueError as error:return self.reply(str(error))
        if re.search(r'有氧|无氧',text):
            today=dt.datetime.now(validate_zone(b.get('time_zone',DEFAULT_ZONE))).date()
            workouts=[w for w in s['workouts'] if dt.datetime.fromtimestamp(w['start'],validate_zone(b.get('time_zone',DEFAULT_ZONE))).date()==today]
            fact='今天读到'+str(len(workouts))+'条运动记录；它们不能完整说明训练强度或局部肌肉疲劳。' if workouts else '目前没有读到完整的今天训练记录，不能判断你没有运动。'
            return self.reply(fact+'\n有氧后能否加力量，需要知道有氧项目、时长、强度、当前疲劳／疼痛，以及明后天专项训练或比赛。先补充这些；不能仅凭活动热量判断可以再练。',fact+'请补充今天有氧的时长、强度和疲劳情况，我再判断如何接力量。')
        if re.search(r'计划|排一下|安排|练肩|练胸|练背|练腿|增肌|健身',text):
            needed=[label for key,label in [('sport','运动项目'),('training_schedule','专项训练和比赛安排'),('restrictions','伤病或禁忌（没有也请明确填无）'),('experience','力量训练经历')] if not p[key].strip()]
            if not p['adult']:needed.append('是否已成年')
            if needed:
                return self.reply('先补齐'+ '、'.join(needed)+'，再定训练量；可以在「健康与训练 → 训练资料」一次填写。\n'+('健康摘要已同步，但这不等于已知疲劳和训练负荷。' if s['fresh'] else '尚无新鲜健康摘要；请先同步华为到 Apple 健康，并在 PersonalOS 开启共享。')+'\n在这些信息未齐之前，我不会把通用动作清单说成你的个性化计划，也没有安排日历。','还需要'+ '、'.join(needed[:3])+'。先补齐资料，再排个性化训练。',needs_profile=needed)
            rolling=bool(re.search(r'接下来|未来|一周|七天|7天',text)) and '下周' not in text
            auto_commit=rolling and bool(re.search(r'(?:同步|写入|记录|添加|放进|排进).{0,5}(?:日历|calendar)',text,re.I)) and not re.search(r'不要|不必|先别|暂不|不需要|不用|是否|能否|可以吗',text)
            try:
                plan=self.preview(owner,b.get('time_zone',DEFAULT_ZONE),row['created'],include_tonight=bool(re.search(r'今晚|今天',text) and re.search(r'肩|健身',text)),rolling=rolling)
                if auto_commit:
                    result=self.commit(owner,plan['id'],row['id']);plan=self.plan(owner)
                    return self.reply(self.format_plan(plan)+'\n'+result['message'],'未来七天训练已记录，日历仍以手机同步回执为准。',fitness_plan_id=plan['id'],todo_ids=result['todo_ids'])
            except ValueError as error:
                from fitness_week import CalendarRefreshNeeded
                if auto_commit and isinstance(error,CalendarRefreshNeeded):
                    return {'state':'needs_details','result':str(error)+' 手机刷新后会自动继续，不用重新提交。请求24小时内有效。','plan':json.dumps({'fitness':True,'fitness_waiting_context':True,'spoken_reply':'已记下训练安排请求，等手机刷新日历后自动继续。'},ensure_ascii=False)}
                return self.reply(str(error))
            detail=self.format_plan(plan)
            if plan['committed']:
                return self.reply(detail,'这份训练计划已记录。实际时段和手机日历同步，以待办状态为准。',fitness_plan_id=plan['id'],todo_ids=[x['todo_id'] for x in plan['sessions']])
            return self.reply(detail,(('今晚优先恢复；' if any(x['title']=='今晚恢复' for x in plan['sessions']) else '今晚先做短时肩部适应；') if any(x.get('evening') for x in plan['sessions']) else '')+'下周草案是两次全身力量、一次可选轻有氧。尚未写入日历，确认后再安排。',fitness_plan_id=plan['id'])
        labels={'weight_kg':'体重','height_cm':'身高','steps':'今日步数','active_kcal':'今日活动消耗','resting_hr':'静息心率','sleep_hours':'最近睡眠','dietary_kcal':'今日已记录摄入','protein_g':'今日已记录蛋白质'}
        lines=[labels[k]+'：'+str(round(v['value'],1))+' '+v['unit']+'（来源：'+v['source']+'）' for k,v in m.items()]
        return self.reply(('\n'.join(lines) if lines else '没有新鲜、可用的健康数据。')+'\n'+s['coverage'],'读到'+str(len(m))+'项可用健康数据。缺失项目不按零计算。' if m else '还没有新鲜健康数据，请先在手机开启并同步。',health_captured_at=s['captured_at'])

    def resume_pending(self,owner):
        # Only previously explicit calendar-write requests are eligible; cancellation removes them.
        if not self.resume_lock.acquire(blocking=False):return
        try:
            with self.store.db() as c:
                rows=[dict(r) for r in c.execute("SELECT * FROM tasks WHERE owner=? AND state='needs_details' ORDER BY created",(owner,))]
            for row in rows:
                if not json.loads(row['plan']).get('fitness_waiting_context'):continue
                if time.time()-row['created']>86400:
                    self.store.update(row['id'],state='failed',result='训练安排请求已超过24小时，未自动写入。请重新提出安排。',plan='{}');continue
                with self.store.db() as c:
                    claimed=c.execute("UPDATE tasks SET state='running' WHERE id=? AND owner=? AND state='needs_details' AND plan=?",(row['id'],owner,row['plan'])).rowcount
                if not claimed:continue
                try:
                    result=self.run({**row,'created':time.time()})
                    self.store.update(row['id'],**result)
                    self.store.audit(row['id'],'fitness_resumed_after_calendar_refresh')
                except Exception:
                    self.store.update(row['id'],state='failed',result='日历已刷新，但训练排程未完成，请查看任务详情。')
        finally:self.resume_lock.release()

    def plan(self,owner):
        self.authorize(owner)
        with self.store.db() as c:r=c.execute('SELECT payload FROM fitness_plans WHERE owner=?',(owner,)).fetchone()
        return json.loads(r['payload']) if r else None
    def preview(self,owner,zone_name=DEFAULT_ZONE,now=None,include_tonight=False,rolling=False):
        self.authorize(owner);now=time.time() if now is None else now;p=self.profile(owner)
        current=self.plan(owner)
        if current and current.get('committed'):return current
        zone=validate_zone(zone_name);today=dt.datetime.fromtimestamp(now,zone).date();monday=today+dt.timedelta(days=7-today.weekday())
        # Conservative re-entry template, not a sport-specific load prescription.
        sessions=[]
        if include_tonight and dt.datetime.fromtimestamp(now,zone).hour<22:
            sessions.append({'key':'tonight','day':today.isoformat(),'title':'肩部恢复适应','minutes':25,'evening':True,'exercises':['先用轻活动和轻重量热身5分钟。','轻重量哑铃侧平举：1–2组 × 10–15次。','轻重量器械划船：1–2组 × 8–12次。','保留至少3次余力，不做大重量推举或力竭训练；肩痛停止。']})
        recipes=[(0,'恢复力量 A',40,['椅子坐站或轻重量腿举：1–2组 × 8–12次','器械坐姿划船：1–2组 × 8–12次','墙壁俯卧撑或轻器械推胸：1–2组 × 8–12次','哑铃侧平举：1–2组 × 10–15次']),
                 (2,'可选轻有氧',25,['步行、轻松跑或熟悉泳池中的轻松游泳任选一项；保持可完整交谈的强度。','若疲劳或酸痛明显，改休息；不追求配速或消耗热量。']),
                 (3,'恢复力量 B',40,['椅子坐站或轻重量腿举：1–2组 × 8–12次','器械坐姿划船：1–2组 × 8–12次','墙壁俯卧撑或轻器械推胸：1–2组 × 8–12次','哑铃侧平举：1–2组 × 10–15次'])]
        for offset,title,minutes,exercises in recipes:sessions.append({'key':str(offset),'day':(monday+dt.timedelta(days=offset)).isoformat(),'title':title,'minutes':minutes,'exercises':exercises})
        health=self.status(owner);recovery='尚无新鲜健康摘要；不把缺失记录当作没有运动。'
        if health['fresh']:
            sleep=health['metrics'].get('sleep_hours')
            recovery='已核对当前共享的健康摘要；手表数据不能单独判断恢复、训练强度或可用重量。'
            if sleep:
                recovery+=' 最近睡眠记录约'+str(round(sleep['value'],1))+'小时。'
                if sleep['value']<6:
                    recovery+=' 这次睡眠记录较短，先反馈困倦、疲劳与酸痛；今晚优先恢复。'
                    for session in sessions:
                        if session.get('evening'):
                            session.update(title='今晚恢复',minutes=15,exercises=['优先休息和补充睡眠；若感觉舒适，可轻松走动。','不补做大重量肩部训练；后续强度看实际恢复反馈。'])
        profile_hash=hashlib.sha256(json.dumps(p,sort_keys=True,ensure_ascii=False).encode()).hexdigest()
        plan={'id':str(uuid.uuid4()),'created':now,'time_zone':zone_name,'sessions':sessions,'committed':False,'profile_hash':profile_hash,'basis':'依据你填写的当前活动情况和增肌／体能目标，采用恢复训练草案；未测得力量能力，未凭手表确定重量。'+recovery,'guidance':'先热身5–8分钟；力量组间休息约1–2分钟，保留至少3次余力，不练到力竭。首周以动作熟悉和恢复反应为准，疼痛时停下。器械不可用时先反馈，不随意替换成更难动作。','source':'https://pmc.ncbi.nlm.nih.gov/articles/PMC12965823/'}
        if rolling:
            from fitness_week import select_week
            selection=select_week(self.store,owner,health,now)
            plan.update(selection);plan['rolling_week']=True
            plan['basis']+=' 这是未来七天的恢复增肌安排；采用当前可见课表、待办截止与日历空档。睡眠和心率不是力量测试，不据此指定公斤数。'
            plan['guidance']+=' 预约前仍需确认学校健身房/泳池开放与器械；计划时段不是场馆开放证明。'
        with self.store.db() as c:c.execute('INSERT OR REPLACE INTO fitness_plans VALUES(?,?,?,?)',(owner,plan['id'],json.dumps(plan,ensure_ascii=False),time.time()))
        return plan
    def format_plan(self,plan):
        days='一二三四五六日';lines=[plan['basis'],plan['guidance']]
        for session in plan['sessions']:
            date=dt.date.fromisoformat(session['day']);lines.append(session['day']+'（周'+days[date.weekday()]+'） · '+session['title']+' · 约'+str(session['minutes'])+'分钟')
            if session.get('slot_start'):lines.append('时段：'+session['slot_start'][11:16]+'–'+session['slot_end'][11:16]+'（待办中的最新安排优先）')
            lines.extend(session['exercises'])
            for tutorial in session.get('tutorials',[]):lines.append(tutorial['name']+' · '+tutorial['type']+'：'+tutorial['url'])
        if plan.get('rest_days'):lines.append('恢复日：'+'、'.join(plan['rest_days'])+'；正常走动即可，不需要补练。')
        lines.append('其余日子先恢复或轻松活动，不要求每天高强度训练。')
        lines.append('已记录计划；实际时段和日历同步以待办状态为准。' if plan['committed'] else '这是草案，尚未写入日历。说“确认安排训练计划”后，按你授权的空闲快照安排；没有新鲜空档时保留待排，不猜时间。')
        return '\n'.join(lines)
    def commit(self,owner,ident,source_task=''):
        p=self.profile(owner);plan=self.plan(owner)
        if not plan or plan['id']!=ident:raise ValueError('训练草案已变化，请重新查看')
        if not p['adult']:raise ValueError('请先在训练资料确认已成年；未成年训练应结合监护人或教练指导。')
        if p['restrictions'].strip() not in ('无','没有','无伤病','目前没有伤病'):raise ValueError('请先核对伤病或限制，再采用通用恢复草案。')
        if any(not p[k].strip() for k in ('goal','sport','experience','training_schedule')):raise ValueError('请先补齐目标、运动背景、当前训练情况和专项安排。')
        if plan['profile_hash']!=hashlib.sha256(json.dumps(p,sort_keys=True,ensure_ascii=False).encode()).hexdigest():raise ValueError('训练资料已变化，请重新生成草案')
        zone=validate_zone(plan['time_zone']);today=dt.datetime.fromtimestamp(time.time(),zone).date()
        if not plan['committed'] and (time.time()-plan['created']>86400 or dt.date.fromisoformat(plan['sessions'][0]['day'])<today):raise ValueError('训练草案已过期，请重新生成')
        if not plan['committed']:
            now=time.time()
            with self.store.db() as c:
                c.execute('BEGIN IMMEDIATE')
                latest=c.execute('SELECT id,payload FROM fitness_plans WHERE owner=?',(owner,)).fetchone()
                if not latest or latest['id']!=plan['id']:raise ValueError('训练草案已变化，请重新查看')
                plan=json.loads(latest['payload'])
                if not plan['committed']:
                    profile=c.execute('SELECT payload FROM fitness_profiles WHERE owner=?',(owner,)).fetchone()
                    if not profile or hashlib.sha256(json.dumps(json.loads(profile['payload']),sort_keys=True,ensure_ascii=False).encode()).hexdigest()!=plan['profile_hash']:raise ValueError('训练资料已变化，请重新生成草案')
                    for session in plan['sessions']:
                        ident=str(uuid.uuid5(uuid.NAMESPACE_URL,owner+':fitness:'+plan['id']+':'+session['key']))
                        notes='PersonalOS 恢复训练\n'+'\n'.join(session['exercises'])+'\n'+plan['guidance']
                        notes+='\n'+'\n'.join(t['name']+' · '+t['type']+'：'+t['url'] for t in session.get('tutorials',[]))
                        c.execute('INSERT INTO todos(id,owner,parent_id,kind,title,notes,deadline,completed,revision,created,updated,source_task) VALUES(?,?,NULL,?,?,?,NULL,0,1,?,?,?)',(ident,owner,'task',session['title'],notes,now,now,source_task))
                        session['todo_id']=ident;day=dt.date.fromisoformat(session['day']);low=dt.datetime.combine(day,dt.time(18 if session.get('evening') else 11),zone).timestamp();high=dt.datetime.combine(day,dt.time(22),zone).timestamp()
                        if session.get('slot_start'):
                            from auto_planner import instant
                            low=instant(session['slot_start']);high=instant(session['slot_end'])
                        c.execute('INSERT INTO todo_estimates VALUES(?,?,?)',(owner,ident,session['minutes']))
                        c.execute('INSERT INTO todo_schedule_windows VALUES(?,?,?,?,0,?)',(owner,ident,low,high,now))
                    plan['committed']=True;c.execute('UPDATE fitness_plans SET payload=?,updated=? WHERE owner=?',(json.dumps(plan,ensure_ascii=False),now,owner))
                    self.store._audit(c,source_task,'fitness_plan_recorded',{'count':len(plan['sessions'])})
        self.store.planner.reconcile(owner)
        return {'message':'训练待办已记录，正在按可用日历快照排程；尚不能声称已写入 Apple 日历。','todo_ids':[x['todo_id'] for x in plan['sessions']]}
    def skip(self,row):
        owner=row['owner'];self.authorize(owner);plan=self.plan(owner)
        with self.store.db() as c:prior=c.execute('SELECT result FROM fitness_operations WHERE owner=? AND task=?',(owner,row['id'])).fetchone()
        if prior:return json.loads(prior['result'])
        if not plan or not plan['committed']:return self.reply('今天可以休息，不必明天补两倍。目前没有已确认的训练计划，我没有改日历。')
        zone=validate_zone(plan['time_zone']);today=dt.datetime.fromtimestamp(row['created'],zone).date()
        selected=[x for x in plan['sessions'] if x['day']==today.isoformat()]
        if len(selected)!=1:return self.reply('今天没有唯一对应的已安排训练。请告诉我具体训练名称；没有改动其他任务。')
        now=time.time();changed=[]
        with self.store.db() as c:
            c.execute('BEGIN IMMEDIATE')
            current=c.execute('SELECT id,payload FROM fitness_plans WHERE owner=?',(owner,)).fetchone()
            if not current or current['id']!=plan['id'] or json.loads(current['payload'])!=plan:raise ValueError('训练计划已变化，请重试')
            target=c.execute('SELECT completed FROM todos WHERE owner=? AND id=?',(owner,selected[0]['todo_id'])).fetchone()
            if not target or target['completed']:return self.reply('今天的训练已完成或已不存在，没有延期后续训练。')
            for session in plan['sessions']:
                if session['day']<today.isoformat():continue
                todo=c.execute('SELECT * FROM todos WHERE owner=? AND id=?',(owner,session['todo_id'])).fetchone()
                if not todo or todo['completed']:continue
                slot=c.execute('SELECT a.*,e.last_op FROM auto_schedule a LEFT JOIN calendar_entries e ON e.id=a.entry_id WHERE a.owner=? AND a.todo_id=?',(owner,todo['id'])).fetchone()
                if slot and (slot['state'] in ('manual','started') or slot['last_op']=='resolve'):raise ValueError('训练包含已经开始或手动安排的时段，请在任务详情调整；未自动改动')
                day=dt.date.fromisoformat(session['day'])+dt.timedelta(days=1);session['day']=day.isoformat()
                low=dt.datetime.combine(day,dt.time(18 if session.get('evening') else 11),zone).timestamp();high=dt.datetime.combine(day,dt.time(22),zone).timestamp()
                if session.get('slot_start'):
                    session['slot_start']=(dt.datetime.fromisoformat(session['slot_start'])+dt.timedelta(days=1)).isoformat()
                    session['slot_end']=(dt.datetime.fromisoformat(session['slot_end'])+dt.timedelta(days=1)).isoformat()
                    low=dt.datetime.fromisoformat(session['slot_start']).timestamp();high=dt.datetime.fromisoformat(session['slot_end']).timestamp()
                c.execute('INSERT OR REPLACE INTO todo_schedule_windows VALUES(?,?,?,?,0,?)',(owner,todo['id'],low,high,now))
                c.execute('UPDATE todos SET revision=revision+1,updated=? WHERE owner=? AND id=?',(now,owner,todo['id']))
                if slot:
                    c.execute("UPDATE auto_schedule SET state='reschedule',reason='训练休息顺延，等待重新排程',updated=? WHERE owner=? AND todo_id=?",(now,owner,todo['id']))
                    c.execute("UPDATE calendar_entries SET status='cancelled',device_sync='pending',revision=revision+1,updated=? WHERE owner=? AND id=?",(now,owner,slot['entry_id']))
                changed.append(todo['id'])
            if not changed:return self.reply('今天的训练已标记完成或已不存在，没有再次延期。')
            response=self.reply('已将今天及后续未完成训练顺延一天，保留原有间隔，不增加补课训练量。日历尚待手机同步。',todo_ids=changed)
            c.execute('UPDATE fitness_plans SET payload=?,updated=? WHERE owner=?',(json.dumps(plan,ensure_ascii=False),now,owner))
            c.execute('INSERT INTO fitness_operations VALUES(?,?,?)',(owner,row['id'],json.dumps(response,ensure_ascii=False)))
        self.store.planner.reconcile(owner)
        return response
