#!/usr/bin/env python3
"""Restricted SSH exporter: read-only metadata/snippets from one fixed PROJECT root.

SSH_ORIGINAL_COMMAND and command-line arguments are intentionally ignored. The
forced command always exports this directory and never evaluates a shell command.
"""
import datetime as dt
import heapq
import json
import os
import pathlib
import stat
import sys

REMOTE_ROOT = str(pathlib.Path(__file__).with_name('project-root.txt').read_text().strip()) if pathlib.Path(__file__).with_name('project-root.txt').is_file() else '/UNCONFIGURED-ELLIS-PROJECT'
MAX_FILES = 10_000
MAX_DIRECTORIES = 10_000
MAX_DOCUMENTS = 100
MAX_FILE_BYTES = 1_048_576
MAX_SNIPPET_CHARS = 500
MAX_OUTPUT_BYTES = 2_097_152
TEXT_SUFFIXES = {'.md', '.txt', '.rst'}
EXCLUDED_DIRECTORIES = {
    'node_modules', 'vendor', 'build', 'dist', 'deriveddata', 'pods', 'carthage',
    '__pycache__', 'venv', 'env', 'target', 'coverage', 'bower_components', 'tmp',
    'temp', 'obj', 'bin', 'logs', 'artifacts', 'packages-cache',
}


def _open_relative_file(root_fd, relative):
    """Open every path component relative to the granted directory, without links."""
    parts = pathlib.PurePosixPath(relative).parts
    if not parts or any(p in ('', '.', '..') or p.startswith('.') for p in parts):
        raise ValueError('Unsafe relative path')
    parent = os.dup(root_fd)
    try:
        for component in parts[:-1]:
            next_fd = os.open(component, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=parent)
            os.close(parent); parent = next_fd
        return os.open(parts[-1], os.O_RDONLY | os.O_NOFOLLOW, dir_fd=parent)
    finally:
        os.close(parent)


def build_index(root=REMOTE_ROOT):
    root = pathlib.Path(root).absolute()
    if root.is_symlink() or not root.is_dir() or root.resolve() != root:
        raise ValueError('PROJECT must be a real directory, without symbolic-link ancestors')
    root_fd = os.open(str(root), os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    scanned = 0; directories = 0; truncated = False; eligible = 0; candidates = []
    try:
        for relative_dir, child_dirs, files, dir_fd in os.fwalk('.', topdown=True, follow_symlinks=False, dir_fd=root_fd):
            directories += 1
            if directories > MAX_DIRECTORIES:
                truncated = True; break
            permitted = []
            for name in sorted(child_dirs):
                if name.startswith('.') or name.lower() in EXCLUDED_DIRECTORIES: continue
                try:
                    if stat.S_ISDIR(os.stat(name, dir_fd=dir_fd, follow_symlinks=False).st_mode): permitted.append(name)
                except OSError: continue
            child_dirs[:] = permitted
            for name in sorted(files):
                if name.startswith('.'): continue
                try: details = os.stat(name, dir_fd=dir_fd, follow_symlinks=False)
                except OSError: continue
                if not stat.S_ISREG(details.st_mode): continue
                if scanned >= MAX_FILES:
                    truncated = True; break
                scanned += 1
                if pathlib.Path(name).suffix.lower() not in TEXT_SUFFIXES or details.st_size > MAX_FILE_BYTES: continue
                eligible += 1
                relative = (pathlib.PurePosixPath(relative_dir) / name).as_posix()
                item = (details.st_mtime, relative)
                if len(candidates) < MAX_DOCUMENTS: heapq.heappush(candidates, item)
                elif item > candidates[0]: heapq.heapreplace(candidates, item)
            if scanned >= MAX_FILES:
                # If the walk hits the budget, conservatively flag a partial index.
                truncated = True; break
        if eligible > MAX_DOCUMENTS: truncated = True
        hits = []
        for _, relative in sorted(candidates, reverse=True):
            try:
                fd = _open_relative_file(root_fd, relative)
                with os.fdopen(fd, 'rb') as stream:
                    details = os.fstat(stream.fileno())
                    if not stat.S_ISREG(details.st_mode) or details.st_size > MAX_FILE_BYTES: continue
                    content = stream.read(MAX_FILE_BYTES + 1)
                if len(content) > MAX_FILE_BYTES or b'\x00' in content: continue
                text = content.decode('utf-8-sig', errors='replace')
                snippet = ''.join(c for c in text[:MAX_SNIPPET_CHARS] if c in '\n\t' or ord(c) >= 32)
                hits.append({'path': relative, 'modified': details.st_mtime, 'snippet': snippet})
            except (OSError, ValueError):
                truncated = True
        hits.sort(key=lambda h: (h['modified'], h['path']), reverse=True)
        result = {'version': 1, 'remote_root': str(root),
                  'fetched_at': dt.datetime.now(dt.timezone.utc).isoformat(),
                  'hits': hits, 'scanned': scanned, 'truncated': truncated}
        while len(json.dumps(result, ensure_ascii=False, separators=(',', ':')).encode('utf-8')) > MAX_OUTPUT_BYTES:
            if not result['hits']: raise ValueError('Index metadata exceeds output budget')
            result['hits'].pop(); result['truncated'] = True
        return result
    finally:
        os.close(root_fd)


def main():
    try:
        payload = build_index(REMOTE_ROOT)
        sys.stdout.write(json.dumps(payload, ensure_ascii=False, separators=(',', ':')) + '\n')
    except (OSError, ValueError) as error:
        # Keep stdout reserved for the JSON protocol. No document content is logged.
        sys.stderr.write('PROJECT read-only export unavailable: ' + str(error) + '\n')
        return 1
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
