"""Bootstrap body executed on Air only after the user authenticates in Terminal.

Receives the public key and exporter source over the encrypted SSH stdin channel.
The dedicated key installed here can invoke only the fixed read-only exporter.
"""
import json
import os
import pathlib
import re
import tempfile
import sys

HOME = pathlib.Path.home()
ROOT = HOME / 'Desktop/PROJECT'
DESTINATION = HOME / '.local/share/personal-os'
MARKER = 'personal-os-project-readonly'


def write_private(path, data, mode=0o600):
    if path.is_symlink(): raise ValueError('Refusing symbolic-link configuration file')
    if path.exists() and path.stat().st_uid != os.getuid(): raise ValueError('Configuration file is not owned by the logged-in user')
    fd, temporary = tempfile.mkstemp(prefix='personal-os-', dir=str(path.parent))
    try:
        os.fchmod(fd, mode)
        with os.fdopen(fd, 'wb') as stream: stream.write(data)
        os.replace(temporary, path)
    finally:
        try: os.unlink(temporary)
        except FileNotFoundError: pass


def main():
    if pathlib.Path.home() != HOME or HOME.stat().st_uid != os.getuid():
        raise ValueError('Please authenticate as the-current-user on the intended Air')
    if ROOT.is_symlink() or not ROOT.is_dir() or ROOT.resolve() != ROOT:
        raise ValueError('The fixed PROJECT path must be an existing real directory')
    # Desktop privacy permission is checked before installing any key. A denied read
    # requires the owner to decide whether to allow Terminal/Remote Login access.
    with os.scandir(ROOT): pass
    raw = sys.stdin.buffer.read(262145)
    if len(raw) > 262144: raise ValueError('Setup payload unexpectedly large')
    payload = json.loads(raw.decode('utf-8'))
    key = payload['public_key'].strip()
    if not re.fullmatch(r'ssh-ed25519 [A-Za-z0-9+/]+={0,3} personal-os-project-readonly', key):
        raise ValueError('Invalid dedicated public key')
    exporter = payload['exporter'].encode('utf-8')
    if len(exporter) > 100_000: raise ValueError('Exporter source unexpectedly large')
    for folder in [HOME/'.local', HOME/'.local/share', DESTINATION, HOME/'.ssh']:
        if folder.is_symlink(): raise ValueError('Refusing symbolic-link configuration directory')
        folder.mkdir(mode=0o700, exist_ok=True)
        if folder.stat().st_uid != os.getuid(): raise ValueError('Configuration directory is not user-owned')
    DESTINATION.chmod(0o700)
    (HOME/'.ssh').chmod(0o700)
    script = DESTINATION/'project_readonly.py'
    write_private(script, exporter)
    authorization = HOME/'.ssh/authorized_keys'
    if authorization.is_symlink(): raise ValueError('Refusing symbolic-link authorized_keys')
    previous = authorization.read_text() if authorization.exists() else ''
    key_body = key.split()[1]
    lines = []
    for line in previous.splitlines():
        if line.rstrip().endswith(' '+MARKER) or key_body in line.split(): continue
        lines.append(line)
    # restrict disables port, agent and X11 forwarding, PTY and user rc execution.
    forced = 'restrict,command="/usr/bin/python3 '+str(script)+'" '+key
    lines.append(forced)
    write_private(authorization, ('\n'.join(lines)+'\n').encode('utf-8'))
    print(json.dumps({'installed': True, 'remote_root': str(ROOT), 'mode': 'restricted-readonly-export'}))


if __name__ == '__main__':
    main()
