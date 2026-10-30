"""Install only the dedicated read-only file agent and its restricted public key.

Executed on Air after the owner authenticates interactively. Does not modify user
files, TCC settings, VPN settings, or the existing PROJECT-only authorization.
"""
import json
import os
import pathlib
import re
import sys
import tempfile

HOME = pathlib.Path.home()
DESTINATION = HOME/'.local/share/personal-os-files'
MARKER = 'personalos-files-readonly'


def write_private(path, data):
    if path.is_symlink(): raise ValueError('Refusing symbolic-link configuration file')
    if path.exists() and path.stat().st_uid != os.getuid(): raise ValueError('Configuration is not owned by this user')
    fd, temporary = tempfile.mkstemp(prefix='personal-os-files-', dir=str(path.parent))
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, 'wb') as stream: stream.write(data)
        os.replace(temporary, path)
    finally:
        try: os.unlink(temporary)
        except FileNotFoundError: pass


def main():
    if pathlib.Path.home() != HOME or HOME.stat().st_uid != os.getuid():
        raise ValueError('Authenticate as the confirmed Air account the-current-user')
    raw = sys.stdin.buffer.read(524289)
    if len(raw) > 524288: raise ValueError('Installation payload too large')
    payload = json.loads(raw.decode('utf-8'))
    if set(payload) != {'public_key', 'agent', 'core'}: raise ValueError('Unexpected installation fields')
    key = payload['public_key'].strip()
    if not re.fullmatch(r'ssh-ed25519 [A-Za-z0-9+/]+={0,3} personalos-files-readonly', key):
        raise ValueError('Invalid dedicated public key')
    sources = {'file_agent.py': payload['agent'], 'file_access.py': payload['core']}
    for name, source in sources.items():
        if not isinstance(source, str) or len(source.encode('utf-8')) > 200_000:
            raise ValueError('Unexpected agent source')
        compile(source, name, 'exec')
    for directory in [HOME/'.local', HOME/'.local/share', DESTINATION, HOME/'.ssh']:
        if directory.is_symlink(): raise ValueError('Refusing symbolic-link installation directory')
        directory.mkdir(mode=0o700, exist_ok=True)
        if directory.stat().st_uid != os.getuid(): raise ValueError('Directory is not owned by this user')
    DESTINATION.chmod(0o700); (HOME/'.ssh').chmod(0o700)
    for name, source in sources.items(): write_private(DESTINATION/name, source.encode('utf-8'))
    authorization = HOME/'.ssh/authorized_keys'
    if authorization.is_symlink(): raise ValueError('Refusing symbolic-link authorized_keys')
    previous = authorization.read_text() if authorization.exists() else ''
    key_body = key.split()[1]
    lines = [line for line in previous.splitlines()
             if not line.rstrip().endswith(' '+MARKER) and key_body not in line.split()]
    # This marker is distinct from personal-os-project-readonly. Its line remains intact.
    lines.append('restrict,command="/usr/bin/python3 '+str(DESTINATION/'file_agent.py')+'" '+key)
    write_private(authorization, ('\n'.join(lines)+'\n').encode('utf-8'))
    print(json.dumps({'installed': True, 'remote_home': str(HOME), 'mode': 'restricted-readonly-file-protocol'}))


if __name__ == '__main__':
    main()
