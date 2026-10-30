"""Per-PersonalOS local model settings; never edits a shared Ollama alias.

``keep_alive`` is returned as integer seconds. The Ollama request adapter should
serialize it as ``str(value) + 's'`` to make the duration unit explicit.
Only a missing config uses defaults. An invalid existing config raises rather
than silently selecting a different model or allowing a cloud/URL target.
"""
import json
import os
import pathlib
import re
import stat


_TAG = re.compile(r'[A-Za-z0-9][A-Za-z0-9_.-]{0,95}(?::[A-Za-z0-9][A-Za-z0-9_.-]{0,95})?\Z')
_FIELDS = {'model', 'num_ctx', 'keep_alive'}
_MAX_BYTES = 4096


def _model(value):
    # Intentionally exclude namespaces/registries and file paths. This config
    # selects an already installed simple local tag, not a download source.
    if (not isinstance(value, str) or not _TAG.fullmatch(value)
            or 'cloud' in value.lower() or '..' in value
            or value.split(':', 1)[0].lower() in {'http', 'https', 'file', 'ftp', 'ollama'}):
        raise ValueError('本地模型名称无效；只允许本地模型标签，不允许云端、网址或文件路径')
    return value


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError('本地模型配置含有重复字段')
        result[key] = value
    return result


def settings(home, default_model='qwen3.5:9b'):
    """Read ``home/local-model.json`` without touching any global config.

    Optional fields: model (local tag), num_ctx (8192..32768), keep_alive
    (0..300 seconds). Unknown keys, booleans masquerading as integers, oversized
    files, symlinks and non-regular files are rejected. No network calls occur.
    """
    result = {'model': _model(default_model), 'num_ctx': 32768, 'keep_alive': 60}
    path = pathlib.Path(home) / 'local-model.json'
    try:
        descriptor = os.open(path, os.O_RDONLY | os.O_NONBLOCK | os.O_NOFOLLOW)
    except FileNotFoundError:
        return result
    except OSError:
        raise ValueError('无法安全读取本地模型配置') from None
    try:
        info = os.fstat(descriptor)
        if not stat.S_ISREG(info.st_mode) or info.st_size > _MAX_BYTES:
            raise ValueError('本地模型配置必须是不超过4096字节的普通文件')
        with os.fdopen(descriptor, 'rb') as stream:
            descriptor = None
            raw = stream.read(_MAX_BYTES + 1)
        if len(raw) > _MAX_BYTES:
            raise ValueError('本地模型配置过长')
        try:
            config = json.loads(raw.decode('utf-8'), object_pairs_hook=_unique_object)
        except (UnicodeError, json.JSONDecodeError):
            raise ValueError('本地模型配置不是有效的UTF-8 JSON') from None
        if not isinstance(config, dict) or set(config) - _FIELDS:
            raise ValueError('本地模型配置格式无效或含有未知字段')
        if 'model' in config:
            result['model'] = _model(config['model'])
        for key, low, high in [('num_ctx', 8192, 32768), ('keep_alive', 0, 300)]:
            if key in config:
                if type(config[key]) is not int or not low <= config[key] <= high:
                    raise ValueError('本地模型配置中的' + key + '超出允许范围')
                result[key] = config[key]
        return result
    except OSError:
        raise ValueError('读取本地模型配置失败') from None
    finally:
        if descriptor is not None:
            os.close(descriptor)
