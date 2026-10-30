#!/usr/bin/env python3
"""One-shot read-only file protocol for a dedicated forced-command SSH key.

No command-line argument or SSH_ORIGINAL_COMMAND is interpreted. Only one bounded
JSON request from stdin reaches LocalFileAccess; stdout is reserved for one JSON reply.
"""
import json
import signal
import sys

from file_access import FileAccessError, LocalFileAccess

VERSION = 1
MAX_REQUEST_BYTES = 16_384
MAX_RESPONSE_BYTES = 2_097_152
OPERATIONS = frozenset(('list', 'search', 'read', 'download'))
FIELDS = frozenset(('version', 'operation', 'path', 'query', 'cursor', 'offset', 'max_bytes', 'etag'))


def error_reply(message, code='invalid_request', status=400):
    return {'version': VERSION, 'ok': False, 'error': message, 'code': code, 'status': status}


def _unique_object(pairs):
    result = {}
    for name, value in pairs:
        if name in result: raise ValueError('请求字段重复')
        result[name] = value
    return result


def parse_request(raw):
    if not isinstance(raw, bytes) or not raw or len(raw) > MAX_REQUEST_BYTES:
        raise ValueError('请求为空或超过长度限制')
    if raw.endswith(b'\r\n'): raw = raw[:-2]
    elif raw.endswith(b'\n'): raw = raw[:-1]
    if b'\n' in raw or b'\r' in raw: raise ValueError('每次连接只允许一行 JSON 请求')
    value = json.loads(raw.decode('utf-8'), object_pairs_hook=_unique_object,
                       parse_constant=lambda _: (_ for _ in ()).throw(ValueError('无效 JSON 数值')))
    if not isinstance(value, dict) or set(value) - FIELDS: raise ValueError('请求含未知字段')
    if type(value.get('version')) is not int or value['version'] != VERSION: raise ValueError('协议版本不支持')
    if value.get('operation') not in OPERATIONS: raise ValueError('仅支持列出、搜索、预览和下载')
    if not isinstance(value.get('path'), str) or not value['path'].startswith('/'):
        raise ValueError('文件路径必须为绝对路径')
    limits = {'path': 4096, 'query': 1000, 'cursor': 4096, 'etag': 256}
    for field, limit in limits.items():
        if field not in value: value[field] = ''
        if not isinstance(value[field], str) or len(value[field]) > limit or '\x00' in value[field]:
            raise ValueError('文件请求字段无效')
    for field, default, minimum, maximum in [('offset', 0, 0, 2**63-1), ('max_bytes', 262144, 1, 262144)]:
        value.setdefault(field, default)
        if type(value[field]) is not int or not minimum <= value[field] <= maximum:
            raise ValueError('读取范围无效')
    return value


def process_request(raw, access=None):
    try:
        request = parse_request(raw)
        access = LocalFileAccess() if access is None else access
        result = access.handle(request['operation'], request['path'],
                               query=request['query'], cursor=request['cursor'],
                               offset=request['offset'], max_bytes=request['max_bytes'], etag=request['etag'])
        return {'version': VERSION, 'ok': True, 'result': result}
    except FileAccessError as error:
        return error_reply(error.message, error.code, error.status)
    except (ValueError, UnicodeError, TypeError, RecursionError) as error:
        return error_reply(str(error))
    except TimeoutError:
        return error_reply('文件访问超时，请缩小范围后重试', 'timeout', 408)
    except Exception:
        return error_reply('文件访问未完成，请检查系统权限后重试', 'unavailable', 500)


def encode_response(response):
    try: raw = json.dumps(response, ensure_ascii=False, allow_nan=False, separators=(',', ':')).encode('utf-8') + b'\n'
    except (ValueError, TypeError, RecursionError):
        raw = json.dumps(error_reply('无法编码文件结果', 'invalid_response', 500), ensure_ascii=False).encode('utf-8') + b'\n'
    if len(raw) > MAX_RESPONSE_BYTES:
        raw = json.dumps(error_reply('文件结果超过响应上限，请缩小范围', 'invalid_response', 413), ensure_ascii=False).encode('utf-8') + b'\n'
    return raw


def _timeout(_signal, _frame):
    raise TimeoutError()


def main():
    signal.signal(signal.SIGALRM, _timeout); signal.alarm(25)
    try:
        try:
            raw = sys.stdin.buffer.read(MAX_REQUEST_BYTES + 1)
            response = process_request(raw)
        except TimeoutError: response = error_reply('请求超时', 'timeout', 408)
        finally: signal.alarm(0)
        sys.stdout.buffer.write(encode_response(response)); sys.stdout.buffer.flush()
    except BrokenPipeError:
        return 1
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
