"""Opt-in APNs delivery. Credentials are read only from the local push.json.

Production path: ~/Library/Application Support/PersonalOS-OpenSource/push.json
Required fields: enabled=true, key_id, team_id, private_key_path, topic.
The topic defaults to org.personalosassistant.app. No credential is returned by APIs.
Uses macOS system curl (HTTP/2) and openssl (ES256); no extra dependency/download.
Apple protocol: https://developer.apple.com/documentation/usernotifications/sending-notification-requests-to-apns
"""
import base64
import dataclasses
import hashlib
import json
import pathlib
import re
import stat
import subprocess
import threading
import time

DEFAULT_CONFIG = pathlib.Path.home() / 'Library/Application Support/PersonalOS-OpenSource/push.json'
TOKEN_RE = re.compile(r'^[0-9a-fA-F]{32,512}$')
SAFE_REASONS = {
    'BadDeviceToken', 'DeviceTokenNotForTopic', 'Unregistered', 'BadTopic',
    'TopicDisallowed', 'ExpiredProviderToken', 'InvalidProviderToken',
    'MissingProviderToken', 'TooManyProviderTokenUpdates', 'TooManyRequests',
    'InternalServerError', 'ServiceUnavailable', 'Shutdown', 'BadPayload',
    'PayloadEmpty', 'PayloadTooLarge', 'BadPriority', 'BadExpirationDate',
}


def validate_registration(token, environment):
    if not isinstance(token, str) or not TOKEN_RE.fullmatch(token) or len(token) % 2:
        raise ValueError('APNs 设备注册格式无效')
    if environment not in ('sandbox', 'production'):
        raise ValueError('APNs 环境必须为 sandbox 或 production')
    return token.lower(), environment


def _b64(value):
    return base64.urlsafe_b64encode(value).rstrip(b'=').decode('ascii')


def _der_signature_to_raw(data):
    """Convert the DER ECDSA signature produced by openssl to JOSE R||S."""
    def length(index):
        first = data[index]
        if first < 128:
            return first, index + 1
        size = first & 127
        if size not in (1, 2) or index + size >= len(data):
            raise ValueError('Invalid signature')
        return int.from_bytes(data[index + 1:index + 1 + size], 'big'), index + 1 + size
    try:
        if data[0] != 0x30:
            raise ValueError('Invalid signature')
        sequence_length, pos = length(1)
        if pos + sequence_length != len(data):
            raise ValueError('Invalid signature')
        values = []
        for _ in range(2):
            if data[pos] != 0x02:
                raise ValueError('Invalid signature')
            size, pos = length(pos + 1)
            value = data[pos:pos + size]
            if len(value) != size or not value or value[0] & 0x80:
                raise ValueError('Invalid signature')
            number = int.from_bytes(value, 'big')
            if number <= 0 or number.bit_length() > 256:
                raise ValueError('Invalid signature')
            values.append(number.to_bytes(32, 'big'))
            pos += size
        if pos != len(data):
            raise ValueError('Invalid signature')
        return b''.join(values)
    except (IndexError, OverflowError):
        raise ValueError('Invalid signature') from None


@dataclasses.dataclass(frozen=True)
class PushResult:
    outcome: str  # sent, retry, permanent, unconfigured
    code: str     # Fixed public code only; never an exception/body/token.
    deactivate: bool = False


class APNsProvider:
    def __init__(self, config_path=None, runner=None):
        # The optional path/runner are dependency injection for isolated tests.
        self.config_path = pathlib.Path(config_path) if config_path else DEFAULT_CONFIG
        self.runner = runner or subprocess.run
        self._jwt = None
        self._jwt_at = 0
        self._jwt_fingerprint = None
        self._lock = threading.Lock()

    def _config(self):
        try:
            p = self.config_path
            if not p.exists():
                return None, 'not_configured'
            if p.is_symlink() or not p.is_file() or p.stat().st_size > 8192:
                return None, 'invalid_config'
            if stat.S_IMODE(p.stat().st_mode) & 0o077:
                return None, 'private_permissions_required'
            config = json.loads(p.read_text())
            if not isinstance(config, dict):
                return None, 'invalid_config'
            if config.get('enabled') is not True:
                return None, 'disabled'
            for field in ('key_id', 'team_id'):
                if not isinstance(config.get(field), str) or not re.fullmatch(r'[A-Z0-9]{10}', config[field]):
                    return None, 'invalid_config'
            topic = config.get('topic', 'org.personalosassistant.app')
            if not isinstance(topic, str) or not re.fullmatch(r'[A-Za-z0-9.-]{3,255}', topic):
                return None, 'invalid_config'
            key_path = pathlib.Path(config['private_key_path']).expanduser()
            if not key_path.is_absolute() or key_path.is_symlink() or not key_path.is_file():
                return None, 'invalid_config'
            key_stat = key_path.stat()
            if key_stat.st_size > 16384 or stat.S_IMODE(key_stat.st_mode) & 0o077:
                return None, 'private_permissions_required'
            config['private_key_path'] = str(key_path)
            config['topic'] = topic
            config['_fingerprint'] = (config['key_id'], config['team_id'], topic,
                                      str(key_path), key_stat.st_mtime_ns, key_stat.st_size)
            return config, 'configured'
        except (OSError, ValueError, TypeError, KeyError):
            return None, 'invalid_config'

    def status(self):
        config, state = self._config()
        return {'push_configured': config is not None, 'push_status': state}

    def _authentication(self, config, now):
        with self._lock:
            if (self._jwt and 0 <= now - self._jwt_at < 3000 and
                    self._jwt_fingerprint == config['_fingerprint']):
                return self._jwt
            header = _b64(json.dumps({'alg': 'ES256', 'kid': config['key_id']}, separators=(',', ':')).encode())
            claims = _b64(json.dumps({'iss': config['team_id'], 'iat': int(now)}, separators=(',', ':')).encode())
            unsigned = header + '.' + claims
            try:
                result = self.runner(['/usr/bin/openssl', 'dgst', '-sha256', '-sign', config['private_key_path']],
                                     input=unsigned.encode(), capture_output=True, timeout=10)
                if result.returncode != 0:
                    raise ValueError('Signing failed')
                signature = _der_signature_to_raw(result.stdout)
            except Exception:
                raise RuntimeError('provider_signing_failed') from None
            self._jwt = unsigned + '.' + _b64(signature)
            self._jwt_at = now
            self._jwt_fingerprint = config['_fingerprint']
            return self._jwt

    @staticmethod
    def _quote(value):
        return '"' + value.replace('\\', '\\\\').replace('"', '\\"').replace('\n', '\\n').replace('\r', '\\r') + '"'

    def send(self, device_token, environment, task_id, event, notification_id, expires, summary=None):
        config, state = self._config()
        if config is None:
            return PushResult('unconfigured', state)
        try:
            device_token, environment = validate_registration(device_token, environment)
            now = time.time()
            jwt = self._authentication(config, now)
            body = {
                'awaiting_confirmation': '有一项任务等待你确认，请打开 App 查看。',
                'failed': '有一项任务未能完成，请打开 App 查看。',
                'needs_review': '有一项任务需要检查，请打开 App 查看。',
                'needs_details': '有一项任务需要补充信息，请打开 App 查看。',
                'reminder_due': '你安排的提醒已到时间，请打开 App 查看。',
            }.get(event, '有一项任务有处理结果，请打开 App 查看进度。')
            if summary is not None:
                if not isinstance(summary,str) or len(summary)>220 or any(ord(ch)<32 for ch in summary):raise ValueError('Invalid digest')
                body=summary
            payload = json.dumps({'aps': {'alert': {'title': 'PersonalOS', 'body': body}, 'sound': 'default'},
                                  'task_id': task_id, 'event': event}, ensure_ascii=False, separators=(',', ':'))
            host = 'api.sandbox.push.apple.com' if environment == 'sandbox' else 'api.push.apple.com'
            # Keep the APNs token, provider JWT and payload out of argv/process listings.
            options = [
                'silent', 'show-error', 'http2', 'request = "POST"', 'proto = "=https"',
                'connect-timeout = 10', 'max-time = 20',
                'url = ' + self._quote('https://' + host + '/3/device/' + device_token),
                'header = ' + self._quote('authorization: bearer ' + jwt),
                'header = ' + self._quote('apns-topic: ' + config['topic']),
                'header = "apns-push-type: alert"', 'header = "apns-priority: 10"',
                'header = ' + self._quote('apns-id: ' + notification_id),
                'header = ' + self._quote('apns-expiration: ' + str(int(expires))),
                'header = ' + self._quote('apns-collapse-id: ' + hashlib.sha256((task_id + ':' + event).encode()).hexdigest()),
                'header = "content-type: application/json"',
                'data-binary = ' + self._quote(payload),
                'write-out = "\\n%{http_code}"',
            ]
            result = self.runner(['/usr/bin/curl', '--disable', '--config', '-'], input=('\n'.join(options) + '\n').encode(),
                                 capture_output=True, timeout=25)
            if result.returncode != 0:
                return PushResult('retry', 'transport_failed')
            response_body, raw_status = result.stdout.decode('utf-8', errors='replace').rsplit('\n', 1)
            status = int(raw_status)
            if status == 200:
                return PushResult('sent', 'accepted_by_apns')
            try:
                raw_reason = json.loads(response_body).get('reason', '')
            except (ValueError, AttributeError):
                raw_reason = ''
            reason = raw_reason if raw_reason in SAFE_REASONS else 'apns_rejected'
            if status == 410 or reason in ('BadDeviceToken', 'DeviceTokenNotForTopic', 'Unregistered'):
                return PushResult('permanent', reason, deactivate=True)
            if status in (429, 500, 503) or status >= 500:
                return PushResult('retry', reason)
            # Credentials may be corrected locally; bounded retries retain the event.
            if status == 403:
                return PushResult('retry', reason)
            return PushResult('permanent', reason)
        except Exception:
            return PushResult('retry', 'provider_request_failed')


class PushDispatcher:
    def __init__(self, store, provider=None, stop=None):
        self.s = store
        self.provider = provider if provider is not None else APNsProvider(store.home / 'push.json')
        self.stop = stop or threading.Event()

    def run(self):
        while not self.stop.wait(1):
            try:
                self.tick()
            except Exception:
                # Never include raw exceptions: HTTP errors may contain tokens/keys.
                self.stop.wait(2)

    def tick(self, now=None):
        now = time.time() if now is None else now
        if not self.provider.status()['push_configured']:
            return 0  # No signing, no curl, no outbound network when unconfigured.
        with self.s.db() as c:
            c.execute('BEGIN IMMEDIATE')
            c.execute("UPDATE notification_outbox SET state='retry' WHERE state='sending' AND next_attempt<=?", (now,))
            c.execute("UPDATE notification_outbox SET state='expired',last_error='expired' WHERE state IN ('pending','retry') AND expires<=?", (now,))
            row = c.execute('''SELECT o.*,p.token,p.environment FROM notification_outbox o
                JOIN push_devices p ON p.device=o.device AND p.active=1
                JOIN devices d ON d.token=o.device
                WHERE o.state IN ('pending','retry') AND o.next_attempt<=? AND o.expires>?
                ORDER BY o.created LIMIT 1''', (now, now)).fetchone()
            if row is None:
                return 0
            row = dict(row)
            task = c.execute('SELECT state,owner,plan FROM tasks WHERE id=?', (row['task'],)).fetchone()
            expected = 'completed' if row['event'] == 'reminder_due' else row['event']
            local = c.execute('SELECT scheduled FROM local_reminders WHERE task=? AND device=?', (row['task'], row['device'])).fetchone()
            if task is None or task['state'] != expected:
                c.execute("UPDATE notification_outbox SET state='superseded',last_error='task_state_changed' WHERE id=?", (row['id'],))
                return 0
            if row['event'] == 'reminder_due' and local and local['scheduled']:
                c.execute("UPDATE notification_outbox SET state='suppressed',last_error='local_reminder' WHERE id=?", (row['id'],))
                return 0
            # A lease permits recovery after process interruption without holding a DB lock during network IO.
            c.execute("UPDATE notification_outbox SET state='sending',next_attempt=? WHERE id=?", (now + 60, row['id']))
        try:
            meta=json.loads(task['plan'] or '{}');summary=meta.get('daily_rollover_summary') if meta.get('kind')=='daily_rollover' and task['owner']==row['device'] else None
            if summary:
                from daily_rollover import DailyRollover
                summary=DailyRollover(self.s).current_summary(row['device'],meta)
                if summary is None:
                    with self.s.db() as c:c.execute("UPDATE notification_outbox SET state='retry',next_attempt=? WHERE id=?",(now+60,row['id']))
                    return 0
            extra={'summary':summary} if summary else {}
            result = self.provider.send(row['token'], row['environment'], row['task'], row['event'], row['id'], row['expires'],**extra)
        except Exception:
            result = PushResult('retry', 'provider_request_failed')
        attempts = row['attempts'] + (0 if result.outcome == 'unconfigured' else 1)
        state = {'sent': 'sent', 'permanent': 'failed', 'retry': 'retry', 'unconfigured': 'pending'}.get(result.outcome, 'retry')
        if state == 'retry' and attempts >= 8:
            state = 'failed'
        # Persist only fixed codes. A custom provider cannot leak a raw HTTP body/token.
        safe_codes = SAFE_REASONS | {'accepted_by_apns', 'apns_rejected', 'transport_failed', 'provider_request_failed',
                                    'not_configured', 'disabled', 'invalid_config', 'private_permissions_required'}
        code = result.code if result.code in safe_codes else 'provider_request_failed'
        with self.s.db() as c:
            c.execute('UPDATE notification_outbox SET state=?,attempts=?,next_attempt=?,last_error=? WHERE id=?',
                      (state, attempts, now + min(3600, 5 * (2 ** attempts)), code, row['id']))
            if result.deactivate:
                c.execute('UPDATE push_devices SET active=0 WHERE device=? AND token=?', (row['device'], row['token']))
            c.execute('INSERT INTO audit(task,event,detail,at) VALUES(?,?,?,?)',
                      (row['task'], 'push_' + state, json.dumps({'code': code, 'attempts': attempts}), time.time()))
        return 1
