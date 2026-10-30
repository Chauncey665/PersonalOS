"""A user-selected assistant name is configuration data, never an authorization."""
import json,pathlib,re,stat
DEFAULT_NAME='PersonalOS'
def valid_name(value):
 if not isinstance(value,str) or not re.fullmatch(r'[\w\u4e00-\u9fff-]{1,24}',value) or not any(c.isalpha() for c in value):raise ValueError('Choose 1–24 letters, Chinese characters, digits, underscore or hyphen; no instructions or whitespace')
 return value

def profile(home):
 p=pathlib.Path(home)/'assistant-profile.json'
 if not p.exists():return {'name':DEFAULT_NAME}
 if p.is_symlink() or not p.is_file() or p.stat().st_size>2048 or stat.S_IMODE(p.stat().st_mode)&0o077:raise ValueError('Assistant profile must be a private regular file')
 data=json.loads(p.read_text())
 if not isinstance(data,dict) or set(data)!={'name'}:raise ValueError('Invalid assistant profile')
 return {'name':valid_name(data['name'])}
