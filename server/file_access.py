"""PersonalOS read-only file access shared by Mini and Air. Pure Python standard library.

Importing this module does not read configuration or credentials. LocalFileAccess
uses the process user's ordinary macOS permissions: never sudo, TCC bypass or writes.
"""
import base64
import collections
import datetime as dt
import errno
import hashlib
import json
import mimetypes
import os
import pathlib
import re
import selectors
import signal
import stat
import subprocess
import time

PAGE_SIZE = 100
PREVIEW_BYTES = 65536
CHUNK_BYTES = 262144
MAX_DIRECTORY_SCAN = 200000
SEARCH_SCAN_BUDGET = 5000
SEARCH_DEPTH = 4
SEARCH_SECONDS = 4
RPC_BYTES = 2 * 1024 * 1024
AIR_HOST = '100.64.0.10'
AIR_USER = 'personalos-user'
AIR_HOME = '/Users/personalos-user'


class FileAccessError(ValueError):
    def __init__(self, status, code, message):
        self.status, self.code, self.message = status, code, message
        super().__init__(message)


PUBLIC_ERRORS = {
    'access_not_enabled':'Mac mini 的广域只读文件访问尚未在本机启用。',
    'permission_denied':'系统未允许读取此位置。请在 Mac 检查文件权限或完整磁盘访问权限。',
    'sensitive_path':'此位置可能包含凭据、私钥或浏览器会话，PersonalOS 默认不提供访问。',
    'sensitive_content':'此文件片段疑似包含凭据或私钥，PersonalOS 默认不提供内容。',
    'symlink_not_allowed':'为保持只读路径边界，PersonalOS 不跟随符号链接。请使用文件的真实路径。',
    'not_found':'文件或目录不存在，可能已移动或删除。',
    'not_directory':'此路径不是普通目录。',
    'not_regular_file':'仅能预览和下载普通文件。',
    'file_changed':'文件在读取期间已变化，请重新开始下载或预览。',
    'directory_changed':'目录已变化，请返回第一页刷新。',
    'invalid_cursor':'分页标记无效，请返回第一页刷新。',
    'invalid_range':'文件读取范围无效，请重新开始。',
    'invalid_request':'文件访问参数无效。',
    'unsupported_operation':'不支持此文件操作。',
    'directory_limit':'此目录条目过多，请使用更具体的路径或文件名检索。',
    'timeout':'只读访问超时，请缩小范围或稍后重试。',
    'unavailable':'MacBook Air 只读文件源尚未就绪或当前无法连接。',
    'invalid_response':'MacBook Air 返回的文件数据未通过校验。',
}


def fail(status, code):
    raise FileAccessError(status, code, PUBLIC_ERRORS[code])


def normalize_path(raw):
    if not isinstance(raw,str) or not raw.startswith('/') or len(raw)>4096 or any(ord(ch)<32 for ch in raw):
        fail(400,'invalid_request')
    pieces=[part for part in raw.split('/') if part]
    if any(part in ('.','..') for part in pieces):fail(400,'invalid_request')
    return '/'+'/'.join(pieces)


def sensitive_path(path):
    """Exclude known credential stores, not whole Library or arbitrary dot projects."""
    lower=path.casefold();parts=[part for part in lower.split('/') if part]
    if any(part in {'.ssh','.gnupg','.aws','.azure','.kube','.password-store','.pki'} for part in parts):return True
    if not parts:return False
    name=parts[-1]
    if name in {'.env','.npmrc','.pypirc','.netrc','.git-credentials','credentials','credentials.json','credentials.yaml',
                'credentials.yml','secrets.json','secrets.yaml','secrets.yml','secrets.toml','auth.json',
                'tokens.json','token.json','master.passwd','shadow'}:return True
    if name.startswith('.env.') or name.endswith('.env') or name.startswith('id_rsa') or name.startswith('id_ed25519'):return True
    if any(name.endswith(ext) for ext in ('.p8','.pem','.key','.p12','.pfx','.kdbx','.keychain','.keychain-db')):return True
    # Each prefix includes a boundary, so a project named 'Chrome tools' remains visible.
    segments=(
        '/library/keychains','/library/cookies','/library/safari',
        '/library/application support/personalos',
        '/library/application support/google/chrome','/library/application support/chromium',
        '/library/application support/microsoft edge','/library/application support/bravesoftware/brave-browser',
        '/library/application support/firefox','/library/application support/arc',
        '/library/application support/vivaldi','/library/application support/com.operasoftware.opera',
        '/library/containers/com.apple.safari','/library/group containers/group.com.apple.safari',
        '/.mozilla/firefox','/.config/google-chrome','/.config/chromium','/.config/microsoft-edge',
        '/.config/bravesoftware','/.config/gcloud','/.docker/config.json',
        '/.codex/auth.json','/.codex/config.toml','/.openclaw/credentials','/.openclaw/openclaw.json',
        '/.hermes/config.yaml','/.git/config','/private/etc/ssh','/etc/ssh',
        '/private/var/db/dslocal','/private/var/db/shadow',
    )
    for segment in segments:
        start=lower.find(segment)
        if start>=0:
            end=start+len(segment)
            if end==len(lower) or lower[end]=='/':return True
    return False


SECRET_PATTERNS = [
    re.compile(rb'-----BEGIN (?:[A-Z0-9 ]+ )?PRIVATE KEY-----'),
    re.compile(rb'\b(?:AKIA|ASIA)[A-Z0-9]{16}\b'),
    re.compile(rb'\bsk-[A-Za-z0-9_-]{20,}\b'),
    re.compile(rb'\b(?:gh[pousr]_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{30,})\b'),
    re.compile(rb'\bxox[baprs]-[A-Za-z0-9-]{20,}\b'),
    re.compile(rb'\beyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{20,}\b'),
    re.compile(rb'authorization\s*:\s*bearer\s+[A-Za-z0-9._~+/-]{24,}',re.I),
    re.compile(rb'''["']?(?:api[_-]?key|access[_-]?token|refresh[_-]?token|client[_-]?secret|password|passwd)["']?\s*[:=]\s*["'][^\r\n"']{12,}["']''',re.I),
]


def check_content(data):
    if any(pattern.search(data) for pattern in SECRET_PATTERNS):fail(403,'sensitive_content')


def fs_error(error):
    if error.errno in (errno.EACCES,errno.EPERM):fail(403,'permission_denied')
    if error.errno in (errno.ELOOP,):fail(403,'symlink_not_allowed')
    if error.errno==errno.ENOENT:fail(404,'not_found')
    if error.errno==errno.ENOTDIR:fail(400,'not_directory')
    fail(403,'permission_denied')


def snapshot(info):
    return (info.st_dev,info.st_ino,info.st_size,info.st_mtime_ns,info.st_ctime_ns)


def file_etag(path,info):
    return hashlib.sha256(json.dumps([path,*snapshot(info)],separators=(',',':')).encode()).hexdigest()


def integer(value,minimum=0,maximum=2**63-1):
    if isinstance(value,bool) or not isinstance(value,(int,str)):fail(400,'invalid_request')
    try:
        if isinstance(value,str) and not re.fullmatch(r'[0-9]{1,19}',value):fail(400,'invalid_request')
        result=int(value)
    except (TypeError,ValueError,OverflowError):fail(400,'invalid_request')
    if not minimum<=result<=maximum:fail(400,'invalid_range')
    return result


def cursor_decode(raw,operation,path,query):
    if not raw:return None
    if not isinstance(raw,str) or len(raw)>4096:fail(400,'invalid_cursor')
    try:
        data=json.loads(base64.urlsafe_b64decode(raw+'='*((4-len(raw)%4)%4)))
        if (not isinstance(data,dict) or data.get('v')!=1 or data.get('op')!=operation or
            data.get('path')!=path or data.get('query')!=query):raise ValueError()
        data['offset']=integer(data['offset'],maximum=MAX_DIRECTORY_SCAN)
        return data
    except (ValueError,TypeError,KeyError):fail(400,'invalid_cursor')


def cursor_encode(operation,path,query,offset,stamp):
    data={'v':1,'op':operation,'path':path,'query':query,'offset':offset,'stamp':stamp}
    return base64.urlsafe_b64encode(json.dumps(data,separators=(',',':')).encode()).rstrip(b'=').decode()


class LocalFileAccess:
    def __init__(self,home=None):
        self.home=str(pathlib.Path(home).expanduser()) if home else os.path.expanduser('~')

    def roots(self):
        return [{'name':'个人目录','path':self.home},{'name':'磁盘与外置卷','path':'/Volumes'},{'name':'所有位置','path':'/'}]

    def _path(self,path):
        path=normalize_path(path)
        if sensitive_path(path):fail(403,'sensitive_path')
        return path

    def _directory(self,path):
        # Walk each component using directory FDs. No resolve(), symlink traversal or shell.
        fd=None
        flags=os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW|getattr(os,'O_CLOEXEC',0)
        try:
            fd=os.open('/',flags)
            for part in path.split('/'):
                if not part:continue
                next_fd=os.open(part,flags,dir_fd=fd)
                os.close(fd);fd=next_fd
            return fd
        except OSError as error:
            if fd is not None:os.close(fd)
            fs_error(error)

    def _file(self,path):
        if path=='/':fail(400,'not_regular_file')
        parent,name=path.rsplit('/',1);directory=self._directory(parent or '/')
        try:
            fd=os.open(name,os.O_RDONLY|os.O_NOFOLLOW|os.O_NONBLOCK|getattr(os,'O_CLOEXEC',0),dir_fd=directory)
            info=os.fstat(fd)
            if not stat.S_ISREG(info.st_mode):os.close(fd);fail(400,'not_regular_file')
            return fd,info
        except OSError as error:fs_error(error)
        finally:os.close(directory)

    @staticmethod
    def _entry(entry,path):
        full=(path.rstrip('/')+'/'+entry.name)
        if sensitive_path(full) or any(ord(ch)<32 for ch in entry.name):return None
        try:
            info=entry.stat(follow_symlinks=False)
        except OSError:return None
        if stat.S_ISLNK(info.st_mode):return None
        if stat.S_ISDIR(info.st_mode):kind='directory'
        elif stat.S_ISREG(info.st_mode):kind='file'
        else:return None
        return {'name':entry.name,'path':full,'kind':kind,'size':info.st_size if kind=='file' else None,'modified':info.st_mtime}

    def _list(self,path,cursor):
        decoded=cursor_decode(cursor,'list',path,'');offset=decoded['offset'] if decoded else 0
        fd=self._directory(path)
        try:
            info=os.fstat(fd);stamp=[info.st_dev,info.st_ino,info.st_mtime_ns]
            if decoded and decoded.get('stamp')!=stamp:fail(409,'directory_changed')
            entries=[];scanned=0;next_offset=None;deadline=time.monotonic()+SEARCH_SECONDS
            with os.scandir(fd) as iterator:
                for entry in iterator:
                    scanned+=1
                    if scanned>MAX_DIRECTORY_SCAN:fail(413,'directory_limit')
                    if time.monotonic()>deadline:fail(408,'timeout')
                    if scanned<=offset:continue
                    item=self._entry(entry,path)
                    if item is None:continue
                    if len(entries)==PAGE_SIZE:
                        next_offset=scanned-1;break
                    entries.append(item)
            after=os.fstat(fd)
            if [after.st_dev,after.st_ino,after.st_mtime_ns]!=stamp:fail(409,'directory_changed')
            entries.sort(key=lambda item:(item['kind']!='directory',item['name'].casefold()))
            return {'path':path,'parent':str(pathlib.PurePosixPath(path).parent),'entries':entries,
                    'next_cursor':cursor_encode('list',path,'',next_offset,stamp) if next_offset is not None else None,
                    'truncated':next_offset is not None,
                    'note':'只读浏览；符号链接、凭据区域及系统拒绝的条目不显示。实际读取权限以 Mac 系统为准。'}
        except OSError as error:fs_error(error)
        finally:os.close(fd)

    def _search(self,path,query,cursor):
        if not isinstance(query,str) or not 1<=len(query.strip())<=200:fail(400,'invalid_request')
        query=query.strip();decoded=cursor_decode(cursor,'search',path,query);offset=decoded['offset'] if decoded else 0
        fd=self._directory(path)
        try:
            info=os.fstat(fd);stamp=[info.st_dev,info.st_ino,info.st_mtime_ns]
        finally:os.close(fd)
        if decoded and decoded.get('stamp')!=stamp:fail(409,'directory_changed')
        queue=collections.deque([(path,0)]);seen=set();hits=[];scanned=0;limited=False;deadline=time.monotonic()+SEARCH_SECONDS
        while queue and scanned<SEARCH_SCAN_BUDGET and time.monotonic()<deadline:
            directory,depth=queue.popleft()
            try:fd=self._directory(directory)
            except FileAccessError:continue
            try:
                current=os.fstat(fd);identity=(current.st_dev,current.st_ino)
                if identity in seen:continue
                seen.add(identity)
                with os.scandir(fd) as iterator:
                    for entry in iterator:
                        if scanned>=SEARCH_SCAN_BUDGET or time.monotonic()>=deadline:limited=True;break
                        scanned+=1;item=self._entry(entry,directory)
                        if item is None:continue
                        if query.casefold() in item['name'].casefold():hits.append(item)
                        if item['kind']=='directory':
                            if depth<SEARCH_DEPTH:queue.append((item['path'],depth+1))
                            else:limited=True
            except OSError:continue
            finally:os.close(fd)
        if queue:limited=True
        hits.sort(key=lambda item:(item['kind']!='directory',item['name'].casefold(),item['path']))
        selected=hits[offset:offset+PAGE_SIZE];next_offset=offset+len(selected)
        more=next_offset<len(hits)
        return {'path':path,'parent':str(pathlib.PurePosixPath(path).parent),'entries':selected,
                'next_cursor':cursor_encode('search',path,query,next_offset,stamp) if more else None,
                'truncated':limited or more,'scanned':scanned,
                'note':'仅搜索此目录及最多 4 层子目录的文件名；每次最多检查 5000 条或 4 秒。未扫描全部时可进入更具体目录继续。'}

    @staticmethod
    def _read_up_to(fd,size):
        pieces=[];remaining=size
        while remaining>0:
            data=os.read(fd,min(65536,remaining))
            if not data:break
            pieces.append(data);remaining-=len(data)
        return b''.join(pieces)

    def _read(self,path):
        fd,info=self._file(path)
        try:
            data=self._read_up_to(fd,PREVIEW_BYTES+1)
            if snapshot(os.fstat(fd))!=snapshot(info):fail(409,'file_changed')
            check_content(data)
            mime=mimetypes.guess_type(path)[0] or 'application/octet-stream'
            visible=data[:PREVIEW_BYTES]
            try:
                if b'\x00' in visible or mime.startswith(('image/','audio/','video/')) or mime in ('application/pdf','application/zip'):raise UnicodeError()
                # Incremental decoder allows a multibyte character cut at the preview boundary.
                import codecs
                text=codecs.getincrementaldecoder('utf-8')().decode(visible,final=len(data)<=PREVIEW_BYTES)
                previewable=True
            except UnicodeError:
                text='此文件不是可安全显示的 UTF-8 文本。可选择只读下载原文件。';previewable=False
            return {'name':pathlib.PurePosixPath(path).name,'path':path,'mime_type':mime,'encoding':'utf-8',
                    'content':text,'size':info.st_size,'modified':info.st_mtime,'truncated':info.st_size>PREVIEW_BYTES,
                    'limit_bytes':PREVIEW_BYTES,'previewable':previewable,'download_allowed':True,'etag':file_etag(path,info)}
        except OSError as error:fs_error(error)
        finally:os.close(fd)

    def _download(self,path,offset,max_bytes,etag):
        offset=integer(offset);max_bytes=integer(max_bytes,minimum=1,maximum=CHUNK_BYTES)
        if not isinstance(etag,str) or (etag and not re.fullmatch(r'[a-f0-9]{64}',etag)):fail(400,'invalid_request')
        if offset>0 and not etag:fail(400,'invalid_request')
        fd,info=self._file(path)
        try:
            current=file_etag(path,info)
            if etag and current!=etag:fail(409,'file_changed')
            if offset>info.st_size:fail(416,'invalid_range')
            # Bounded lookaround also detects recognizable credentials straddling chunk boundaries.
            start=max(0,offset-8192);os.lseek(fd,start,os.SEEK_SET)
            window=self._read_up_to(fd,(offset-start)+max_bytes+8192)
            if snapshot(os.fstat(fd))!=snapshot(info):fail(409,'file_changed')
            check_content(window)
            data=window[offset-start:offset-start+max_bytes];next_offset=offset+len(data)
            return {'name':pathlib.PurePosixPath(path).name,'path':path,'mime_type':mimetypes.guess_type(path)[0] or 'application/octet-stream',
                    'encoding':'base64','content_base64':base64.b64encode(data).decode('ascii'),'size':info.st_size,
                    'modified':info.st_mtime,'offset':offset,'next_offset':next_offset,'eof':next_offset>=info.st_size,
                    'etag':current,'limit_bytes':CHUNK_BYTES}
        except OSError as error:fs_error(error)
        finally:os.close(fd)

    def handle(self,operation,path,query='',cursor='',offset=0,max_bytes=CHUNK_BYTES,etag=''):
        path=self._path(path)
        if operation=='list':return self._list(path,cursor)
        if operation=='search':return self._search(path,query,cursor)
        if operation=='read':return self._read(path)
        if operation=='download':return self._download(path,offset,max_bytes,etag)
        fail(400,'unsupported_operation')


def _stop(process):
    try:os.killpg(process.pid,signal.SIGTERM)
    except ProcessLookupError:pass
    try:process.wait(timeout=1)
    except subprocess.TimeoutExpired:
        try:os.killpg(process.pid,signal.SIGKILL)
        except ProcessLookupError:pass
        process.wait(timeout=1)


def ssh_exchange(argv,payload,timeout=20,max_output=RPC_BYTES):
    encoded=(json.dumps(payload,ensure_ascii=False,separators=(',',':'))+'\n').encode()
    if len(encoded)>16384:fail(400,'invalid_request')
    process=None;selector=selectors.DefaultSelector();output=bytearray();sent=0
    try:
        process=subprocess.Popen(argv,stdin=subprocess.PIPE,stdout=subprocess.PIPE,stderr=subprocess.PIPE,start_new_session=True)
        for stream,role,event in ((process.stdin,'input',selectors.EVENT_WRITE),(process.stdout,'output',selectors.EVENT_READ),(process.stderr,'error',selectors.EVENT_READ)):
            os.set_blocking(stream.fileno(),False);selector.register(stream,event,role)
        deadline=time.monotonic()+timeout
        while selector.get_map():
            remaining=deadline-time.monotonic()
            if remaining<=0:fail(408,'timeout')
            for key,_ in selector.select(min(.25,remaining)):
                try:
                    if key.data=='input':
                        sent+=os.write(key.fileobj.fileno(),encoded[sent:])
                        if sent==len(encoded):selector.unregister(key.fileobj);key.fileobj.close()
                    else:
                        data=os.read(key.fileobj.fileno(),65536)
                        if not data:selector.unregister(key.fileobj);key.fileobj.close();continue
                        if key.data=='output':
                            if len(output)+len(data)>max_output:fail(502,'invalid_response')
                            output.extend(data)
                except BlockingIOError:continue
        code=process.wait(timeout=max(.01,deadline-time.monotonic()))
        if code:fail(503,'unavailable')
        return bytes(output)
    except FileAccessError:
        if process is not None:_stop(process)
        raise
    except (OSError,subprocess.SubprocessError):
        if process is not None:_stop(process)
        fail(503,'unavailable')
    finally:
        selector.close()
        if process is not None:
            for stream in (process.stdin,process.stdout,process.stderr):
                if stream is not None:stream.close()


class RemoteFileAccess:
    def __init__(self,config_path,runner=None):
        self.config_path=pathlib.Path(config_path);self.runner=runner or ssh_exchange
    def _config(self):
        try:
            p=self.config_path
            if not p.is_file() or p.is_symlink() or p.stat().st_size>8192 or stat.S_IMODE(p.stat().st_mode)&0o077:return None
            value=json.loads(p.read_text())
            from deployment_config import valid_remote
            if not valid_remote(value,'remote_home') or value.get('enabled') is not True:return None
            for field in ('key_path','known_hosts_path'):
                raw=value.get(field)
                if not isinstance(raw,str) or any(ord(ch)<32 for ch in raw):return None
                path=pathlib.Path(raw).expanduser()
                if not path.is_absolute() or not path.is_file() or path.is_symlink():return None
                if field=='key_path' and stat.S_IMODE(path.stat().st_mode)&0o077:return None
                value[field]=str(path)
            return value
        except (ValueError,TypeError,OSError):return None
    def available(self):return self._config() is not None
    def handle(self,operation,path,query='',cursor='',offset=0,max_bytes=CHUNK_BYTES,etag=''):
        path=normalize_path(path)
        if sensitive_path(path):fail(403,'sensitive_path')
        if operation not in ('list','search','read','download'):fail(400,'unsupported_operation')
        config=self._config()
        if not config:fail(503,'unavailable')
        known=config['known_hosts_path'].replace('\\','\\\\').replace('"','\\"')
        argv=['/usr/bin/ssh','-F','/dev/null','-T','-a','-x','-o','BatchMode=yes','-o','IdentitiesOnly=yes',
              '-o','IdentityAgent=none','-o','StrictHostKeyChecking=yes','-o','UserKnownHostsFile="'+known+'"',
              '-o','GlobalKnownHostsFile=/dev/null','-o','ClearAllForwardings=yes','-o','PermitLocalCommand=no',
              '-o','ConnectTimeout=8','-o','ConnectionAttempts=1','-o','LogLevel=ERROR',
              '-o','ProxyCommand=/Applications/Tailscale.app/Contents/MacOS/Tailscale nc %h %p',
              '-i',config['key_path'],'-p','22',config['user']+'@'+config['host']]
        request={'version':1,'operation':operation,'path':path,'query':query,'cursor':cursor,
                 'offset':integer(offset),'max_bytes':integer(max_bytes,minimum=1,maximum=CHUNK_BYTES),'etag':etag}
        try:
            raw=self.runner(argv,request,timeout=20,max_output=RPC_BYTES)
            if not isinstance(raw,bytes) or len(raw)>RPC_BYTES:fail(502,'invalid_response')
            result=json.loads(raw.decode('utf-8'))
            if not isinstance(result,dict) or result.get('version')!=1 or not isinstance(result.get('ok'),bool):fail(502,'invalid_response')
            if not result['ok']:
                code=result.get('code');status=result.get('status')
                if code not in PUBLIC_ERRORS or status not in (400,403,404,408,409,413,416,500,502,503):fail(502,'invalid_response')
                fail(status,code)
            body=result.get('result')
            if not isinstance(body,dict) or body.get('path')!=path:fail(502,'invalid_response')
            if operation in ('list','search'):
                if not isinstance(body.get('entries'),list) or len(body['entries'])>PAGE_SIZE:fail(502,'invalid_response')
                for item in body['entries']:
                    if not isinstance(item,dict) or item.get('kind') not in ('file','directory'):fail(502,'invalid_response')
                    entry_path=normalize_path(item.get('path'))
                    if sensitive_path(entry_path) or not entry_path.startswith(path.rstrip('/')+'/'):fail(502,'invalid_response')
            elif operation=='download':
                encoded=body.get('content_base64')
                if not isinstance(encoded,str) or len(encoded)>CHUNK_BYTES*4//3+8:fail(502,'invalid_response')
                data=base64.b64decode(encoded,validate=True);check_content(data)
                if body.get('offset')!=request['offset'] or body.get('next_offset')!=request['offset']+len(data):fail(502,'invalid_response')
                if request['etag'] and body.get('etag')!=request['etag']:fail(409,'file_changed')
            elif operation=='read':
                if body.get('encoding')!='utf-8' or not isinstance(body.get('content'),str) or len(body['content'].encode())>PREVIEW_BYTES:fail(502,'invalid_response')
                check_content(body['content'].encode())
            return body
        except FileAccessError:raise
        except (ValueError,TypeError,UnicodeError,KeyError):fail(502,'invalid_response')


class FileAccessService:
    def __init__(self,config_home,local=None,remote=None):
        self.config_home=pathlib.Path(config_home)
        self.local=local or LocalFileAccess()
        self.remote=remote or RemoteFileAccess(pathlib.Path(config_home)/'air-files-source.json')
    def mini_enabled(self):
        try:
            path=self.config_home/'files-access.json'
            if not path.is_file() or path.is_symlink() or path.stat().st_size>4096 or stat.S_IMODE(path.stat().st_mode)&0o077:return False
            data=json.loads(path.read_text())
            return isinstance(data,dict) and data.get('enabled') is True
        except (OSError,ValueError):return False
    def devices(self):
        note='仅按 Mac 系统现有权限只读访问。凭据、私钥和浏览器会话区域排除；系统受保护目录可能需要你授权完整磁盘访问。'
        return {'devices':[{'id':'mini','name':'Mac mini','available':self.mini_enabled(),'roots':self.local.roots(),'permission_note':note},
                           {'id':'air','name':'MacBook Air','available':self.remote.available(),
                            'roots':[{'name':'个人目录','path':(self.remote._config() or {}).get('remote_home',AIR_HOME)},{'name':'磁盘与外置卷','path':'/Volumes'},{'name':'所有位置','path':'/'}],
                            'permission_note':note+' 需要先完成专用只读连接；在线状态在实际访问时检查。'}]}
    def handle(self,operation,body):
        if not isinstance(body,dict) or set(body)-{'device','path','query','cursor','offset','max_bytes','etag'}:fail(400,'invalid_request')
        if any(not isinstance(value,str) for value in body.values()):fail(400,'invalid_request')
        device=body.get('device')
        if device=='mini' and not self.mini_enabled():fail(403,'access_not_enabled')
        source=self.local if device=='mini' else self.remote if device=='air' else None
        if source is None:fail(400,'invalid_request')
        result=source.handle(operation,body.get('path',''),body.get('query',''),body.get('cursor',''),
                             body.get('offset','0'),body.get('max_bytes',str(CHUNK_BYTES)),body.get('etag',''))
        return {'device':device,**result}
