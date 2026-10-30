"""Locally configured Canvas read-only connector. Tokens stay in a private local config."""
import json
import datetime as dt
import os
import pathlib
import re
import stat
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from html.parser import HTMLParser
from todos import TodoError

BASE = 'https://canvas.example.edu'


def plain_description(value):
    class Text(HTMLParser):
        def __init__(self):
            super().__init__(convert_charrefs=True)
            self.parts, self.hidden = [], 0
        def handle_starttag(self, tag, attrs):
            if tag in ('script', 'style'): self.hidden += 1
            if tag in ('p', 'div', 'br', 'li'): self.parts.append('\n')
        def handle_endtag(self, tag):
            if tag in ('script', 'style'): self.hidden = max(0, self.hidden - 1)
        def handle_data(self, data):
            if not self.hidden: self.parts.append(data)
    parser = Text()
    parser.feed(str(value or '')[:50000])
    return '\n'.join(' '.join(line.split()) for line in ''.join(parser.parts).splitlines() if line.strip())[:1600]


def feed_url(value, base_url=BASE):
    """Only the school's official user calendar export; never an arbitrary URL."""
    if not isinstance(value, str):
        raise TodoError('请粘贴Canvas Calendar Feed链接')
    if value.startswith('webcal://'):
        value = 'https://' + value[9:]
    if not re.fullmatch(re.escape(base_url)+r'/feeds/calendars/user_[A-Za-z0-9_-]{20,150}\.ics', value):
        raise TodoError('只支持学校Canvas的个人Calendar Feed链接')
    return value


def feed_items(raw, known=(), now=None, base_url=BASE):
    """Bounded RFC5545 subset for dated Canvas assignments, not personal todos.

    Completion is unknown in an iCal feed. Importer must preserve existing state.
    Old, previously unseen assignments aren't suddenly added as unfinished tasks.
    """
    from calendar_journal import validate_zone
    if isinstance(raw, bytes):
        try: raw = raw.decode('utf-8-sig')
        except UnicodeError: raise TodoError('Canvas日历编码无效') from None
    if not isinstance(raw, str) or len(raw) > 4 * 1024 * 1024:
        raise TodoError('Canvas日历过大或格式无效')
    lines = re.sub(r'\r?\n[ \t]', '', raw).splitlines()
    if not lines or lines[0] != 'BEGIN:VCALENDAR' or lines[-1] != 'END:VCALENDAR':
        raise TodoError('Canvas没有返回完整日历，已有待办保留')
    blocks, current, depth = [], None, 0
    for line in lines[1:-1]:
        if line.startswith('BEGIN:'):
            if line == 'BEGIN:VEVENT':
                if current is not None: raise TodoError('Canvas日历事件嵌套无效')
                current, depth = {}, 0
            elif current is not None: depth += 1
        elif line.startswith('END:'):
            if line == 'END:VEVENT':
                if current is None or depth: raise TodoError('Canvas日历事件不完整')
                blocks.append(current); current = None
                if len(blocks) > 1000: raise TodoError('Canvas日历超过1000项，本轮未导入')
            elif current is not None: depth -= 1
        elif current is not None and depth == 0 and ':' in line:
            head, value = line.split(':', 1)
            name = head.split(';', 1)[0]
            if name in ('UID', 'URL', 'SUMMARY', 'DESCRIPTION', 'DTSTART', 'STATUS', 'RRULE', 'RECURRENCE-ID'):
                if name in current: raise TodoError('Canvas日历事件字段重复')
                current[name] = (head, value)
    if current is not None: raise TodoError('Canvas日历事件不完整')
    def text(value):
        return re.sub(r'\\([nN,;\\])', lambda m: '\n' if m[1] in 'nN' else m[1], value)
    result = {}
    clock = time.time() if now is None else now
    for event in blocks:
        uid = event.get('UID', ('', ''))[1]
        url = text(event.get('URL', ('', ''))[1])
        match = re.fullmatch(re.escape(base_url)+r'/courses/(\d+)/assignments/(\d+)', url)
        if not match:
            # Canvas IcalEvent.to_ics uses a calendar deep link rather than the
            # assignment page. Normalize only this exact official URL shape.
            match = re.fullmatch(re.escape(base_url)+r'/calendar\?include_contexts=course_(\d+)&month=\d{1,2}&year=\d{4}#assignment_(\d+)', url)
        if not match:
            if re.fullmatch(r'event-assignment-(?:override-)?\d+', uid):
                raise TodoError('Canvas作业订阅链接格式暂不支持，已有待办保留')
            continue
        if not re.fullmatch(r'(?:event-assignment-' + match[2] + r'|event-assignment-override-\d+)(?:@[^\s]+)?', uid):
            continue  # Do not turn classes, appointments, quizzes or personal events into deadlines.
        url = base_url + '/courses/' + match[1] + '/assignments/' + match[2]
        if 'RRULE' in event or 'RECURRENCE-ID' in event:
            raise TodoError('Canvas作业出现重复规则，未猜测截止日期')
        if event.get('STATUS', ('', ''))[1] == 'CANCELLED': continue
        key = match[1] + ':' + match[2]
        head, value = event.get('DTSTART', ('', ''))
        try:
            if ';VALUE=DATE' in head and re.fullmatch(r'\d{8}', value):
                due = dt.datetime.strptime(value, '%Y%m%d').date().isoformat()
                future = due >= dt.datetime.fromtimestamp(clock, validate_zone('Asia/Hong_Kong')).date().isoformat()
            elif re.fullmatch(r'\d{8}T\d{6}Z', value):
                date = dt.datetime.strptime(value, '%Y%m%dT%H%M%SZ').replace(tzinfo=dt.timezone.utc)
                due, future = date.isoformat().replace('+00:00', 'Z'), date.timestamp() >= clock
            elif re.fullmatch(r'\d{8}T\d{6}', value) and ';TZID=' in head:
                zone = head.split(';TZID=', 1)[1].strip('"')
                if zone not in ('Asia/Hong_Kong', 'Asia/Shanghai', 'UTC'): raise ValueError()
                date = dt.datetime.strptime(value, '%Y%m%dT%H%M%S').replace(tzinfo=validate_zone(zone))
                due, future = date.isoformat(), date.timestamp() >= clock
            else: raise ValueError()
        except (ValueError, OverflowError):
            raise TodoError('Canvas作业截止时间无法可靠解析，已有待办保留') from None
        if not future and key not in known: continue
        title = text(event.get('SUMMARY', ('', ''))[1]).strip()
        if not title: raise TodoError('Canvas作业标题为空')
        course = 'Canvas'
        suffix = re.fullmatch(r'(.*?)\s*\[([^\[\]]+)\]', title)
        if suffix: title, course = suffix[1].strip(), suffix[2]
        item = {'external_id': key, 'title': title[:160], 'course': course[:200],
                'description': plain_description(text(event.get('DESCRIPTION', ('', ''))[1])),
                'deadline': due, 'completed': False, 'url': url}
        if key in result and result[key] != item: raise TodoError('Canvas同一作业有多个截止时间，请在学校页面核对')
        result[key] = item
    if len(result) > 500: raise TodoError('Canvas作业超过500项，本轮未导入')
    return list(result.values())


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


class CanvasSync:
    def __init__(self, store, transport=None):
        self.store = store
        from deployment_config import canvas_base
        self.base=canvas_base(store.home)
        self.path = store.home / 'canvas-source.json'
        self.transport = transport
        self.lock = threading.Lock()
        self.last_success = 0
        self.last_error = ''
        self.last_counts = {}
        self.next_attempt = 0

    def config(self):
        try:
            if self.path.is_symlink() or not self.path.is_file() or self.path.stat().st_size > 8192 or stat.S_IMODE(self.path.stat().st_mode) & 0o077:
                return None
            value = json.loads(self.path.read_text())
            if set(value) == {'owner', 'feed_url'}:
                feed_url(value['feed_url'],self.base)
            elif set(value) != {'owner', 'token', 'user_id'} or not isinstance(value['token'], str) or not 10 <= len(value['token']) <= 2048:
                return None
            return value
        except (OSError, ValueError, TypeError):
            return None

    def get(self, path, token):
        if not re.fullmatch(r'/api/v1/(?:users/self/(?:profile|todo)(?:\?per_page=100&page=\d+)?|courses/\d+/assignments/\d+/submissions/self)', path):
            raise TodoError('Canvas接口不在只读允许范围')
        request = urllib.request.Request(self.base + path, headers={'Authorization': 'Bearer ' + token, 'Accept': 'application/json'})
        try:
            open_request = self.transport or urllib.request.build_opener(NoRedirect()).open
            with open_request(request, timeout=15) as response:
                raw = response.read(4 * 1024 * 1024 + 1)
            if len(raw) > 4 * 1024 * 1024:
                raise TodoError('Canvas响应过大，请稍后重试')
            return json.loads(raw)
        except urllib.error.HTTPError as error:
            if error.code in (401, 403):
                raise TodoError('Canvas授权无效或已过期，请重新连接', 401) from None
            raise TodoError('Canvas暂时无法读取，请稍后重试', 502) from None
        except (OSError, ValueError):
            raise TodoError('Canvas返回异常或连接超时，保留已有待办', 502) from None

    def fetch_feed(self, url):
        request = urllib.request.Request(feed_url(url,self.base), headers={'Accept': 'text/calendar'})
        try:
            open_request = self.transport or urllib.request.build_opener(NoRedirect()).open
            with open_request(request, timeout=15) as response:
                raw = response.read(4 * 1024 * 1024 + 1)
            if len(raw) > 4 * 1024 * 1024: raise TodoError('Canvas日历过大，已有待办保留')
            return raw
        except urllib.error.HTTPError as error:
            if error.code in (401, 403, 404): raise TodoError('Canvas订阅链接无效、已失效或被学校限制', 401) from None
            raise TodoError('Canvas日历暂时无法读取，已有待办保留', 502) from None
        except OSError:
            raise TodoError('Canvas日历连接超时，已有待办保留', 502) from None

    def connect(self, owner, token):
        if not isinstance(token, str) or not 10 <= len(token) <= 2048 or any(ord(c) <= 32 for c in token):
            raise TodoError('请在手机粘贴Canvas个人访问令牌')
        existing = self.config()
        if existing and existing['owner'] != owner:
            raise TodoError('Canvas已由另一配对设备连接，请先在原设备断开', 409)
        if token.startswith(('https://', 'webcal://', 'http://')):
            url = feed_url(token,self.base)
            feed_items(self.fetch_feed(url),base_url=self.base)  # Validate before saving any persistent source.
            data = {'owner': owner, 'feed_url': url}
        else:
            profile = self.get('/api/v1/users/self/profile', token)
            if not isinstance(profile, dict) or type(profile.get('id')) is not int:
                raise TodoError('Canvas账号校验失败')
            data = {'owner': owner, 'token': token, 'user_id': profile['id']}
        temp = self.path.with_name('.canvas-source-' + os.urandom(8).hex())
        fd = os.open(str(temp), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        try:
            with os.fdopen(fd, 'w') as output:
                json.dump(data, output)
            temp.replace(self.path)
        finally:
            if temp.exists():
                temp.unlink()
        self.last_success = 0
        self.last_error = ''
        self.next_attempt = 0
        self.store.audit('', 'canvas_connected', {'read_only': True, 'mode': 'calendar_feed' if 'feed_url' in data else 'api'})
        return self.status(owner)

    def status(self, owner):
        config = self.config()
        mine = bool(config and config['owner'] == owner)
        with self.store.db() as c:
            snapshot = c.execute("SELECT max(last_seen) FROM todo_sources WHERE owner=? AND source='canvas'", (owner,)).fetchone()[0]
        return {'configured': mine, 'host': self.base, 'read_only': True,
                'mode': ('calendar_feed' if 'feed_url' in config else 'api') if mine else None,
                'submission_status_available': mine and 'feed_url' not in config,
                'syncing': self.lock.locked() if mine else False,
                'last_sync': self.last_success if mine and self.last_success else snapshot,
                'error': self.last_error if mine else '', 'counts': self.last_counts if mine else {}}

    def start_sync(self, owner):
        config = self.config()
        if not config or config['owner'] != owner:
            raise TodoError('请先连接Canvas')
        def run():
            try:
                self.sync(owner)
            except Exception:
                pass
        if not self.lock.locked():
            threading.Thread(target=run, daemon=True).start()
        return {**self.status(owner), 'syncing': True}

    def disconnect(self, owner):
        config = self.config()
        if config and config['owner'] != owner:
            raise TodoError('未找到属于本设备的Canvas连接', 404)
        if self.path.exists():
            self.path.unlink()
        self.last_error = ''
        self.store.audit('', 'canvas_disconnected', '')
        return self.status(owner)

    def sync(self, owner):
        config = self.config()
        if not config or config['owner'] != owner:
            raise TodoError('请先连接Canvas；已有导入数据会保留')
        if not self.lock.acquire(blocking=False):
            raise TodoError('Canvas正在同步，请稍后刷新', 409)
        try:
            with self.store.db() as c:
                if not c.execute('SELECT 1 FROM devices WHERE token=?', (owner,)).fetchone():
                    raise TodoError('配对已失效，Canvas同步已停止', 401)
                known = [dict(r) for r in c.execute("SELECT s.* FROM todo_sources s JOIN todos t ON s.todo_id=t.id AND s.owner=t.owner WHERE s.owner=? AND s.source='canvas' AND t.completed=0 LIMIT 100", (owner,))]
            if 'feed_url' in config:
                known_ids = [r['external_id'] for r in known]
                items = feed_items(self.fetch_feed(config['feed_url']), known_ids,base_url=self.base)
                if self.config() != config: raise TodoError('Canvas连接已改变，本轮结果未保存', 409)
                result = self.store.todos.import_canvas(owner, items, preserve_fields=('completed', 'notes'))
                self.last_success, self.last_counts, self.last_error = time.time(), result, ''
                self.next_attempt = time.time() + 900
                return result
            raw = []
            for page in range(1, 6):
                rows = self.get('/api/v1/users/self/todo?per_page=100&page=' + str(page), config['token'])
                if not isinstance(rows, list):
                    raise TodoError('Canvas待办格式无效')
                raw.extend(rows)
                if len(rows) < 100:
                    break
            else:
                raise TodoError('Canvas待办超过500项，本轮没有用截断结果覆盖原数据')
            items = {}
            for row in raw:
                assignment = row.get('assignment') or {}
                course, ident = row.get('course_id'), assignment.get('id')
                if type(course) is not int or type(ident) is not int or row.get('type') not in ('submitting', 'participating'):
                    continue
                key = str(course) + ':' + str(ident)
                submission = self.get('/api/v1/courses/' + str(course) + '/assignments/' + str(ident) + '/submissions/self', config['token'])
                completed = bool(submission.get('submitted_at')) and submission.get('workflow_state') in ('submitted', 'graded', 'pending_review')
                items[key] = {'external_id': key, 'title': str(assignment.get('name', 'Canvas任务'))[:160],
                              'course': str(row.get('context_short_name') or row.get('context_name') or 'Canvas')[:300],
                              'description': plain_description(assignment.get('description')),
                              'deadline': submission.get('cached_due_date') or assignment.get('due_at'),
                              'completed': completed, 'url': self.base + '/courses/' + str(course) + '/assignments/' + str(ident)}
            # Disappearance from todo may mean submission, ignore, or changed
            # visibility. Only a verified self-submission changes completion.
            for prior in known:
                if prior['external_id'] in items:
                    continue
                course, ident = prior['external_id'].split(':')
                submission = self.get('/api/v1/courses/' + course + '/assignments/' + ident + '/submissions/self', config['token'])
                if submission.get('submitted_at') and submission.get('workflow_state') in ('submitted', 'graded', 'pending_review'):
                    old = json.loads(prior['remote_json'])
                    items[prior['external_id']] = {'external_id': prior['external_id'], 'title': old['title'],
                        'course': old['notes'].split('\n')[0], 'description': '\n'.join(old['notes'].split('\n')[1:-1]),
                        'deadline': submission.get('cached_due_date') or old['deadline'],
                        'completed': True, 'url': old['url']}
            # Do not retain a credential after disconnect/re-pair during fetch.
            if self.config() != config:
                raise TodoError('Canvas连接已改变，本轮结果未保存', 409)
            result = self.store.todos.import_canvas(owner, list(items.values()))
            self.last_success = time.time()
            self.last_counts = result
            self.last_error = ''
            self.next_attempt = time.time() + 900
            return result
        except TodoError as error:
            self.last_error = str(error)
            self.next_attempt = time.time() + 300
            raise
        finally:
            self.lock.release()

    def run(self, stop):
        while not stop.wait(5):
            config = self.config()
            if config and time.time() >= self.next_attempt:
                try:
                    self.sync(config['owner'])
                except Exception:
                    self.next_attempt = time.time() + 300
