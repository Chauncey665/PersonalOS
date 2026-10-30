"""Local-admin-selected integration endpoints. No phone can change this file."""
import ipaddress,json,pathlib,re,stat,urllib.parse

def canvas_base(home):
    p=pathlib.Path(home)/'integrations.json'
    if not p.exists():return 'https://canvas.example.edu'
    if p.is_symlink() or not p.is_file() or p.stat().st_size>4096 or stat.S_IMODE(p.stat().st_mode)&0o077:raise ValueError('Integration config must be a private regular file')
    data=json.loads(p.read_text())
    if not isinstance(data,dict) or set(data)!={'canvas_base_url'}:raise ValueError('Invalid integration config')
    return validate_canvas_base(data['canvas_base_url'])

def validate_canvas_base(value):
    if not isinstance(value,str):raise ValueError('Canvas requires a public HTTPS origin')
    p=urllib.parse.urlsplit(value)
    if p.scheme!='https' or not p.hostname or p.username or p.password or p.port or p.query or p.fragment or p.path not in ('','/'):raise ValueError('Canvas requires an HTTPS origin without paths or credentials')
    host=p.hostname.lower()
    if not re.fullmatch(r'[a-z0-9](?:[a-z0-9.-]{0,251}[a-z0-9])?',host) or '.' not in host or '..' in host or host.endswith(('.local','.localhost','.ts.net')):raise ValueError('Canvas requires a public hostname')
    try:ipaddress.ip_address(host)
    except ValueError:pass
    else:raise ValueError('Canvas does not accept IP addresses')
    return 'https://'+host

def valid_remote(config,root_field):
    if not isinstance(config,dict):return False
    host,user,root=(config.get(k) for k in ('host','user',root_field))
    if not isinstance(host,str) or len(host)>253 or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9.-]*',host):return False
    try:
        address=ipaddress.ip_address(host)
        private=address.is_private or address in ipaddress.ip_network('100.64.0.0/10')
    except ValueError:private=host.endswith(('.ts.net','.local'))
    if not private or user in ('root','nobody') or not isinstance(user,str) or not re.fullmatch(r'[a-z_][a-z0-9_-]{0,63}',user):return False
    if not isinstance(root,str) or not root.startswith('/') or any(ord(c)<32 for c in root) or '..' in pathlib.PurePosixPath(root).parts or root=='/':return False
    return True
