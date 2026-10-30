#!/usr/bin/env python3
"""Local-only Personal OS. Python 3.9+, no third-party dependencies."""
from contextlib import contextmanager
import argparse, datetime as dt, hashlib, hmac, json, os, pathlib, platform, secrets, shutil, sqlite3, threading, time, urllib.request, urllib.parse, uuid, re, stat
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from push import APNsProvider, PushDispatcher, validate_registration
from remote_sources import ProjectRemoteSource, PROJECT_URI
from file_access import FileAccessService, FileAccessError
from voice import VoiceService, VoiceError, MAX_BODY_BYTES
from calendar_journal import CalendarJournal, CalendarError, journal_intent, validate_zone, DEFAULT_ZONE
from reminder_time import parse_reminder_time, ReminderNeedsDetails
from reminder_slots import choose_reminder_slot
from assistant_brain import AssistantBrain, conversational_request
from todos import TodoStore, TodoError, backlog_request
from deadline_intent import deadline_request, unscheduled_reminder, simple_deadline_item
from canvas_sync import CanvasSync
from auto_planner import AutoPlanner
from live_info import LiveInfo
from documents import Documents
from mac_actions import MacActions, spoken_mac_action, spoken_phone_action
from voice_sessions import VoiceSessions, validate_fields
from spoken_reply import task_speech
from speech_output import SpeechOutput
from fitness import Fitness

VERSION = "0.13"
MODEL = "qwen3.5:9b"

class ConflictError(ValueError):
    pass

def task_kind(text):
    if spoken_mac_action(text) or spoken_phone_action(text) or re.match(r'^(?:请)?(?:帮我)?回复(?:一下)?(?:这条消息)?[：:]',text):return "plan"
    if (deadline_request(text) or unscheduled_reminder(text)) and not conversational_request(text): return "plan"
    if backlog_request(text) and not conversational_request(text) and re.search(r'提醒|\bremind(?:er)?\b',text,re.I) and not re.search(r'新增|添加|记录|记下|拆分|拆成|分解',text):
        return 'reminder'
    if backlog_request(text): return 'plan'
    if conversational_request(text): return 'plan'
    journal=journal_intent(text)
    if journal in ('done','plan'): return 'calendar'
    if journal=='blocked': return 'plan'
    if "提醒" in text or re.search(r'\bremind(?:er)?\b',text,re.I): return "reminder"
    if "整理" in text: return "organize"
    if "PROJECT" in text.upper() or "查找" in text or "检索" in text: return "search"
    if "状态" in text: return "status"
    if re.search(r'\b(?:(?:device|system|computer)\s+status|check\s+(?:(?:my|the)\s+)?(?:(?:device|system|computer)\s+)?status)\b',text,re.I): return "status"
    return "plan"

def parse_when(value):
    try:
        parsed = dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
        if parsed.tzinfo is None: raise ValueError()
        return parsed.timestamp()
    except (ValueError, TypeError, AttributeError, OverflowError):
        raise ValueError("时间必须是含时区的 ISO8601 格式") from None

def validate_calendar(raw):
    if len(raw.encode("utf-8")) > 8192: raise ValueError("日历上下文最多 8KB")
    try: context = json.loads(raw)
    except (ValueError, TypeError): raise ValueError("日历上下文必须是有效的 JSON 字符串") from None
    if isinstance(context, list): context = {"events": context}
    if not isinstance(context, dict) or set(context) - {"events", "date", "timeZone", "timezone", "range_start", "range_end", "truncated"}:
        raise ValueError("日历上下文字段无效")
    if "truncated" in context and not isinstance(context["truncated"], bool): raise ValueError("日历截断标记无效")
    events = context.get("events")
    if not isinstance(events, list) or len(events) > 50: raise ValueError("最多提供 50 条日历事件")
    for field in ("date", "timeZone", "timezone", "range_start", "range_end"):
        if field in context and (not isinstance(context[field], str) or len(context[field]) > 100): raise ValueError("日历范围无效")
    for field in ("range_start", "range_end"):
        if field in context: parse_when(context[field])
    if "date" in context:
        try:
            value = context["date"]
            if re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
                dt.date.fromisoformat(value)
            elif re.fullmatch(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:\d{2})", value):
                # Legacy iPhone builds sent local day start as an ISO timestamp.
                # Keep it intact with timeZone, avoiding an incorrect UTC day shift.
                parse_when(value)
            else: raise ValueError()
        except ValueError: raise ValueError("日历日期无效") from None
    for event in events:
        if not isinstance(event, dict) or set(event) - {"title", "start", "end", "isAllDay", "all_day"}: raise ValueError("日历事件字段无效")
        if not isinstance(event.get("title"), str) or len(event["title"]) > 300: raise ValueError("日历事件标题无效")
        start, end = parse_when(event.get("start")), parse_when(event.get("end"))
        if end < start: raise ValueError("日历事件结束时间早于开始时间")
        for field in ("isAllDay", "all_day"):
            if field in event and not isinstance(event[field], bool): raise ValueError("全天事件标记无效")
    return context

def validate_task_body(body):
    allowed = {"text", "root", "query", "due", "request_id", "calendar_context", "include_snippets", "time_zone", "document_ids", "mac_action", "message_draft", "session_id", "reply_to", "input_refs"}
    if not isinstance(body, dict) or set(body) - allowed: raise ValueError("任务字段无效")
    if any(not isinstance(value, str) for value in body.values()): raise ValueError("任务字段必须是字符串")
    if not 0 < len(body.get("text", "").strip()) <= 4000: raise ValueError("任务文字无效")
    if len(body.get("root", "")) > 4096 or len(body.get("query", "")) > 200: raise ValueError("文件参数过长")
    if body.get("due"): parse_when(body["due"])
    if 'time_zone' in body: validate_zone(body['time_zone'])
    if body.get("request_id") and not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", body["request_id"]): raise ValueError("request_id 格式无效")
    if "include_snippets" in body and body["include_snippets"] not in ("true", "false"): raise ValueError("include_snippets 必须为 true 或 false")
    for field in ('mac_action','message_draft'):
        if field in body:
            try:
                if len(body[field])>12000 or not isinstance(json.loads(body[field]),dict):raise ValueError()
            except (ValueError,TypeError):raise ValueError('操作请求格式无效') from None
    if sum(bool(body.get(f)) for f in ('mac_action','message_draft','document_ids'))>1:raise ValueError('请一次提交一种操作')
    validate_fields(body)
    clean = dict(body)
    if not any(body.get(k) for k in ('mac_action','message_draft','document_ids')):
        quoted_reply=re.match(r'^(?:请)?(?:帮我)?回复(?:一下)?(?:这条消息)?[：:]\s*(.+)$',body['text'],re.S)
        if quoted_reply:clean['message_draft']=json.dumps({'message':quoted_reply.group(1),'tone':'简洁','instruction':''},ensure_ascii=False)
    if not any(clean.get(k) for k in ('mac_action','message_draft','document_ids')):
        mac_request=spoken_mac_action(body['text'])
        if mac_request:clean['mac_action']=json.dumps(mac_request,ensure_ascii=False)
    if 'document_ids' in body:
        try:
            ids=json.loads(body['document_ids'])
            if not isinstance(ids,list) or not 1<=len(ids)<=3 or len(set(ids))!=len(ids) or any(not isinstance(i,str) or not re.fullmatch(r'[0-9a-f-]{36}',i) for i in ids):raise ValueError()
        except (ValueError,TypeError):raise ValueError('文档附件编号无效') from None
    if body.get("calendar_context"):
        clean["calendar_context"] = json.dumps(validate_calendar(body["calendar_context"]), ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return clean

class Store:
    def __init__(self, home):
        self.home = pathlib.Path(home).expanduser(); self.home.mkdir(parents=True, exist_ok=True); self.home.chmod(0o700)
        self.path = self.home/'state.sqlite3'
        self.remote_source = ProjectRemoteSource(self.home/'project-source.json')
        self.file_access = FileAccessService(self.home)
        with self.db() as c:
            c.executescript('''CREATE TABLE IF NOT EXISTS tasks(id TEXT PRIMARY KEY, body TEXT, state TEXT, result TEXT, plan TEXT, created REAL, due REAL);
            CREATE TABLE IF NOT EXISTS audit(id INTEGER PRIMARY KEY, task TEXT, event TEXT, detail TEXT, at REAL);
            CREATE TABLE IF NOT EXISTS devices(token TEXT PRIMARY KEY, name TEXT);
            CREATE TABLE IF NOT EXISTS pairing(hash TEXT, expires REAL);
            CREATE TABLE IF NOT EXISTS roots(path TEXT PRIMARY KEY);
            CREATE INDEX IF NOT EXISTS tasks_state_created ON tasks(state,created);
            CREATE INDEX IF NOT EXISTS tasks_state_due ON tasks(state,due);
            CREATE TABLE IF NOT EXISTS task_requests(device TEXT, request_id TEXT, task TEXT, body_hash TEXT, PRIMARY KEY(device,request_id));
            CREATE TABLE IF NOT EXISTS push_devices(device TEXT PRIMARY KEY, token TEXT, environment TEXT, registered REAL, active INTEGER);
            CREATE TABLE IF NOT EXISTS local_reminders(task TEXT, device TEXT, scheduled INTEGER, updated REAL, PRIMARY KEY(task,device));
            CREATE TABLE IF NOT EXISTS notification_events(event_key TEXT PRIMARY KEY, task TEXT, event TEXT, created REAL);
            CREATE TABLE IF NOT EXISTS notification_outbox(id TEXT PRIMARY KEY, event_key TEXT, task TEXT, event TEXT, device TEXT,
                state TEXT, attempts INTEGER, next_attempt REAL, created REAL, expires REAL, last_error TEXT,
                UNIQUE(event_key,device));
            CREATE INDEX IF NOT EXISTS outbox_state_due ON notification_outbox(state,next_attempt);''')
            c.execute('CREATE TABLE IF NOT EXISTS voice_jobs(task TEXT PRIMARY KEY,state TEXT,mime TEXT,bytes INTEGER,created REAL)')
            CalendarJournal.initialize(c)
            AssistantBrain.initialize(c)
            TodoStore.initialize(c)
            AutoPlanner.initialize(c)
            from task_editing import TaskEditing
            TaskEditing.initialize(c)
            LiveInfo.initialize(c)
            Documents.initialize(c)
            MacActions.initialize(c)
            VoiceSessions.initialize(c)
            SpeechOutput.initialize(c)
            Fitness.initialize(c)
            from backlog_reschedule import BacklogReschedule
            BacklogReschedule.initialize(c)
            from daily_rollover import DailyRollover
            DailyRollover.initialize(c)

        self.path.chmod(0o600)
        self.voice = VoiceService(self)
        self.calendar = CalendarJournal(self)
        self.brain = AssistantBrain(self, MODEL)
        self.todos = TodoStore(self)
        self.canvas = CanvasSync(self)
        self.planner = AutoPlanner(self)
        self.live = LiveInfo(self)
        self.documents = Documents(self)
        self.mac = MacActions(self)
        self.sessions = VoiceSessions(self)
        self.speech = SpeechOutput(self)
        self.fitness = Fitness(self)
    @contextmanager
    def db(self):
        c=sqlite3.connect(str(self.path), timeout=15); c.row_factory=sqlite3.Row
        try:
            with c: yield c
        finally: c.close()
    def audit(self, task, event, detail=''):
        with self.db() as c: c.execute('INSERT INTO audit(task,event,detail,at) VALUES(?,?,?,?)',(task,event,json.dumps(detail,ensure_ascii=False),time.time()))
    def tasks(self, device=None):
        with self.db() as c:
            internal={r[0] for r in c.execute('SELECT session FROM assistant_internal_sessions')}
            rows=c.execute("SELECT * FROM tasks WHERE owner=? ORDER BY created DESC LIMIT 200",(device,)) if device is not None else c.execute('SELECT * FROM tasks ORDER BY created DESC LIMIT 200')
            # Preserve the existing shared non-calendar task history. Imported
            # documents and their generated result text are device-private.
            return [dict(r) for r in rows if (device is None or json.loads(r['body']).get('session_id') not in internal or r['state']=='scheduled') and (device is None or r['owner']==device or not (any(json.loads(r['body']).get(k) for k in ('document_ids','mac_action','message_draft','session_id')) or json.loads(r['plan']).get('kind') in ('mac_action','phone_action','daily_rollover')))]
    @staticmethod
    def _audit(c, task, event, detail=''):
        c.execute('INSERT INTO audit(task,event,detail,at) VALUES(?,?,?,?)',
                  (task,event,json.dumps(detail,ensure_ascii=False),time.time()))
    def _notify(self, c, ident, event):
        if event not in ('completed','failed','needs_review','needs_details','awaiting_confirmation','reminder_due'): return
        task=c.execute('SELECT owner,body FROM tasks WHERE id=?',(ident,)).fetchone()
        internal=task and c.execute('SELECT 1 FROM assistant_internal_sessions WHERE session=?',(json.loads(task['body']).get('session_id'),)).fetchone()
        if internal and event!='reminder_due':return
        now=time.time(); key=ident+':'+event
        inserted=c.execute('INSERT OR IGNORE INTO notification_events VALUES(?,?,?,?)',(key,ident,event,now)).rowcount
        if not inserted: return
        devices=c.execute('SELECT p.device FROM push_devices p JOIN devices d ON d.token=p.device WHERE p.active=1').fetchall()
        task=c.execute('SELECT owner,body FROM tasks WHERE id=?',(ident,)).fetchone()
        private_owner=task['owner'] if task else None
        for row in devices:
            device=row['device']
            if device!=private_owner:continue
            local=c.execute('SELECT scheduled FROM local_reminders WHERE task=? AND device=?',(ident,device)).fetchone()
            state='suppressed' if event=='reminder_due' and local and local['scheduled'] else 'pending'
            notification_id=str(uuid.uuid5(uuid.NAMESPACE_URL,key+':'+device))
            c.execute('INSERT OR IGNORE INTO notification_outbox VALUES(?,?,?,?,?,?,?,?,?,?,?)',
                      (notification_id,key,ident,event,device,state,0,now,now,now+86400,'local_reminder' if state=='suppressed' else ''))
    def update(self, ident, **kw):
        if not kw or set(kw)-{'state','result','plan','due'}: raise ValueError('无效任务更新')
        with self.db() as c:
            c.execute('BEGIN IMMEDIATE')
            c.execute('UPDATE tasks SET '+','.join(k+'=?' for k in kw)+' WHERE id=?',list(kw.values())+[ident])
            if 'state' in kw: self._notify(c,ident,kw['state'])
    def submit(self, body, device=''):
        body=validate_task_body(body)
        for ident in json.loads(body.get('document_ids','[]')):self.documents.info(device,ident)
        request_id=body.get('request_id','')
        canonical={k:v for k,v in body.items() if k!='request_id'}
        digest=hashlib.sha256(json.dumps(canonical,ensure_ascii=False,sort_keys=True,separators=(',',':')).encode()).hexdigest()
        ident=str(uuid.uuid4())
        with self.db() as c:
            c.execute('BEGIN IMMEDIATE')
            if request_id:
                previous=c.execute('SELECT task,body_hash FROM task_requests WHERE device=? AND request_id=?',(device,request_id)).fetchone()
                if previous:
                    if previous['body_hash']!=digest: raise ConflictError('相同 request_id 对应不同任务，请使用新的 request_id')
                    return previous['task']
            self.sessions.attach(c,device,body,ident)
            c.execute('INSERT INTO tasks(id,body,state,result,plan,created,due,owner) VALUES(?,?,?,?,?,?,?,?)',(ident,json.dumps(body,ensure_ascii=False),'queued','','{}',time.time(),None,device))
            if request_id: c.execute('INSERT INTO task_requests VALUES(?,?,?,?)',(device,request_id,ident,digest))
            self._audit(c,ident,'submitted')
        return ident
    def roots(self):
        with self.db() as c:
            local = [{'path':r['path'],'name':pathlib.Path(r['path']).name or r['path']} for r in c.execute('SELECT path FROM roots ORDER BY path')]
        return local + self.remote_source.roots()
    def task(self, ident, device=None):
        with self.db() as c:
            row=c.execute('SELECT * FROM tasks WHERE id=?',(ident,)).fetchone()
            if device is not None and row and (row['owner']!=device or c.execute('SELECT 1 FROM calendar_entries WHERE id=? AND owner<>?',(ident,device)).fetchone() or ((any(json.loads(row['body']).get(k) for k in ('document_ids','mac_action','message_draft','session_id')) or json.loads(row['plan']).get('kind') in ('mac_action','phone_action','daily_rollover')) and row['owner']!=device)): raise CalendarError('任务不存在。',404)
        if not row: raise KeyError('任务不存在')
        return dict(row)
    def audits(self, ident=None):
        with self.db() as c:
            if ident:
                if not c.execute('SELECT 1 FROM tasks WHERE id=?',(ident,)).fetchone(): raise KeyError('任务不存在')
                return [dict(r) for r in c.execute('SELECT * FROM audit WHERE task=? ORDER BY id DESC LIMIT 500',(ident,))]
            return [dict(r) for r in c.execute('SELECT * FROM audit ORDER BY id DESC LIMIT 500')]
    def register_push(self, device, token, environment):
        token,environment=validate_registration(token,environment)
        with self.db() as c:
            c.execute('BEGIN IMMEDIATE')
            if not c.execute('SELECT 1 FROM devices WHERE token=?',(device,)).fetchone(): raise ValueError('设备认证失效')
            # A phone may re-pair and receive a new auth token; do not send twice to the same APNs token.
            c.execute('DELETE FROM push_devices WHERE token=? AND environment=? AND device<>?',(token,environment,device))
            c.execute('INSERT INTO push_devices VALUES(?,?,?,?,1) ON CONFLICT(device) DO UPDATE SET token=excluded.token,environment=excluded.environment,registered=excluded.registered,active=1',
                      (device,token,environment,time.time()))
            self._audit(c,'','push_registered',{'environment':environment})
    def mark_local_reminder(self, ident, device, scheduled):
        if scheduled not in ('true','false'): raise ValueError('scheduled 必须为 true 或 false')
        with self.db() as c:
            c.execute('BEGIN IMMEDIATE')
            row=c.execute('SELECT body,state FROM tasks WHERE id=?',(ident,)).fetchone()
            if not row: raise KeyError('任务不存在')
            if task_kind(json.loads(row['body'])['text'])!='reminder' and not (json.loads(row['body']).get('due') or (unscheduled_reminder(json.loads(row['body'])['text']) and row['state'] in ('needs_details','scheduled'))): raise ValueError('仅提醒任务可本地排程')
            if scheduled=='true' and row['state'] not in ('queued','running','scheduled'): raise ConflictError('提醒已结束，不能再登记本地排程')
            c.execute('INSERT INTO local_reminders VALUES(?,?,?,?) ON CONFLICT(task,device) DO UPDATE SET scheduled=excluded.scheduled,updated=excluded.updated',
                      (ident,device,int(scheduled=='true'),time.time()))
            if scheduled=='true': c.execute("UPDATE notification_outbox SET state='suppressed',last_error='local_reminder' WHERE task=? AND device=? AND event='reminder_due' AND state IN ('pending','retry')",(ident,device))
            self._audit(c,ident,'local_reminder_'+('scheduled' if scheduled=='true' else 'removed'))
    def resolve_reminder(self, ident, device, body):
        """Complete an owned reminder without changing its original submission."""
        if not isinstance(body,dict) or set(body)!={'due','time_zone'} or any(not isinstance(value,str) for value in body.values()):
            raise ValueError('请提供提醒时间 due 和手机时区 time_zone，且均为字符串')
        if not re.fullmatch(r'\d{4}-\d{2}-\d{2}T\d{2}:\d{2}(?::\d{2}(?:\.\d{1,6})?)?(?:Z|[+-]\d{2}:\d{2})',body['due']):
            raise ValueError('时间必须是含时区的 ISO8601 格式')
        due=parse_when(body['due']); zone_name=body['time_zone']; zone=validate_zone(zone_name)
        # The phone may send UTC (Z); display and persist the chosen instant in
        # its explicitly supplied zone rather than using the Mac's local zone.
        try: chosen=dt.datetime.fromtimestamp(due,zone)
        except (ValueError,OverflowError): raise ValueError('提醒日期超出可用范围') from None
        with self.db() as c:
            c.execute('BEGIN IMMEDIATE')
            row=c.execute('SELECT * FROM tasks WHERE id=? AND owner=?',(ident,device)).fetchone()
            if not device or not row: raise CalendarError('任务不存在。',404)
            if task_kind(json.loads(row['body'])['text'])!='reminder' and not (json.loads(row['body']).get('due') or (unscheduled_reminder(json.loads(row['body'])['text']) and row['state'] in ('needs_details','scheduled'))):
                raise ConflictError('只有待补充时间的提醒可以使用此操作')
            plan=json.loads(row['plan'])
            # Retry only the successful, still scheduled manual resolution.
            # Cancelled/completed reminders must never be revived by a retry.
            if row['state']=='scheduled' and row['due']==due and plan.get('strategy')=='manual_resolution' and plan.get('time_zone')==zone_name:
                return {key:value for key,value in dict(row).items() if key!='owner'}
            if row['state']!='needs_details':
                raise ConflictError('提醒状态已改变，请刷新记录后查看；不能重复修改或恢复已结束的提醒')
            if due<=time.time(): raise ValueError('提醒时间必须在未来')
            plan={'kind':'reminder','strategy':'manual_resolution','due':chosen.isoformat(),'time_zone':zone_name}
            result='已安排 '+chosen.strftime('%Y-%m-%d %H:%M')+'（'+zone_name+'）。到时更新任务记录，通知以手机权限和推送配置为准。'
            c.execute("UPDATE tasks SET state='scheduled',due=?,result=?,plan=? WHERE id=?",(due,result,json.dumps(plan,ensure_ascii=False),ident))
            self._audit(c,ident,'reminder_resolved',{'due':chosen.isoformat(),'time_zone':zone_name})
            updated=c.execute('SELECT * FROM tasks WHERE id=?',(ident,)).fetchone()
            return {key:value for key,value in dict(updated).items() if key!='owner'}
    def process_reminders(self, now=None, only_id=None):
        now=time.time() if now is None else now
        with self.db() as c:
            c.execute('BEGIN IMMEDIATE')
            for row in c.execute("SELECT id,body,created FROM tasks WHERE state='queued' ORDER BY created").fetchall():
                if only_id and row['id']!=only_id:continue
                body=json.loads(row['body'])
                if body.get('_voice_id'): continue
                if not only_id and self.sessions.engine.enabled() and body.get('session_id'):continue
                if any(body.get(k) for k in ('mac_action','message_draft','document_ids')) or (task_kind(body['text'])!='reminder' and not body.get('due')): continue
                ident=row['id']
                try:
                    zone_name=body.get('time_zone',DEFAULT_ZONE)
                    zone=validate_zone(zone_name)
                    plan={'kind':'reminder','strategy':'explicit_time'}
                    if body.get('due'): due=parse_when(body['due'])
                    else:
                        try:
                            due=parse_reminder_time(body['text'],row['created'],zone_name)
                        except ReminderNeedsDetails as missing:
                            if missing.code != 'missing_time': raise
                            try:
                                context=json.loads(body['calendar_context']) if body.get('calendar_context') else None
                                # The phone collects this opt-in snapshot immediately
                                # before submitting. Never treat an old queue item as fresh.
                                slot=choose_reminder_slot(body['text'],row['created'],zone_name,context,now,
                                                          snapshot_at=row['created'])
                            except ValueError as error:
                                raise ReminderNeedsDetails(str(error)) from None
                            due=parse_when(slot['due'])
                            plan={'kind':'reminder','strategy':'calendar_gap','reason':slot['reason'],
                                  'duration_minutes':15,'buffer_minutes':10}
                    if due<=now: raise ValueError('提醒时间必须在未来')
                    result='已安排 '+dt.datetime.fromtimestamp(due,zone).strftime('%Y-%m-%d %H:%M')+'。'
                    if plan.get('reason'): result+=plan['reason']
                    result+='到时更新任务记录，通知以手机权限和推送配置为准。'
                    c.execute("UPDATE tasks SET state='scheduled',due=?,result=?,plan=? WHERE id=?",(due,result,json.dumps(plan,ensure_ascii=False),ident))
                    self._audit(c,ident,'reminder_scheduled',due)
                    if plan['strategy']=='calendar_gap': self._audit(c,ident,'reminder_calendar_slot',plan)
                except ReminderNeedsDetails as error:
                    c.execute("UPDATE tasks SET state='needs_details',due=NULL,result=? WHERE id=?",(str(error),ident))
                    self._audit(c,ident,'needs_details',str(error)); self._notify(c,ident,'needs_details')
                except ValueError as error:
                    c.execute("UPDATE tasks SET state='failed',result=? WHERE id=?",(str(error),ident))
                    self._audit(c,ident,'failed',str(error)); self._notify(c,ident,'failed')
            for row in c.execute("SELECT id FROM tasks WHERE state='scheduled' AND due<=?",(now,)).fetchall():
                c.execute("UPDATE tasks SET state='completed',result='提醒已到时间：打开任务记录查看你安排的内容。' WHERE id=?",(row['id'],))
                self._audit(c,row['id'],'reminder_due'); self._notify(c,row['id'],'reminder_due')
    def root(self, raw):
        if isinstance(raw,str) and raw.startswith('project://'): raise ValueError('PROJECT · MacBook Air 是只读资料源，不能整理或修改远端文件')
        p=pathlib.Path(raw).expanduser().resolve(strict=True)
        with self.db() as c: roots=[pathlib.Path(r[0]) for r in c.execute('SELECT path FROM roots')]
        if p not in roots: raise ValueError('目录未在 Mac 本机授权；请用 grant 命令授权精确目录')
        return p
    def plan_files(self, raw):
        root=self.root(raw); moves=[]
        for p in sorted(root.iterdir()):
            if p.is_symlink() or not p.is_file() or p.name.startswith('.'): continue
            if p.stat().st_size>50*1024*1024: raise ValueError('MVP 一次只整理小于 50MB 的文件')
            group=p.suffix.lower().lstrip('.') or 'no-extension'
            dest=root/('PersonalOS-'+group)/p.name
            if dest.parent.is_symlink() or dest.exists(): raise ValueError('目标冲突或符号链接；请先处理：'+p.name)
            moves.append({'src':str(p),'dst':str(dest),'sha':hashlib.sha256(p.read_bytes()).hexdigest()})
            if len(moves)>200: raise ValueError('一次最多整理 200 个文件')
        return {'root':str(root),'moves':moves}
    def move(self, ident, plan, undo=False):
        root=self.root(plan['root']); moves=list(reversed(plan['moves'])) if undo else plan['moves']
        # Preflight everything before first mutation. Refuse modified files and linked parents.
        for m in moves:
            src=pathlib.Path(m['dst'] if undo else m['src']); dst=pathlib.Path(m['src'] if undo else m['dst'])
            if src.is_symlink() or src.parent.is_symlink() or dst.parent.is_symlink() or not src.is_file() or dst.exists(): raise ValueError('文件已改变或目标冲突，停止操作')
            if root not in src.resolve().parents or root not in dst.resolve().parents: raise ValueError('越界路径')
            if hashlib.sha256(src.read_bytes()).hexdigest()!=m['sha']: raise ValueError('文件内容改变，停止操作')
        completed=[]
        try:
            for m in moves:
                src=pathlib.Path(m['dst'] if undo else m['src']); dst=pathlib.Path(m['src'] if undo else m['dst'])
                dst.parent.mkdir(exist_ok=True)
                # link then unlink: never overwrite an existing destination.
                self.audit(ident,'move_intent',{'src':str(src),'dst':str(dst)})
                os.link(src,dst); src.unlink(); completed.append((src,dst))
                self.audit(ident,'undo_move' if undo else 'move',{'src':str(src),'dst':str(dst)})
        except Exception:
            for src,dst in reversed(completed):
                if not src.exists(): os.link(dst,src); dst.unlink()
            raise
        if undo:
            for parent in {pathlib.Path(m['dst']).parent for m in moves}:
                try: parent.rmdir()
                except OSError: pass
    def search_files(self, raw, query, snippets=False):
        if raw == PROJECT_URI: return self.remote_source.search(query)
        root=self.root(raw)
        if not query.strip(): raise ValueError('请输入检索关键词')
        term=query.casefold(); matches=[]; scanned=0
        for directory, dirs, files in os.walk(root,followlinks=False):
            base=pathlib.Path(directory)
            dirs[:]=[name for name in dirs if not (base/name).is_symlink()]
            for name in files:
                scanned+=1
                if scanned>10000: raise ValueError('目录超过 10000 个文件，请授权更小的目录后检索，以保证最新结果完整排序')
                candidate=base/name
                if candidate.is_symlink() or not candidate.is_file() or root not in candidate.resolve().parents: continue
                if term not in name.casefold(): continue
                info=candidate.stat()
                if not stat.S_ISREG(info.st_mode): continue
                matches.append({'path':str(candidate),'modified':info.st_mtime})
        matches.sort(key=lambda item:(-item['modified'],item['path']))
        hits=matches[:100]
        if snippets:
            text_types={'.txt','.md','.csv','.json','.py','.swift','.js','.ts','.html','.css','.yaml','.yml'}
            for hit in hits:
                candidate=pathlib.Path(hit['path'])
                if candidate.suffix.lower() not in text_types: continue
                try:
                    if candidate.is_symlink() or root not in candidate.resolve().parents: continue
                    descriptor=os.open(str(candidate),os.O_RDONLY|getattr(os,'O_NOFOLLOW',0))
                    with os.fdopen(descriptor,'rb') as f:
                        info=os.fstat(f.fileno())
                        if not stat.S_ISREG(info.st_mode) or info.st_size>1024*1024: continue
                        data=f.read(4096)
                    if b'\x00' not in data: hit['snippet']=' '.join(data.decode('utf-8',errors='replace').split())[:500]
                except OSError: continue
        return {'scope':'仅授权目录按文件名检索，按修改时间从新到旧；不是互联网资料',
                'hits':hits,'matched_count':len(matches),'truncated':len(matches)>100}
    def act(self, ident, action):
        if self.sessions.engine.task_action(ident,action):return
        if action in ('approve','undo') and json.loads(self.task(ident)['plan']).get('kind')=='mac_action':
            return self.mac.act(ident,action)
        allowed={'approve':{'awaiting_confirmation'},'undo':{'completed'},'cancel':{'queued','awaiting_confirmation','scheduled','needs_details'}}[action]
        with self.db() as c:
            c.execute('BEGIN IMMEDIATE')
            row=c.execute('SELECT * FROM tasks WHERE id=?',(ident,)).fetchone()
            if not row or row['state'] not in allowed: raise ValueError('任务状态不允许该操作；执行中的任务无法取消')
            plan=json.loads(row['plan'])
            if action in ('approve','undo') and 'moves' not in plan: raise ValueError('该任务不可执行此操作')
            if action=='cancel':
                c.execute("UPDATE tasks SET state='cancelled',result='任务已取消' WHERE id=?",(ident,))
                if c.execute("UPDATE voice_jobs SET state='cancelled' WHERE task=? AND state='queued'",(ident,)).rowcount:
                    self.voice.cleanup(ident)
                self._audit(c,ident,'cancel'); return
            c.execute("UPDATE tasks SET state='running' WHERE id=?",(ident,)); self._audit(c,ident,action)
        try:
            self.move(ident,plan,action=='undo')
            self.update(ident,state='completed' if action=='approve' else 'undone',result='整理完成，可撤销' if action=='approve' else '已撤销，文件恢复原位')
        except Exception as e:
            self.update(ident,state='needs_review',result=str(e)); self.audit(ident,'action_failed',str(e)); raise

class ReminderScheduler:
    def __init__(self, store, stop=None): self.s=store; self.stop=stop or threading.Event()
    def run(self):
        while not self.stop.wait(.2):
            try: self.s.process_reminders()
            except sqlite3.OperationalError: self.stop.wait(.5)

class Worker:
    def __init__(self, store): self.s=store; self.stop=threading.Event()
    def run(self):
        scheduler=threading.Thread(target=ReminderScheduler(self.s,self.stop).run,daemon=True)
        scheduler.start()
        try:
            while not self.stop.wait(.2):
                self.s.sessions.engine.tick(self.execute_legacy)
                with self.s.db() as c:
                    c.execute('BEGIN IMMEDIATE')
                    rows=c.execute("SELECT * FROM tasks WHERE state='queued' ORDER BY created").fetchall()
                    internal={r[0] for r in c.execute('SELECT session FROM assistant_internal_sessions')}
                    row=next((r for r in rows if json.loads(r['body']).get('session_id') not in internal and ((self.s.sessions.engine.enabled() and json.loads(r['body']).get('session_id')) or any(json.loads(r['body']).get(k) for k in ('mac_action','message_draft','document_ids')) or (task_kind(json.loads(r['body'])['text'])!='reminder' and not json.loads(r['body']).get('due')))),None)
                    if row: c.execute("UPDATE tasks SET state='running' WHERE id=?",(row['id'],))
                if row:
                    try: self.execute(dict(row))
                    except Exception as e: self.s.update(row['id'],state='failed',result=str(e)); self.s.audit(row['id'],'failed',str(e))
        finally:
            self.stop.set(); scheduler.join(timeout=2)
    def execute(self, row):
        s=self.s; ident=row['id']; b=json.loads(row['body'])
        if b.get('_voice_id'):
            if b['_voice_id']!=ident: raise ValueError('语音任务引用无效')
            s.voice.transcribe(row); return
        if b.get('session_id'):
            self.s.sessions.run(row,self.execute_legacy); return
        self.execute_legacy(row)
    def execute_legacy(self,row,semantic=False):
        s=self.s;ident=row['id'];b=json.loads(row['body'])
        text=b['text']; s.audit(ident,'planning')
        from daily_rollover import DailyRollover
        daily=DailyRollover(s).command(row)
        if daily is not None:
            s.update(ident,**daily);return
        from failure_evidence import run as failure_explanation
        diagnosed=failure_explanation(s,row)
        if diagnosed is not None:
            s.update(ident,**diagnosed);s.audit(ident,'failure_explanation');return
        from backlog_reschedule import BacklogReschedule
        rescheduled=BacklogReschedule(s).run(row)
        if rescheduled is not None:
            s.update(ident,**rescheduled);s.audit(ident,'backlog_reschedule');return
        from task_outcomes import status_query
        status=status_query(s,row)
        if status is not None:
            s.update(ident,**status);s.audit(ident,status['state']);return
        from live_intent import readonly as live_readonly_request, followup as live_followup_request
        if live_readonly_request(text) or live_followup_request(text):
            outcome=s.brain.run(row);s.update(ident,**outcome);s.audit(ident,outcome['state']);return
        from task_editing import TaskEditing
        edit=TaskEditing(s).run(row)
        if edit is not None:
            s.update(ident,**edit);s.audit(ident,edit['state']);return
        fitness=s.fitness.run(row)
        if fitness is not None:
            s.update(ident,**fitness);s.audit(ident,'fitness_answer');return
        if not any(b.get(k) for k in ('mac_action','message_draft','document_ids')) and (task_kind(text)=='reminder' or b.get('due')):
            s.update(ident,state='queued');s.process_reminders(only_id=ident);return
        if b.get('mac_action'):
            outcome=s.mac.prepare(row,json.loads(b['mac_action']));s.update(ident,**outcome);s.audit(ident,'mac_action_preview');return
        if b.get('document_ids') or b.get('message_draft'):
            outcome=s.brain.run(row);s.update(ident,**outcome);s.audit(ident,outcome['state']);return
        mac_action=None if semantic else spoken_mac_action(text)
        if mac_action:
            s.update(ident,**s.mac.prepare(row,mac_action));s.audit(ident,'mac_action_preview');return
        phone_action=None if semantic else spoken_phone_action(text)
        if phone_action:
            # Legacy foreground clients infer the nearby toggle from query text.
            if phone_action.get('nearby'):phone_action={**phone_action,'query':'附近'+phone_action['query']}
            s.update(ident,state='completed',result='已准备手机地图操作；请在任务详情选择地点，再打开导航。',plan=json.dumps({'kind':'phone_action',**phone_action},ensure_ascii=False));s.audit(ident,'phone_action_prepared');return
        if task_kind(text)=='calendar': s.calendar.create(row); return
        if task_kind(text)=='organize':
            plan=s.plan_files(b.get('root','')); s.update(ident,state='awaiting_confirmation',plan=json.dumps(plan),result='请检查每个移动路径，再确认执行。'); s.audit(ident,'preview',plan); return
        if task_kind(text)=='search' and b.get('root') and re.match(r'^\s*(?:查找|检索|搜索|search\b|find\b)',text,re.I):
            results=s.search_files(b.get('root',''),b.get('query','PROJECT'),b.get('include_snippets')=='true')
            s.update(ident,state='completed',result=json.dumps(results,ensure_ascii=False)); s.audit(ident,'file_search',{'count':len(results['hits'])}); return
        if task_kind(text)=='status':
            result=json.dumps({'platform':platform.platform(),'free_disk_gb':round(shutil.disk_usage(s.home).free/2**30,1)},ensure_ascii=False)
        else:
            outcome=s.brain.run(row)
            s.update(ident, **outcome); s.audit(ident,outcome['state']); return
        s.update(ident,state='completed',result=result); s.audit(ident,'completed')

class Handler(BaseHTTPRequestHandler):
    def setup(self):
        super().setup(); self.connection.settimeout(15)
    def log_message(self,*a): pass # Never log pairing codes or tokens.
    def reply(self, code, body):
        data=json.dumps(body,ensure_ascii=False).encode(); self.send_response(code); self.send_header('Content-Type','application/json; charset=utf-8'); self.send_header('Cache-Control','no-store'); self.send_header('Content-Length',str(len(data))); self.end_headers(); self.wfile.write(data)
    def do_GET(self): self.handle_api()
    def do_POST(self): self.handle_api()
    def handle_api(self):
        s=self.server.store
        if self.path=='/' and self.command=='GET':
            data=pathlib.Path(__file__).with_name('dashboard.html').read_bytes()
            self.send_response(200); self.send_header('Content-Type','text/html; charset=utf-8'); self.send_header('Cache-Control','no-store'); self.send_header('X-Frame-Options','DENY'); self.send_header('Content-Security-Policy',"default-src 'self'; script-src 'unsafe-inline'; style-src 'unsafe-inline'; frame-ancestors 'none'"); self.end_headers(); self.wfile.write(data); return
        try:
            parsed=urllib.parse.urlsplit(self.path); path=parsed.path
            # Authenticate before reading any potentially large audio body.
            device=None
            if not (path=='/pair' and self.command=='POST'):
                token=self.headers.get('Authorization','').removeprefix('Bearer ')
                device=hashlib.sha256(token.encode()).hexdigest()
                with s.db() as c: valid=c.execute('SELECT 1 FROM devices WHERE token=?',(device,)).fetchone()
                if not valid: return self.reply(401,{'error':'需要设备认证'})
            if self.headers.get('Transfer-Encoding'): return self.reply(400,{'error':'不支持此请求编码'})
            size=int(self.headers.get('Content-Length',0))
            limit=MAX_BODY_BYTES if path=='/voice' and self.command=='POST' else 30*1024*1024 if path=='/transfer' and self.command=='POST' else 8*1024*1024 if path=='/documents' and self.command=='POST' else 256*1024 if path=='/planning/context' else 65536
            if size<0 or size>limit: return self.reply(413,{'error':'请求过大'})
            b=json.loads(self.rfile.read(size)) if size else {}
            if not isinstance(b,dict): raise ValueError('请求必须为 JSON 对象')
            if path=='/pair' and self.command=='POST':
                # One short-lived high-entropy code; persisted and single-use.
                digest=hashlib.sha256(str(b.get('code','')).encode()).hexdigest()
                with s.db() as c:
                    c.execute('BEGIN IMMEDIATE')
                    row=c.execute('SELECT * FROM pairing WHERE expires>?',(time.time(),)).fetchone()
                    if not row or not hmac.compare_digest(row['hash'],digest): return self.reply(401,{'error':'配对码无效或过期'})
                    token=secrets.token_urlsafe(32); c.execute('INSERT INTO devices VALUES(?,?)',(hashlib.sha256(token.encode()).hexdigest(),str(b.get('name','iPhone'))[:100])); c.execute('DELETE FROM pairing')
                s.audit('','paired'); return self.reply(200,{'token':token})
            provider=getattr(self.server,'push_provider',None)
            if provider is None: provider=APNsProvider(s.home/'push.json')
            if path=='/files/devices' and self.command=='GET': return self.reply(200,s.file_access.devices())
            if path in ('/files/list','/files/search','/files/read','/files/download') and self.command=='POST':
                operation=path.rsplit('/',1)[1]
                audit_path=b.get('path','') if isinstance(b.get('path',''),str) else ''
                audit_device=b.get('device','') if b.get('device') in ('mini','air') else 'unknown'
                try:
                    result=s.file_access.handle(operation,b)
                except FileAccessError as error:
                    s.audit('','file_'+operation+'_denied',{'device':audit_device,'path':audit_path[:4096],'code':error.code})
                    raise
                detail={'device':audit_device,'path':result['path']}
                if operation in ('list','search'): detail['count']=len(result['entries'])
                elif operation=='read': detail['bytes']=min(result['size'],result['limit_bytes'])
                else: detail.update({'offset':result['offset'],'bytes':result['next_offset']-result['offset']})
                s.audit('','file_'+operation,detail)
                return self.reply(200,result)
            if path=='/status' and self.command=='GET':
                status={'service':'Personal OS','version':VERSION,'model':s.brain.model,
                        'assistant_mode':'local_multiturn_tools','assistant_thinking':True,
                        **provider.status(),**s.remote_source.status(),**s.voice.status()}
                with s.db() as c:
                    status['push_device_registered']=bool(c.execute('SELECT 1 FROM push_devices WHERE device=? AND active=1',(device,)).fetchone())
                    status['push_pending_count']=c.execute("SELECT count(*) FROM notification_outbox WHERE device=? AND state IN ('pending','retry','sending')",(device,)).fetchone()[0]
                return self.reply(200,status)
            if path=='/todos' and self.command=='GET':
                query=urllib.parse.parse_qs(parsed.query,keep_blank_values=True)
                if set(query)-{'cursor'} or any(len(values)!=1 for values in query.values()): raise TodoError('待办分页参数无效')
                return self.reply(200,s.todos.list(device,query.get('cursor',['0'])[0]))
            if path=='/canvas/status' and self.command=='GET': return self.reply(200,s.canvas.status(device))
            if re.fullmatch(r'/todos/[0-9a-f-]{36}/calendar-ack',path) and self.command=='POST':
                if set(b)!={'payload'}:raise TodoError('截止日历回执格式无效')
                return self.reply(200,s.todos.calendar_ack(device,path.split('/')[2],json.loads(b['payload'])))
            if path=='/planning/rollover':
                from daily_rollover import DailyRollover
                daily=DailyRollover(s)
                if self.command=='GET':return self.reply(200,daily.status(device))
                if set(b)!={'enabled'} or b['enabled'] not in ('true','false'):raise ValueError('自动重排开关无效')
                return self.reply(200,daily.configure(device,b['enabled']=='true'))
            if path=='/planning/status' and self.command=='GET':return self.reply(200,s.planner.status(device))
            if path in ('/planning/settings','/planning/context','/planning/estimate') and self.command=='POST':
                if set(b)!={'payload'}:raise TodoError('排程请求格式无效')
                value=json.loads(b['payload'])
                if path.endswith('/settings'):result=s.planner.configure(device,value)
                elif path.endswith('/context'):
                    result=s.planner.context(device,value)
                    s.fitness.resume_pending(device)
                    result=s.planner.status(device)
                else:
                    if not isinstance(value,dict) or set(value)!={'id','minutes','reschedule'} or type(value['reschedule']) is not bool:raise TodoError('预计用时格式无效')
                    result=s.planner.estimate(device,value['id'],value['minutes'],value['reschedule'])
                return self.reply(200,result)
            if path=='/fitness/plan' and self.command=='GET':return self.reply(200,{'plan':s.fitness.plan(device)})
            if path=='/fitness/preview' and self.command=='POST':
                if b:raise ValueError('训练草案请求无效')
                return self.reply(200,{'plan':s.fitness.preview(device)})
            if path=='/fitness/commit' and self.command=='POST':
                if set(b)!={'id'}:raise ValueError('训练安排请求无效')
                return self.reply(200,s.fitness.commit(device,b['id']))
            if path=='/fitness/status' and self.command=='GET':return self.reply(200,s.fitness.status(device))
            if path=='/fitness/clear' and self.command=='POST':
                if b:raise ValueError('清除参数无效')
                return self.reply(200,s.fitness.clear(device))
            if path in ('/fitness/snapshot','/fitness/profile') and self.command=='POST':
                if set(b)!={'payload'}:raise ValueError('健康请求格式无效')
                payload=json.loads(b['payload'])
                return self.reply(200,s.fitness.snapshot(device,payload) if path.endswith('snapshot') else s.fitness.profile(device,payload))
            if path=='/location/status' and self.command=='GET':return self.reply(200,s.live.location_status(device))
            if path=='/location/clear' and self.command=='POST':
                if b:raise TodoError('天气地区清除参数无效')
                with s.db() as c:c.execute('DELETE FROM weather_regions WHERE owner=?',(device,))
                return self.reply(200,{'cleared':True})
            if path=='/location' and self.command=='POST':
                if set(b)!={'payload'}:raise TodoError('天气地区格式无效')
                return self.reply(200,s.live.location(device,json.loads(b['payload'])))
            if path=='/mac/status' and self.command=='GET':return self.reply(200,s.mac.status())
            if path=='/mac/actions' and self.command=='POST':
                if set(b)!={'payload'}:raise TodoError('Mac动作格式无效')
                action=json.loads(b['payload']);s.mac.validate(device,action)
                return self.reply(201,s.mac._submit(device,'Mac应用操作',action))
            if path=='/transfer' and self.command=='POST':return self.reply(201,s.mac.transfer(device,b))
            if path=='/documents/import' and self.command=='POST':return self.reply(201,s.mac.import_file(device,b))
            if re.fullmatch(r'/documents/[0-9a-f-]{36}/writeback',path) and self.command=='POST':
                if set(b)!={'version'}:raise TodoError('写回文档参数无效')
                return self.reply(201,s.mac.writeback(device,path.split('/')[2],int(b['version'])))
            if path=='/documents' and self.command=='POST':return self.reply(201,s.documents.upload(device,b))
            if re.fullmatch(r'/documents/[0-9a-f-]{36}(?:/download|/undo)?',path):
                ident=path.split('/')[2]
                if path.endswith('/download') and self.command=='GET':
                    q=urllib.parse.parse_qs(parsed.query)
                    if set(q)-{'version','offset'} or any(len(v)!=1 for v in q.values()):raise TodoError('文档下载参数无效')
                    return self.reply(200,s.documents.download(device,ident,int(q.get('version',['0'])[0]),int(q.get('offset',['0'])[0])))
                if path.endswith('/undo') and self.command=='POST':
                    if set(b)!={'version'}:raise TodoError('撤销文档参数无效')
                    return self.reply(200,s.documents.undo(device,ident,int(b['version'])))
                if self.command=='GET' and not path.endswith(('/download','/undo')):return self.reply(200,s.documents.info(device,ident))
            if path=='/canvas/connect' and self.command=='POST':
                if set(b)!={'token'}: raise TodoError('Canvas连接参数无效')
                return self.reply(200,s.canvas.connect(device,b['token']))
            if path in ('/canvas/sync','/canvas/disconnect') and self.command=='POST':
                if b: raise TodoError('Canvas操作参数无效')
                return self.reply(200,s.canvas.start_sync(device) if path=='/canvas/sync' else s.canvas.disconnect(device))
            if (path=='/todos' or re.fullmatch(r'/todos/[0-9a-f-]{36}/update',path)) and self.command=='POST':
                if set(b)!={'payload'} or not isinstance(b['payload'],str): raise TodoError('待办请求格式无效')
                value=json.loads(b['payload'])
                if path=='/todos': return self.reply(201,s.todos.create(device,value))
                result=s.todos.update(device,path.split('/')[2],value)
                s.planner.reconcile(device)
                return self.reply(200,result)
            if path=='/calendar/entries' and self.command=='GET':
                query=urllib.parse.parse_qs(parsed.query,keep_blank_values=True)
                if set(query)-{'cursor','limit'} or any(len(values)!=1 for values in query.values()): raise CalendarError('日历分页参数无效。')
                return self.reply(200,s.calendar.entries(device,query.get('cursor',[''])[0],query.get('limit',['100'])[0]))
            if path=='/roots' and self.command=='GET': return self.reply(200,s.roots())
            if path=='/devices/push' and self.command=='POST':
                s.register_push(device,b.get('token'),b.get('environment'))
                configured=provider.status()['push_configured']
                return self.reply(200,{'ok':True,'status':'registered','apns_configured':configured,'push_configured':configured})
            if path=='/tasks' and self.command=='GET': return self.reply(200,[__import__('task_outcomes').public_task(s,row) for row in s.tasks(device)])
            if path=='/assistant/speech/preferences':
                if self.command=='GET':return self.reply(200,s.speech.preferences(device))
                if self.command=='POST':return self.reply(200,s.speech.configure(device,b))
            if path=='/assistant/speech' and self.command=='POST':return self.reply(202,s.speech.request(device,b))
            speech_match=re.fullmatch(r'/assistant/speech/([0-9a-f]{32})(/audio)?',path)
            if speech_match and self.command=='GET':
                if not speech_match[2]:return self.reply(200,s.speech.status(device,speech_match[1]))
                data=s.speech.audio(device,speech_match[1]);self.send_response(200);self.send_header('Content-Type','audio/wav');self.send_header('Cache-Control','no-store');self.send_header('Content-Length',str(len(data)));self.end_headers();self.wfile.write(data);return
            if path=='/assistant/sessions' and self.command=='POST':
                if b:raise TodoError('新会话不需要参数')
                return self.reply(201,s.sessions.create(device))
            if path=='/assistant/sessions/current' and self.command=='GET':return self.reply(200,s.sessions.current(device))
            match=re.fullmatch(r'/assistant/sessions/([0-9a-f-]{36})(/receipts|/client-events)?',path)
            if match:
                if self.command=='GET' and not match[2]:return self.reply(200,s.sessions.state(device,match[1]))
                if self.command=='POST' and match[2]:
                    if set(b)!={'payload'}:raise TodoError('手机回执格式无效')
                    handler=s.sessions.client_event if match[2]=='/client-events' else s.sessions.receipt
                    return self.reply(200,handler(device,match[1],json.loads(b['payload'])))
            if path=='/assistant/latest-result' and self.command=='GET':
                with s.db() as c:row=c.execute("SELECT id,result,created FROM tasks WHERE owner=? AND state='completed' AND result<>'' ORDER BY created DESC LIMIT 1",(device,)).fetchone()
                return self.reply(200,dict(row) if row else {'id':'','result':'暂无已完成结果','created':0})
            if path=='/audit' and self.command=='GET':
                with s.db() as c: rows=[dict(r) for r in c.execute('SELECT a.* FROM audit a JOIN tasks t ON t.id=a.task WHERE t.owner=? ORDER BY a.id DESC LIMIT 500',(device,))]
                return self.reply(200,rows)
            if path=='/tasks' and self.command=='POST': return self.reply(201,{'id':s.submit(b,device)})
            if path=='/voice' and self.command=='POST': return self.reply(201,{'id':s.voice.submit(b,device,validate_calendar)})
            parts=path.strip('/').split('/')
            if len(parts)==4 and parts[:2]==['calendar','entries'] and self.command=='POST':
                action=parts[3]
                if action in ('resolve','cancel','ack'): return self.reply(200,getattr(s.calendar,action)(parts[2],device,b))
            if len(parts)==4 and parts[0]=='tasks' and parts[2:]==['reminder','resolve'] and self.command=='POST':
                return self.reply(200,s.resolve_reminder(parts[1],device,b))
            if len(parts)==2 and parts[0]=='tasks' and self.command=='GET':
                row=s.task(parts[1],device);return self.reply(200,__import__('task_outcomes').public_task(s,row))
            if len(parts)==3 and parts[0]=='tasks':
                s.task(parts[1],device)
                if parts[2]=='audit' and self.command=='GET': return self.reply(200,s.audits(parts[1]))
                if parts[2]=='local-reminder' and self.command=='POST':
                    s.mark_local_reminder(parts[1],device,b.get('scheduled')); return self.reply(200,{'ok':True})
                if parts[2] in ('approve','undo','cancel') and self.command=='POST':
                    s.act(parts[1],parts[2]); return self.reply(200,{'ok':True})
            self.reply(404,{'error':'接口不存在'})
        except FileAccessError as e: self.reply(e.status,{'error':e.message,'code':e.code})
        except CalendarError as e: self.reply(e.status,{'error':e.message})
        except TodoError as e: self.reply(e.status,{'error':str(e)})
        except VoiceError as e: self.reply(e.status,{'error':e.message,'code':e.code})
        except ConflictError as e: self.reply(409,{'error':str(e)})
        except (ValueError,KeyError,FileNotFoundError) as e: self.reply(400,{'error':str(e)})
        except Exception: self.reply(500,{'error':'内部错误，请查看本机审计'})

def main():
    p=argparse.ArgumentParser(); p.add_argument('--home',default='~/Library/Application Support/PersonalOS-OpenSource'); sub=p.add_subparsers(dest='command',required=True)
    sub.add_parser('serve').add_argument('--port',type=int,default=8765)
    sub.add_parser('pair'); sub.add_parser('revoke'); sub.add_parser('grant').add_argument('path'); sub.add_parser('ungrant').add_argument('path')
    sub.add_parser('files-enable'); sub.add_parser('files-disable')
    sub.add_parser('voice-enable'); sub.add_parser('voice-disable')
    a=p.parse_args(); s=Store(a.home)
    if a.command in ('voice-enable','voice-disable'):
        try: s.voice.enable(a.command=='voice-enable')
        except VoiceError as error: p.error(error.message)
        print('本地语音识别已启用。' if a.command=='voice-enable' else '本地语音识别已停用。')
    elif a.command in ('files-enable','files-disable'):
        enabled=a.command=='files-enable'; target=s.home/'files-access.json'
        temporary=s.home/('.files-access-'+secrets.token_hex(8)+'.tmp')
        descriptor=os.open(str(temporary),os.O_WRONLY|os.O_CREAT|os.O_EXCL,0o600)
        with os.fdopen(descriptor,'w') as stream: json.dump({'enabled':enabled},stream)
        os.replace(temporary,target)
        s.audit('','file_access_enabled' if enabled else 'file_access_disabled',{'device':'mini','read_only':True,'scope':'OS-readable files excluding protected credentials; organize grants unchanged'})
        print('已启用 Mac mini 广域只读文件访问；整理仍需单独目录授权。' if enabled else '已停用 Mac mini 广域只读文件访问。')
    elif a.command=='pair':
        code=secrets.token_urlsafe(18)
        with s.db() as c: c.execute('DELETE FROM pairing'); c.execute('INSERT INTO pairing VALUES(?,?)',(hashlib.sha256(code.encode()).hexdigest(),time.time()+300))
        print('配对码（5 分钟有效，仅在可信 HTTPS 连接中输入）：'+code)
    elif a.command=='ungrant':
        root=pathlib.Path(a.path).expanduser().resolve()
        with s.db() as c: c.execute('DELETE FROM roots WHERE path=?',(str(root),))
        s.audit('','ungrant',str(root)); print('目录授权已移除')
    elif a.command=='grant':
        root=pathlib.Path(a.path).expanduser().resolve(strict=True)
        if not root.is_dir(): p.error('必须是目录')
        with s.db() as c: c.execute('INSERT OR IGNORE INTO roots VALUES(?)',(str(root),))
        s.audit('','grant',str(root)); print('已授权精确目录：'+str(root))
    elif a.command=='revoke':
        with s.db() as c:
            c.execute('DELETE FROM devices'); c.execute('DELETE FROM pairing'); c.execute('DELETE FROM push_devices')
            c.execute("UPDATE notification_outbox SET state='cancelled' WHERE state IN ('pending','retry','sending')")
        print('全部设备令牌已撤销')
    else:
        server=ThreadingHTTPServer(('127.0.0.1',a.port),Handler); server.store=s
        # Bind first, so an accidental second instance cannot reset the live worker's tasks.
        s.voice.recover()
        s.speech.recover()
        with s.db() as c:
            interrupted=[r['id'] for r in c.execute("SELECT id FROM tasks WHERE state='running'")]
            c.execute("UPDATE tasks SET state='needs_review',result='服务重启中断执行；请查看审计后重新提交。' WHERE state='running'")
            for ident in interrupted: s._notify(c,ident,'needs_review')
        server.push_provider=APNsProvider(s.home/'push.json')
        worker=Worker(s); dispatcher=PushDispatcher(s,server.push_provider,worker.stop)
        threading.Thread(target=worker.run,daemon=True).start()
        threading.Thread(target=dispatcher.run,daemon=True).start()
        threading.Thread(target=s.canvas.run,args=(worker.stop,),daemon=True).start()
        threading.Thread(target=s.speech.maintain,args=(worker.stop,),daemon=True).start()
        threading.Thread(target=s.planner.run,args=(worker.stop,),daemon=True).start()
        print('Personal OS: http://127.0.0.1:'+str(a.port),flush=True)
        try: server.serve_forever()
        finally: worker.stop.set(); server.server_close()
if __name__=='__main__': main()
