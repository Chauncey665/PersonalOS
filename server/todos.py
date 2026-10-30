"""Owned, versioned unscheduled tasks. A deadline never creates a calendar slot."""
import datetime as dt
import hashlib
import json
import re
import time
import uuid
from calendar_journal import DATE_RE, _date, validate_zone
from deadline_intent import deadline_request, unscheduled_reminder, normalized_time, deadline_fact


class TodoError(ValueError):
    def __init__(self, message, status=400):
        self.status = status
        super().__init__(message)


def backlog_request(text):
    return deadline_request(text) or unscheduled_reminder(text) or bool(re.search(r'待办|ddl|deadline|截止|拆分|拆成|分解|子任务|小任务|大任务|项目任务|要做的事|'
        r'(?:任务|项目).{0,30}(?:标记|勾|增加|新增|记录|做完)|(?:标记完成|完成|勾|做完).{0,30}(?:任务|项目)|'
        r'(?:新增|添加|记录|记下|完成|勾).{0,30}任务|'
        r'不确定.{0,10}时间|没(?:有)?(?:定|安排|确定).{0,10}时间', text, re.I))


def deadline(value):
    if value is None or value == '':
        return None
    if not isinstance(value, str) or len(value) > 80:
        raise TodoError('截止日期格式无效')
    try:
        if re.fullmatch(r'\d{4}-\d{2}-\d{2}', value):
            dt.date.fromisoformat(value)
        else:
            parsed = dt.datetime.fromisoformat(value.replace('Z', '+00:00'))
            if parsed.tzinfo is None:
                raise ValueError()
        return value
    except (ValueError, OverflowError):
        raise TodoError('截止时间请使用日期或带时区的完整时间') from None


class TodoStore:
    def __init__(self, store):
        self.store = store

    @staticmethod
    def initialize(c):
        c.execute('CREATE TABLE IF NOT EXISTS todo_calendar_receipts(owner TEXT NOT NULL,todo_id TEXT NOT NULL,revision INTEGER NOT NULL,fingerprint TEXT NOT NULL,status TEXT NOT NULL,updated REAL NOT NULL,PRIMARY KEY(owner,todo_id))')
        c.executescript('''CREATE TABLE IF NOT EXISTS todos(
            seq INTEGER PRIMARY KEY AUTOINCREMENT,id TEXT UNIQUE NOT NULL,owner TEXT NOT NULL,
            parent_id TEXT,kind TEXT NOT NULL,title TEXT NOT NULL,notes TEXT NOT NULL,
            deadline TEXT,completed INTEGER NOT NULL DEFAULT 0,revision INTEGER NOT NULL DEFAULT 1,
            created REAL NOT NULL,updated REAL NOT NULL,source_task TEXT NOT NULL);
            CREATE INDEX IF NOT EXISTS todos_owner_seq ON todos(owner,seq);
            CREATE INDEX IF NOT EXISTS todos_parent ON todos(owner,parent_id);
            CREATE TABLE IF NOT EXISTS todo_batches(owner TEXT NOT NULL,request_id TEXT NOT NULL,
                body_hash TEXT NOT NULL,ids TEXT NOT NULL,PRIMARY KEY(owner,request_id));
            CREATE TABLE IF NOT EXISTS todo_sources(owner TEXT NOT NULL,source TEXT NOT NULL,
                external_id TEXT NOT NULL,todo_id TEXT NOT NULL,remote_json TEXT NOT NULL,last_seen REAL NOT NULL,
                PRIMARY KEY(owner,source,external_id));''')

    @staticmethod
    def public(row, progress=None, sources=None):
        result = {k: row[k] for k in ('id', 'parent_id', 'kind', 'title', 'notes', 'deadline',
                                      'revision', 'created', 'updated')}
        result['completed'] = bool(row['completed'])
        source = (sources or {}).get(row['id'])
        if source:
            data = json.loads(source['remote_json'])
            result.update(source='canvas', source_url=data['url'], source_synced=source['last_seen'])
        if row['kind'] == 'project':
            done, total = (progress or {}).get(row['id'], (0, 0))
            result.update(completed_count=done, total_count=total)
            if total:
                result['completed'] = done == total
        return result

    @staticmethod
    def _progress(c, owner):
        return {r['parent_id']: (r['done'], r['total']) for r in c.execute(
            'SELECT parent_id,sum(completed) AS done,count(*) AS total FROM todos WHERE owner=? AND parent_id IS NOT NULL GROUP BY parent_id', (owner,))}

    @staticmethod
    def _sources(c, owner):
        return {r['todo_id']: dict(r) for r in c.execute('SELECT * FROM todo_sources WHERE owner=?', (owner,))}

    def list(self, owner, cursor='0'):
        if not owner:
            raise TodoError('需要设备认证', 401)
        if not isinstance(cursor, str) or not re.fullmatch(r'\d{1,16}', cursor):
            raise TodoError('待办分页参数无效')
        with self.store.db() as c:
            rows = c.execute('SELECT * FROM todos WHERE owner=? AND seq>? ORDER BY seq LIMIT 201',
                             (owner, int(cursor))).fetchall()
            progress = self._progress(c, owner)
            sources = self._sources(c, owner)
            return {'items': [self.public(r, progress, sources) for r in rows[:200]],
                    'next_cursor': str(rows[199]['seq']) if len(rows) > 200 else None}

    @staticmethod
    def _clean(item):
        if not isinstance(item, dict) or set(item) - {'title', 'notes', 'deadline', 'kind', 'children'}:
            raise TodoError('待办字段无效')
        title, notes = item.get('title'), item.get('notes', '')
        if not isinstance(title, str) or not 1 <= len(title.strip()) <= 160:
            raise TodoError('任务标题须为1到160字')
        if not isinstance(notes, str) or len(notes) > 2000:
            raise TodoError('任务备注过长')
        children = item.get('children', [])
        if not isinstance(children, list) or len(children) > 30:
            raise TodoError('一个项目最多30项子任务')
        kind = item.get('kind', 'project' if children else 'task')
        if kind not in ('task', 'project') or (children and kind != 'project'):
            raise TodoError('子任务须放在项目中')
        clean = {'title': title.strip(), 'notes': notes.strip(), 'deadline': deadline(item.get('deadline')), 'kind': kind}
        clean['children'] = []
        for child in children:
            value = TodoStore._clean(child)
            if value['children'] or value['kind'] != 'task':
                raise TodoError('当前支持项目和一层子任务')
            clean['children'].append(value)
        return clean

    def create(self, owner, body, source_task=''):
        if not owner:
            raise TodoError('需要设备认证', 401)
        if not isinstance(body, dict) or set(body) - {'items', 'request_id', 'parent_id'}:
            raise TodoError('待办请求无效')
        request_id, items = body.get('request_id'), body.get('items')
        if not isinstance(request_id, str) or not re.fullmatch(r'[A-Za-z0-9:_-]{1,128}', request_id):
            raise TodoError('待办请求编号无效')
        if not isinstance(items, list) or not 1 <= len(items) <= 20:
            raise TodoError('一次可记录1到20项任务')
        cleaned = [self._clean(item) for item in items]
        if sum(1 + len(item['children']) for item in cleaned) > 100:
            raise TodoError('一次最多100项任务和子任务')
        parent = body.get('parent_id')
        if parent is not None and (not isinstance(parent, str) or any(i['kind'] != 'task' for i in cleaned)):
            raise TodoError('只可向项目添加普通子任务')
        canonical = json.dumps({'items': cleaned, 'parent_id': parent}, ensure_ascii=False, sort_keys=True)
        digest = hashlib.sha256(canonical.encode()).hexdigest()
        with self.store.db() as c:
            c.execute('BEGIN IMMEDIATE')
            if not c.execute('SELECT 1 FROM devices WHERE token=?', (owner,)).fetchone():
                raise TodoError('设备认证已失效', 401)
            prior = c.execute('SELECT body_hash,ids FROM todo_batches WHERE owner=? AND request_id=?', (owner, request_id)).fetchone()
            if prior:
                if prior['body_hash'] != digest:
                    raise TodoError('本次请求已经记录，不能重试为另一组任务；请编辑已保存事项', 409)
                ids = json.loads(prior['ids'])
            else:
                if parent:
                    target = c.execute("SELECT * FROM todos WHERE id=? AND owner=? AND parent_id IS NULL", (parent, owner)).fetchone()
                    if not target:
                        raise TodoError('项目不存在', 404)
                ids = []
                now = time.time()
                def insert(item, path, parent_id):
                    ident = str(uuid.uuid5(uuid.NAMESPACE_URL, owner + ':' + request_id + ':' + path))
                    c.execute('INSERT INTO todos(id,owner,parent_id,kind,title,notes,deadline,completed,revision,created,updated,source_task) VALUES(?,?,?,?,?,?,?,0,1,?,?,?)',
                              (ident, owner, parent_id, item['kind'], item['title'], item['notes'], item['deadline'], now, now, source_task))
                    ids.append(ident)
                    for index, child in enumerate(item['children']):
                        insert(child, path + '/' + str(index), ident)
                for index, item in enumerate(cleaned):
                    insert(item, str(index), parent)
                c.execute('INSERT INTO todo_batches VALUES(?,?,?,?)', (owner, request_id, digest, json.dumps(ids)))
                self.store._audit(c, source_task, 'todos_created', {'count': len(ids), 'project_count': sum(i['kind'] == 'project' for i in cleaned)})
                if parent:
                    c.execute("UPDATE todos SET kind='project',revision=revision+1,updated=? WHERE id=? AND owner=?", (now, parent, owner))
            progress = self._progress(c, owner)
            rows = [c.execute('SELECT * FROM todos WHERE id=? AND owner=?', (i, owner)).fetchone() for i in ids]
            return {'items': [self.public(r, progress) for r in rows], 'created_ids': ids,
                    'calendar_slots_created': 0}

    @staticmethod
    def calendar_fingerprint(item):
        return hashlib.sha256('\x1f'.join([item['id'],item['title'],item['notes'],item['deadline'] or '', '1' if item['completed'] else '0']).encode()).hexdigest()

    def calendar_ack(self,owner,ident,body):
        if not isinstance(body,dict) or set(body)!={'revision','fingerprint','status'} or type(body['revision']) is not int or body['status'] not in ('written','removed','not_required'):raise TodoError('截止日历回执格式无效')
        with self.store.db() as c:
            c.execute('BEGIN IMMEDIATE')
            row=c.execute('SELECT * FROM todos WHERE id=? AND owner=?',(ident,owner)).fetchone()
            if not row:raise TodoError('待办不存在',404)
            item=self.public(row,self._progress(c,owner))
            if row['revision']!=body['revision'] or self.calendar_fingerprint(item)!=body['fingerprint']:raise TodoError('日历回执不是当前待办版本',409)
            if bool(item['deadline'])!=(body['status']=='written'):raise TodoError('日历回执与截止记录不一致',409)
            c.execute('INSERT INTO todo_calendar_receipts VALUES(?,?,?,?,?,?) ON CONFLICT(owner,todo_id) DO UPDATE SET revision=excluded.revision,fingerprint=excluded.fingerprint,status=excluded.status,updated=excluded.updated',(owner,ident,row['revision'],body['fingerprint'],body['status'],time.time()))
            self.store._audit(c,'','todo_calendar_ack',{'id':ident,'revision':row['revision'],'status':body['status']})
        return {'accepted':True}

    def update(self, owner, ident, body):
        if not isinstance(body, dict) or set(body) - {'revision', 'title', 'notes', 'deadline', 'completed'} or len(body) < 2:
            raise TodoError('待办修改字段无效')
        if type(body.get('revision')) is not int:
            raise TodoError('请携带当前任务版本')
        with self.store.db() as c:
            c.execute('BEGIN IMMEDIATE')
            row = c.execute('SELECT * FROM todos WHERE id=? AND owner=?', (ident, owner)).fetchone()
            if not owner or not row:
                raise TodoError('任务不存在', 404)
            if row['revision'] != body['revision']:
                raise TodoError('任务已更新，请刷新后再修改', 409)
            changes = {}
            for key in ('title', 'notes'):
                if key in body:
                    value = body[key]
                    maximum = 160 if key == 'title' else 2000
                    if not isinstance(value, str) or len(value.strip()) > maximum or (key == 'title' and not value.strip()):
                        raise TodoError('任务标题或备注无效')
                    changes[key] = value.strip()
            if 'deadline' in body:
                changes['deadline'] = deadline(body['deadline'])
            if 'completed' in body:
                if type(body['completed']) is not bool:
                    raise TodoError('完成状态无效')
                if row['kind'] == 'project' and c.execute('SELECT 1 FROM todos WHERE parent_id=? AND owner=?', (ident, owner)).fetchone():
                    raise TodoError('项目进度根据子任务计算，请逐项勾选')
                changes['completed'] = int(body['completed'])
            changes['updated'] = time.time()
            c.execute('UPDATE todos SET ' + ','.join(k + '=?' for k in changes) + ',revision=revision+1 WHERE id=? AND owner=?',
                      list(changes.values()) + [ident, owner])
            self.store._audit(c, '', 'todo_updated', {'id': ident, 'fields': sorted(k for k in changes if k != 'updated')})
            updated = c.execute('SELECT * FROM todos WHERE id=? AND owner=?', (ident, owner)).fetchone()
            return self.public(updated, self._progress(c, owner), self._sources(c, owner))

    def import_canvas(self, owner, items, snapshot_at=None, preserve_fields=()):
        """Stable source IDs and three-way field merge; absence never means done."""
        if not owner or not isinstance(items, list) or len(items) > 500:
            raise TodoError('Canvas待办快照无效')
        now = snapshot_at or time.time()
        inserted = updated = conflicts = 0
        with self.store.db() as c:
            c.execute('BEGIN IMMEDIATE')
            if not c.execute('SELECT 1 FROM devices WHERE token=?', (owner,)).fetchone():
                raise TodoError('设备认证已失效', 401)
            for item in items:
                required = {'external_id', 'title', 'course', 'deadline', 'completed', 'url'}
                if not isinstance(item, dict) or required - set(item) or set(item) - required - {'description'}:
                    raise TodoError('Canvas快照字段无效')
                if not re.fullmatch(r'\d+:\d+', str(item['external_id'])) or type(item['completed']) is not bool:
                    raise TodoError('Canvas来源编号或状态无效')
                url = item['url']
                if not isinstance(url, str) or not re.fullmatch(r'https://canvas\.example\.edu/courses/\d+/assignments/\d+', url):
                    raise TodoError('Canvas来源链接无效')
                note = str(item['course'])[:200] + '\n' + str(item.get('description', ''))[:1600] + '\n' + url
                clean = self._clean({'title': item['title'], 'notes': note.strip(), 'deadline': item['deadline']})
                fields = {k: clean[k] for k in ('title', 'notes', 'deadline')}
                fields['completed'] = int(item['completed'])
                remote = {**fields, 'url': url}
                prior = c.execute("SELECT * FROM todo_sources WHERE owner=? AND source='canvas' AND external_id=?",
                                  (owner, item['external_id'])).fetchone()
                if not prior:
                    ident = str(uuid.uuid5(uuid.NAMESPACE_URL, owner + ':canvas:' + item['external_id']))
                    c.execute("INSERT INTO todos(id,owner,parent_id,kind,title,notes,deadline,completed,revision,created,updated,source_task) VALUES(?,?,NULL,'task',?,?,?,?,1,?,?,'canvas')",
                              (ident, owner, fields['title'], fields['notes'], fields['deadline'], fields['completed'], now, now))
                    c.execute("INSERT INTO todo_sources VALUES(?,'canvas',?,?,?,?)",
                              (owner, item['external_id'], ident, json.dumps(remote, ensure_ascii=False), now))
                    inserted += 1
                else:
                    row = c.execute('SELECT * FROM todos WHERE id=? AND owner=?', (prior['todo_id'], owner)).fetchone()
                    if not row:
                        raise TodoError('Canvas来源记录不完整，请检查本机记录', 409)
                    previous = json.loads(prior['remote_json'])
                    for key in preserve_fields:
                        if key in fields:
                            fields[key] = previous.get(key, row[key])
                            remote[key] = fields[key]
                    changes = {}
                    for key, value in fields.items():
                        if row[key] == previous.get(key):
                            if row[key] != value:
                                changes[key] = value
                        elif value != previous.get(key):
                            conflicts += 1
                    if changes:
                        c.execute('UPDATE todos SET ' + ','.join(k + '=?' for k in changes) + ',revision=revision+1,updated=? WHERE id=? AND owner=?',
                                  list(changes.values()) + [now, row['id'], owner])
                        updated += 1
                    c.execute("UPDATE todo_sources SET remote_json=?,last_seen=? WHERE owner=? AND source='canvas' AND external_id=?",
                              (json.dumps(remote, ensure_ascii=False), now, owner, item['external_id']))
            self.store._audit(c, '', 'canvas_todos_imported', {'received': len(items), 'inserted': inserted,
                                                            'updated': updated, 'manual_fields_preserved': conflicts})
        return {'received': len(items), 'inserted': inserted, 'updated': updated,
                'manual_fields_preserved': conflicts, 'synced_at': now}

    @staticmethod
    def model_items(items, text, created, zone_name):
        """LLM deadlines require an exact user quote and deterministic date match."""
        if not isinstance(items, list):
            raise TodoError('任务列表无效')
        zone = validate_zone(zone_name)
        today = dt.datetime.fromtimestamp(created, zone).date()
        def convert(item):
            if not isinstance(item, dict) or set(item) - {'title', 'notes', 'kind', 'deadline', 'deadline_quote', 'children'}:
                raise TodoError('模型待办字段无效')
            output = {k: v for k, v in item.items() if k not in ('deadline_quote', 'children')}
            due = deadline(item.get('deadline'))
            if due:
                quote = item.get('deadline_quote', '')
                if not isinstance(quote, str) or not quote or quote not in text:
                    raise TodoError('截止日期必须引用本轮用户明确说出的日期；不确定时留空')
                dates = {_date(match.group(), today) for match in DATE_RE.finditer(normalized_time(quote))}
                target = dt.date.fromisoformat(due) if len(due) == 10 else dt.datetime.fromisoformat(due.replace('Z', '+00:00')).astimezone(zone).date()
                if dates != {target}:
                    raise TodoError('模型截止日期与用户原话不一致，未保存')
                if len(due) != 10:
                    # Exact time extraction already has strict validation.
                    from reminder_time import parse_reminder_time
                    if abs(parse_reminder_time(normalized_time(quote).replace("之前", "，").replace("以前", "，"), created, zone_name) - dt.datetime.fromisoformat(due.replace('Z', '+00:00')).timestamp()) > 1:
                        raise TodoError('截止时刻与用户原话不一致')
            output['deadline'] = due
            if item.get('children'):
                output['children'] = [convert(child) for child in item['children']]
            return output
        return [convert(item) for item in items]
