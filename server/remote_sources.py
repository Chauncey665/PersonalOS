"""Read-only PROJECT document source on the owner's MacBook Air.

Every search opens a fresh SSH connection using a dedicated forced-command key.
This client never sends a remote shell command, writes remote files, or uses a cache.
The forced-command exporter must be installed separately by the Air's owner.
"""
import datetime as dt
import json
import math
import os
import pathlib
import selectors
import signal
import stat
import subprocess
import time

PROJECT_URI = 'project://air'
PROJECT_HOST = '100.64.0.10'
PROJECT_USER = 'personalos-user'
PROJECT_REMOTE_ROOT = '/Users/personalos-user/Desktop/PROJECT'
PROJECT_NAME = 'PROJECT · MacBook Air（只读）'
DEFAULT_CONFIG = pathlib.Path.home() / 'Library/Application Support/PersonalOS-OpenSource/project-source.json'
MAX_OUTPUT = 2 * 1024 * 1024


class RemoteSourceError(ValueError):
    pass


def _stop_group(process):
    # The SSH ProxyCommand is a child of the SSH process; stop the whole group.
    try:
        os.killpg(process.pid, signal.SIGTERM)
    except ProcessLookupError:
        pass
    try:
        process.wait(timeout=1)
    except subprocess.TimeoutExpired:
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        process.wait(timeout=1)


def bounded_output(argv, timeout=20, max_output=MAX_OUTPUT):
    """Read at most max_output stdout bytes; stderr is drained and never surfaced."""
    output = bytearray()
    process = None
    selector = selectors.DefaultSelector()
    try:
        process = subprocess.Popen(argv, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                   stderr=subprocess.PIPE, start_new_session=True)
        selector.register(process.stdout, selectors.EVENT_READ, 'stdout')
        selector.register(process.stderr, selectors.EVENT_READ, 'stderr')
        deadline = time.monotonic() + timeout
        while selector.get_map():
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise RemoteSourceError('连接 MacBook Air 超时；本次未提供 PROJECT 检索结果。')
            for key, _ in selector.select(min(remaining, .25)):
                chunk = os.read(key.fileobj.fileno(), 65536)
                if not chunk:
                    selector.unregister(key.fileobj)
                    key.fileobj.close()
                    continue
                if key.data == 'stdout':
                    if len(output) + len(chunk) > max_output:
                        raise RemoteSourceError('PROJECT 导出超过 2MB 限制；本次未提供检索结果。')
                    output.extend(chunk)
        remaining = max(.01, deadline - time.monotonic())
        try:
            code = process.wait(timeout=remaining)
        except subprocess.TimeoutExpired:
            raise RemoteSourceError('连接 MacBook Air 超时；本次未提供 PROJECT 检索结果。') from None
        if code:
            raise RemoteSourceError('无法读取 MacBook Air 的 PROJECT；请检查 Air 在线、只读配对及主机验证。')
        return bytes(output)
    except RemoteSourceError:
        if process is not None:
            _stop_group(process)
        raise
    except (OSError, subprocess.SubprocessError):
        if process is not None:
            _stop_group(process)
        raise RemoteSourceError('无法建立 PROJECT 只读连接；本次未提供检索结果。') from None
    finally:
        selector.close()
        if process is not None:
            for stream in (process.stdout, process.stderr):
                if stream is not None:
                    stream.close()


class ProjectRemoteSource:
    def __init__(self, config_path=None, runner=None):
        self.config_path = pathlib.Path(config_path) if config_path else DEFAULT_CONFIG
        self.runner = runner or bounded_output

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
            from deployment_config import valid_remote
            if not valid_remote(config,'remote_root'):
                return None, 'invalid_config'
            if config.get('enabled') is False:
                return None, 'disabled'
            for field in ('key_path', 'known_hosts_path'):
                raw = config.get(field)
                if not isinstance(raw, str) or any(ord(ch) < 32 for ch in raw):
                    return None, 'invalid_config'
                path = pathlib.Path(raw).expanduser()
                if not path.is_absolute() or path.is_symlink() or not path.is_file():
                    return None, 'invalid_config'
                if field == 'key_path' and stat.S_IMODE(path.stat().st_mode) & 0o077:
                    return None, 'private_permissions_required'
                config[field] = str(path)
            return config, 'configured'
        except (OSError, ValueError, TypeError):
            return None, 'invalid_config'

    def status(self):
        config, state = self._config()
        return {'project_source_configured': config is not None, 'project_source_status': state}

    def roots(self):
        if not self.status()['project_source_configured']:
            return []
        return [{'id': 'project-air', 'path': PROJECT_URI, 'name': PROJECT_NAME, 'read_only': True}]

    @staticmethod
    def _arguments(config):
        # Quoting here is OpenSSH config syntax, not a shell command. Values are local paths.
        known_hosts = config['known_hosts_path'].replace('\\', '\\\\').replace('"', '\\"')
        return [
            '/usr/bin/ssh', '-F', '/dev/null', '-T', '-a', '-x',
            '-o', 'BatchMode=yes', '-o', 'IdentitiesOnly=yes', '-o', 'IdentityAgent=none',
            '-o', 'StrictHostKeyChecking=yes', '-o', 'UserKnownHostsFile="' + known_hosts + '"',
            '-o', 'GlobalKnownHostsFile=/dev/null', '-o', 'ClearAllForwardings=yes',
            '-o', 'PermitLocalCommand=no', '-o', 'RequestTTY=no',
            '-o', 'ConnectTimeout=8', '-o', 'ConnectionAttempts=1',
            '-o', 'ServerAliveInterval=5', '-o', 'ServerAliveCountMax=2', '-o', 'LogLevel=ERROR',
            '-o', 'ProxyCommand=/Applications/Tailscale.app/Contents/MacOS/Tailscale nc %h %p',
            '-i', config['key_path'], '-p', '22', config['user'] + '@' + config['host'],
        ]  # No remote command: the dedicated key's forced command is the only operation.

    @staticmethod
    def _validate_export(data, expected_root=PROJECT_REMOTE_ROOT):
        if not isinstance(data, dict) or data.get('version') != 1 or data.get('remote_root') != expected_root:
            raise ValueError('Invalid source')
        fetched = data.get('fetched_at')
        if not isinstance(fetched, str) or len(fetched) > 100:
            raise ValueError('Invalid source timestamp')
        parsed = dt.datetime.fromisoformat(fetched.replace('Z', '+00:00'))
        if parsed.tzinfo is None:
            raise ValueError('Invalid source timestamp')
        rows = data.get('hits')
        if not isinstance(rows, list) or len(rows) > 100:
            raise ValueError('Invalid source entries')
        scanned = data.get('scanned')
        if isinstance(scanned, bool) or not isinstance(scanned, int) or not 0 <= scanned <= 10001:
            raise ValueError('Invalid source count')
        if not isinstance(data.get('truncated'), bool):
            raise ValueError('Invalid source limit')
        hits = []
        seen = set()
        for item in rows:
            if not isinstance(item, dict):
                raise ValueError('Invalid source entry')
            raw = item.get('path')
            if not isinstance(raw, str) or not raw or len(raw) > 4096 or any(ord(ch) < 32 for ch in raw):
                raise ValueError('Invalid source path')
            path = pathlib.PurePosixPath(raw)
            if path.is_absolute() or '..' in path.parts or not path.parts or str(path) in seen:
                raise ValueError('Invalid source path')
            if path.suffix.lower() not in ('.md', '.txt', '.rst'):
                raise ValueError('Invalid source document type')
            modified = item.get('modified')
            if isinstance(modified, bool) or not isinstance(modified, (int, float)) or not math.isfinite(modified) or modified < 0:
                raise ValueError('Invalid source date')
            snippet = item.get('snippet', '')
            if not isinstance(snippet, str) or len(snippet) > 500:
                raise ValueError('Invalid source snippet')
            seen.add(str(path))
            hits.append({'path': expected_root + '/' + str(path), 'modified': modified, 'snippet': snippet})
        hits.sort(key=lambda item: (-item['modified'], item['path']))
        return hits

    def search(self, query):
        config, state = self._config()
        if config is None:
            raise RemoteSourceError('PROJECT 只读资料源尚未完成本机配对配置；请先在 MacBook Air 授权。')
        try:
            raw = self.runner(self._arguments(config), timeout=20, max_output=MAX_OUTPUT)
            if not isinstance(raw, bytes) or len(raw) > MAX_OUTPUT:
                raise ValueError('Invalid source output')
            data = json.loads(raw.decode('utf-8'))
            hits = self._validate_export(data,config['remote_root'])
            term = query.strip().casefold()
            if term and term != 'project':
                hits = [item for item in hits if term in pathlib.PurePosixPath(item['path']).name.casefold() or term in item['snippet'].casefold()]
            return {
                'scope': '通过 SSH 实时读取 MacBook Air 的 PROJECT（只读）；范围为导出器提供的最新最多 100 个项目文档，按修改时间从新到旧。',
                'source': 'project-air', 'read_only': True, 'remote_root': config['remote_root'],
                'fetched_at': dt.datetime.now(dt.timezone.utc).isoformat(),
                'source_fetched_at': data['fetched_at'], 'hits': hits,
                'matched_count': len(hits), 'exported_count': len(data['hits']),
                'scanned': data['scanned'], 'truncated': data['truncated'],
            }
        except RemoteSourceError:
            raise
        except (ValueError, TypeError, UnicodeError, OverflowError):
            raise RemoteSourceError('PROJECT 导出内容校验失败；本次未提供检索结果。') from None
