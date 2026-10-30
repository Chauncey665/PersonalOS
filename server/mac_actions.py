"""Explicit, owner-scoped Mac requests. No shell or arbitrary AppleScript."""
import ctypes,sys,errno
import base64,datetime as dt,difflib,fcntl,hashlib,json,os,pathlib,re,stat,subprocess,time,uuid,urllib.parse
from todos import TodoError
from file_access import LocalFileAccess,normalize_path,sensitive_path,check_content,snapshot,file_etag
from documents import MAX_FILE,unpack
APPS={'finder':'Finder','safari':'Safari','textedit':'TextEdit','notes':'Notes','calendar':'Calendar','preview':'Preview'}
OPEN_TYPES={'.txt','.md','.docx','.pdf','.rtf','.png','.jpg','.jpeg','.heic','.csv','.xlsx','.pptx'}
TRANSFER_TYPES=OPEN_TYPES|{'.csv','.xlsx','.pptx'}
def digest(data):return hashlib.sha256(data).hexdigest()
def move_exclusive(directory,source,target):
 if sys.platform=='darwin':
  library=ctypes.CDLL('/usr/lib/libSystem.B.dylib',use_errno=True);move=library.renameatx_np
  move.argtypes=[ctypes.c_int,ctypes.c_char_p,ctypes.c_int,ctypes.c_char_p,ctypes.c_uint32];move.restype=ctypes.c_int
  if move(directory,os.fsencode(source),directory,os.fsencode(target),4)!=0:
   code=ctypes.get_errno()
   if code==errno.EEXIST:raise FileExistsError(code,'Target exists')
   raise OSError(code,'Could not save received file')
 else:
  os.link(source,target,src_dir_fd=directory,dst_dir_fd=directory,follow_symlinks=False);os.unlink(source,dir_fd=directory)

def preserve_metadata(source_fd,target_fd):
 # Keep Finder tags, quarantine and access-control metadata on atomic writes.
 # The Darwin C API works on already-verified descriptors, not mutable paths.
 if sys.platform!='darwin':return
 library=ctypes.CDLL('/usr/lib/libSystem.B.dylib',use_errno=True)
 copy=library.fcopyfile;copy.argtypes=[ctypes.c_int,ctypes.c_int,ctypes.c_void_p,ctypes.c_uint32];copy.restype=ctypes.c_int
 if copy(source_fd,target_fd,None,5)!=0:raise OSError(ctypes.get_errno(),'Could not preserve file security metadata')

def clean_name(name):
 if not isinstance(name,str) or not 1<=len(name)<=160 or name in ('.','..') or any(c in name for c in ('/','\\')) or any(ord(c)<32 for c in name) or sensitive_path('/'+name):raise TodoError('文件名不在允许范围')
 return name

class MacActions:
 def __init__(self,store,runner=None,inbox=None):
  self.s=store;self.runner=runner;self.inbox=pathlib.Path(inbox or pathlib.Path.home()/'Documents/PersonalOS Inbox')
 @staticmethod
 def initialize(c):
  c.executescript('''CREATE TABLE IF NOT EXISTS document_sources(document_id TEXT PRIMARY KEY,owner TEXT NOT NULL,device TEXT NOT NULL,path TEXT NOT NULL,etag TEXT NOT NULL,sha TEXT NOT NULL,mode INTEGER NOT NULL);
  CREATE TABLE IF NOT EXISTS mac_receipts(task TEXT PRIMARY KEY,owner TEXT NOT NULL,state TEXT NOT NULL,payload TEXT NOT NULL,updated REAL NOT NULL);
  CREATE TABLE IF NOT EXISTS transfer_requests(owner TEXT NOT NULL,request_id TEXT NOT NULL,digest TEXT NOT NULL,task TEXT NOT NULL,PRIMARY KEY(owner,request_id));''')
 def status(self):
  helper=self.s.home/'runtime/personalos-mac-actions'
  return {'device':'mini','applications':APPS,'helper_available':bool(self.runner or (helper.is_file() and not helper.is_symlink() and os.access(helper,os.X_OK))), 'inbox':str(self.inbox),'file_writeback':'selected Mini txt/md/docx only; preview, explicit confirmation, original backup and guarded undo','air':'read-only'}
 def _file(self,path):
  if not self.s.file_access.mini_enabled():raise TodoError('请先在 Mini 启用文件访问')
  local=self.s.file_access.local;path=local._path(path);fd,info=local._file(path)
  if pathlib.Path(path).suffix.lower() not in OPEN_TYPES:os.close(fd);raise TodoError('只打开普通文档和图片，不执行脚本或程序')
  return path,fd,info
 def import_file(self,owner,body):
  if set(body)!={'device','path','etag','request_id'} or body['device'] not in ('mini','air'):raise TodoError('请选择具体文件')
  path=normalize_path(body['path']);ext=pathlib.Path(path).suffix.lower()
  if ext not in ('.txt','.md','.docx'):raise TodoError('可编辑格式为.txt、.md、.docx')
  data=bytearray();etag=body['etag'];mode=0
  for _ in range(21):
   value=self.s.file_access.handle('download',{'device':body['device'],'path':path,'offset':str(len(data)),'etag':etag})
   if value['size']>MAX_FILE:raise TodoError('编辑文档最多5MB')
   etag=value['etag'];data.extend(base64.b64decode(value['content_base64'],validate=True))
   if value['eof']:break
  else:raise TodoError('文件读取未完成')
  if len(data)!=value['size']:raise TodoError('文件读取不完整')
  if body['device']=='mini':
   p,fd,info=self._file(path)
   try:
    if file_etag(path,info)!=etag:raise TodoError('文件已变化，请重新选择',409)
    mode=stat.S_IMODE(info.st_mode)
   finally:os.close(fd)
  doc=self.s.documents.upload(owner,{'name':clean_name(pathlib.Path(path).name),'data':base64.b64encode(data).decode(),'request_id':body['request_id']})
  with self.s.db() as c:c.execute('INSERT OR IGNORE INTO document_sources VALUES(?,?,?,?,?,?,?)',(doc['id'],owner,body['device'],path,etag,digest(data),mode))
  return self.document_info(owner,doc['id'])
 def document_info(self,owner,ident):
  doc=self.s.documents.info(owner,ident)
  with self.s.db() as c:r=c.execute('SELECT device,path FROM document_sources WHERE document_id=? AND owner=?',(ident,owner)).fetchone()
  return {**doc,'source_device':r['device'] if r else None,'source_path':r['path'] if r else None}
 def _version(self,owner,ident,version):
  with self.s.db() as c:
   self.s.documents._owned(c,owner,ident);r=c.execute('SELECT * FROM document_versions WHERE document_id=? AND version=?',(ident,version)).fetchone()
   if not r:raise TodoError('文档版本不存在',404)
   return self.s.documents._bytes(r)
 def _submit(self,owner,text,action):
  ident=str(uuid.uuid4());body={'text':text,'mac_action':json.dumps(action,ensure_ascii=False)}
  outcome=self.prepare({'id':ident,'owner':owner},action)
  # Explicit UI requests are inserted directly as previews. They never enter
  # the worker queue, avoiding a late worker overwriting an approved receipt.
  with self.s.db() as c:
   c.execute('INSERT INTO tasks(id,body,state,result,plan,created,due,owner) VALUES(?,?,?,?,?,?,NULL,?)',(ident,json.dumps(body,ensure_ascii=False),outcome['state'],outcome['result'],outcome['plan'],time.time(),owner))
   self.s._audit(c,ident,'mac_action_preview',{'operation':action['operation']});self.s._notify(c,ident,'awaiting_confirmation')
  return {'id':ident}
 def writeback(self,owner,ident,version):
  doc=self.document_info(owner,ident)
  if type(version) is not int or version!=doc['version'] or version<=1:raise TodoError('请选择当前修改版')
  with self.s.db() as c:r=c.execute('SELECT * FROM document_sources WHERE document_id=? AND owner=?',(ident,owner)).fetchone()
  if not r or r['device']!='mini':raise TodoError('只有从 Mini 文件页选中的原件支持写回；Air 保持只读')
  old=self._version(owner,ident,1);new=self._version(owner,ident,version)
  if digest(old)!=r['sha']:raise TodoError('原件备份校验失败')
  return self._submit(owner,'写回 Mac 原文件：'+doc['name'],{'operation':'write_document','document_id':ident,'version':version})
 def validate(self,owner,action):
  if not isinstance(action,dict):raise TodoError('Mac动作格式无效')
  op=action.get('operation')
  if op=='launch_app' and set(action)=={'operation','app'} and action['app'] in APPS:return dict(action)
  if op=='open_url' and set(action)=={'operation','url'}:
   url=action['url']
   if not isinstance(url,str) or len(url)>2000 or any(ord(c)<33 for c in url):raise TodoError('网页地址无效')
   p=urllib.parse.urlsplit(url)
   if p.scheme!='https' or not p.hostname or p.username or p.password or p.port or p.fragment or not re.fullmatch(r'[a-zA-Z0-9.-]+',p.hostname) or '.' not in p.hostname or re.fullmatch(r'[\d.]+',p.hostname) or p.hostname.endswith(('.local','.localhost','.ts.net')):raise TodoError('只打开明确的公开HTTPS网页')
   if any(re.search(r'token|secret|password|authorization|credential|key',k,re.I) for k in urllib.parse.parse_qs(p.query)):raise TodoError('网页地址含私人凭据参数')
   check_content(url.encode());return dict(action)
  if op in ('open_file','reveal_file') and set(action)=={'operation','path'}:
   path,fd,info=self._file(action['path']);os.close(fd);return {'operation':op,'path':path,'etag':file_etag(path,info)}
  if op=='write_document' and set(action)=={'operation','document_id','version'}:
   doc=self.document_info(owner,action['document_id'])
   if type(action['version']) is not int or doc['version']!=action['version'] or action['version']<=1:raise TodoError('修改版已改变，请重新生成写回清单',409)
   with self.s.db() as c:src=c.execute('SELECT * FROM document_sources WHERE document_id=? AND owner=?',(doc['id'],owner)).fetchone()
   if not src or src['device']!='mini':raise TodoError('此文档没有可写回的 Mini 原件')
   path,fd,info=self._file(src['path'])
   try:
    if info.st_uid!=os.getuid() or info.st_nlink!=1 or file_etag(path,info)!=src['etag']:raise TodoError('原文件已变化、是硬链接或不属于当前用户，未写回',409)
    raw=os.read(fd,MAX_FILE+1)
    if digest(raw)!=src['sha'] or snapshot(os.fstat(fd))!=snapshot(info):raise TodoError('原文件已变化，未写回',409)
   finally:os.close(fd)
   old=self._version(owner,doc['id'],1);new=self._version(owner,doc['id'],action['version']);ext=pathlib.Path(path).suffix.lower()
   oldtext=unpack(old,ext)[1];newtext=unpack(new,ext)[1]
   preview='\n'.join(list(difflib.unified_diff(oldtext,newtext,fromfile='原版',tofile='修改版',lineterm=''))[:100])[:10000]
   return {**action,'path':path,'etag':src['etag'],'original_sha256':src['sha'],'new_sha256':digest(new),'mode':stat.S_IMODE(info.st_mode),'preview':preview}
  raise TodoError('Mac动作不在允许范围')
 def prepare(self,row,action):
  with self.s.db() as c:prior=c.execute('SELECT state FROM mac_receipts WHERE task=?',(row['id'],)).fetchone()
  if prior:return {'state':'needs_review','result':'执行曾中断，请检查执行记录；不会自动重放动作。'}
  clean=self.validate(row['owner'],action)
  if clean['operation']!='write_document' and not self.status()['helper_available']:raise TodoError('Mac应用执行组件尚未安装')
  title={'launch_app':'打开 '+APPS.get(clean.get('app'),''),'open_url':'用 Safari 打开网页','open_file':'在 Mac 打开文件','reveal_file':'在 Finder 显示文件','write_document':'写回 Mac 原文件'}[clean['operation']]
  return {'state':'awaiting_confirmation','result':'请确认这次 Mac 操作。','plan':json.dumps({'kind':'mac_action','title':title,'action':clean,'expires':time.time()+3600,'undo_available':clean['operation']=='write_document'},ensure_ascii=False)}
 def _replace(self,action,data,expected_sha,expected_etag):
  path,fd,info=self._file(action['path']);parent,name=path.rsplit('/',1);directory=self.s.file_access.local._directory(parent or '/');temp='.personalos-'+uuid.uuid4().hex
  try:
   fcntl.flock(fd,fcntl.LOCK_EX|fcntl.LOCK_NB)
   if info.st_uid!=os.getuid() or info.st_nlink!=1 or file_etag(path,info)!=expected_etag or digest(os.read(fd,MAX_FILE+1))!=expected_sha:raise TodoError('原文件有新的修改，已停止；没有覆盖',409)
   tmp=os.open(temp,os.O_WRONLY|os.O_CREAT|os.O_EXCL|os.O_NOFOLLOW,0o600,dir_fd=directory)
   with os.fdopen(tmp,'wb') as f:f.write(data);f.flush();preserve_metadata(fd,f.fileno());os.fchmod(f.fileno(),action['mode']&0o777);os.fsync(f.fileno())
   # Atomic replacement in the already-open, non-symlink parent. Recheck the
   # actual inode and metadata immediately before rename; locked writers refuse.
   current=os.stat(name,dir_fd=directory,follow_symlinks=False)
   if snapshot(current)!=snapshot(info):raise TodoError('原文件在写入前改变，已停止',409)
   os.replace(temp,name,src_dir_fd=directory,dst_dir_fd=directory);os.fsync(directory)
   check=os.open(name,os.O_RDONLY|os.O_NOFOLLOW,dir_fd=directory)
   try:
    actual=os.fstat(check)
    if not stat.S_ISREG(actual.st_mode) or digest(os.read(check,MAX_FILE+1))!=digest(data) or snapshot(os.fstat(check))!=snapshot(actual):raise TodoError('写入后的状态无法确认，请检查原文件；备份仍保留',409)
    return file_etag(path,actual)
   finally:os.close(check)
  finally:
   try:os.unlink(temp,dir_fd=directory)
   except FileNotFoundError:pass
   os.close(fd);os.close(directory)
 def act(self,ident,operation):
  with self.s.db() as c:
   c.execute('BEGIN IMMEDIATE');row=c.execute('SELECT * FROM tasks WHERE id=?',(ident,)).fetchone();plan=json.loads(row['plan']);action=plan['action']
   if row['state']!=('awaiting_confirmation' if operation=='approve' else 'completed'):raise TodoError('此动作已处理或状态已变化',409)
   if operation=='undo' and not plan['undo_available']:raise TodoError('打开应用/文件不可自动撤销，不会关闭你的应用')
   if operation=='approve' and plan['expires']<time.time():raise TodoError('确认清单已过期，请重新提交',409)
   if operation=='approve':c.execute('INSERT INTO mac_receipts VALUES(?,?,?, ?,?)',(ident,row['owner'],'applying',json.dumps(plan),time.time()))
   else:
    prior_receipt=json.loads(c.execute('SELECT payload FROM mac_receipts WHERE task=?',(ident,)).fetchone()[0])['receipt']
    c.execute("UPDATE mac_receipts SET state='undoing',updated=? WHERE task=?",(time.time(),ident))
   c.execute("UPDATE tasks SET state='running' WHERE id=?",(ident,));self.s._audit(c,ident,'mac_'+operation,{'operation':action['operation']})
  try:
   if action['operation']=='write_document':
    current=self.s.documents.info(row['owner'],action['document_id'])
    if operation=='approve' and current['version']!=action['version']:raise TodoError('文档修改版已改变，请重新检查',409)
    version=1 if operation=='undo' else action['version'];data=self._version(row['owner'],action['document_id'],version)
    expected=action['new_sha256'] if operation=='undo' else action['original_sha256'];etag=prior_receipt['etag'] if operation=='undo' else action['etag'];after_etag=self._replace(action,data,expected,etag)
    receipt={'operation':'write_document','sha256':digest(data),'etag':after_etag,'original_backup_preserved':True}
    result='原文件已恢复。' if operation=='undo' else 'Mac原文件已写回，原版备份保留，可撤销。'
   else:
    if action['operation'] in ('open_file','reveal_file'):
     p,fd,info=self._file(action['path']);os.close(fd)
     if file_etag(p,info)!=action['etag']:raise TodoError('文件已变化，请重新确认',409)
    request={k:v for k,v in action.items() if k in ('operation','app','url','path')}
    if self.runner:receipt=self.runner(request)
    else:
     done=subprocess.run([str(self.s.home/'runtime/personalos-mac-actions')],input=json.dumps(request).encode(),capture_output=True,timeout=20,check=False)
     if done.returncode or len(done.stdout)>8192:raise TodoError('Mac未确认打开成功，请在电脑检查；不会自动重试')
     receipt=json.loads(done.stdout)
    if receipt.get('accepted') is not True:raise TodoError('Mac未确认打开成功')
    result='Mac已接受打开请求。应用内的后续操作尚未执行。'
   with self.s.db() as c:
    c.execute('UPDATE mac_receipts SET state=?,payload=?,updated=? WHERE task=?',('undone' if operation=='undo' else 'completed',json.dumps({'plan':plan,'receipt':receipt}),time.time(),ident));self.s._audit(c,ident,'mac_execution_receipt',receipt)
   self.s.update(ident,state='undone' if operation=='undo' else 'completed',result=result)
  except Exception as error:
   self.s.update(ident,state='needs_review',result=str(error));self.s.audit(ident,'mac_action_failed',{'operation':action['operation'],'error':str(error)});raise
 def transfer(self,owner,body):
  if set(body)!={'name','data','request_id'}:raise TodoError('传输格式无效')
  name=clean_name(body['name']);ext=pathlib.Path(name).suffix.lower();request=body['request_id']
  if ext not in TRANSFER_TYPES or not isinstance(request,str) or not re.fullmatch(r'[A-Za-z0-9_-]{1,128}',request) or not isinstance(body['data'],str) or len(body['data'])>28*1024*1024:raise TodoError('支持普通文档和图片，单文件最多20MB')
  try:data=base64.b64decode(body['data'],validate=True)
  except ValueError:raise TodoError('文件编码无效') from None
  if not 0<len(data)<=20*1024*1024:raise TodoError('文件须为1字节到20MB')
  check_content(data);sha=digest(name.encode()+data)
  with self.s.db() as c:
   c.execute('BEGIN IMMEDIATE');old=c.execute('SELECT * FROM transfer_requests WHERE owner=? AND request_id=?',(owner,request)).fetchone()
   if old:
    if old['digest']!=sha:raise TodoError('重复编号对应不同文件',409)
    return {'id':old['task'],'already_received':True}
   # Fixed destination, never a client-supplied path or a linked directory.
   parent=self.s.file_access.local._directory(str(self.inbox.parent))
   try:
    try:os.mkdir(self.inbox.name,0o700,dir_fd=parent)
    except FileExistsError:pass
    folder=os.open(self.inbox.name,os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW,dir_fd=parent)
   finally:os.close(parent)
   suffix=pathlib.Path(name).suffix;stem=name[:-len(suffix)]
   while len((stem+suffix).encode())>180:stem=stem[:-1]
   short=stem+suffix
   filename=digest((owner+'|'+request+'|'+name).encode())[:32]+'-'+short
   temporary='.personalos-incoming-'+uuid.uuid4().hex
   try:
    descriptor=os.open(temporary,os.O_WRONLY|os.O_CREAT|os.O_EXCL|os.O_NOFOLLOW,0o600,dir_fd=folder)
    with os.fdopen(descriptor,'wb') as f:f.write(data);f.flush();os.fsync(f.fileno())
    try:move_exclusive(folder,temporary,filename)
    except FileExistsError:
     existing=os.open(filename,os.O_RDONLY|os.O_NOFOLLOW|os.O_NONBLOCK,dir_fd=folder)
     try:
      info=os.fstat(existing)
      with os.fdopen(os.dup(existing),'rb') as stream:received=stream.read(20*1024*1024+1)
      if not stat.S_ISREG(info.st_mode) or info.st_nlink!=1 or info.st_size!=len(data) or digest(received)!=digest(data):raise TodoError('接收位置已有不同内容，未覆盖；请重新选择文件',409)
     finally:os.close(existing)
    os.fsync(folder)
   finally:
    try:os.unlink(temporary,dir_fd=folder)
    except FileNotFoundError:pass
    os.close(folder)
   ident=str(uuid.uuid4());receipt={'kind':'file_transfer','name':name,'path':str(self.inbox/filename),'bytes':len(data),'sha256':digest(data)}
   c.execute('INSERT INTO tasks(id,body,state,result,plan,created,due,owner) VALUES(?,?,?,?,?,?,NULL,?)',(ident,json.dumps({'text':'传送文档到 Mac：'+name,'mac_action':'transfer'}),'completed','已保存到 Mac 的 PersonalOS Inbox。',json.dumps(receipt),time.time(),owner));c.execute('INSERT INTO transfer_requests VALUES(?,?,?,?)',(owner,request,sha,ident));self.s._audit(c,ident,'file_received_on_mac',{'bytes':len(data),'sha256':digest(data)});self.s._notify(c,ident,'completed')
  return {'id':ident,'already_received':False,'receipt':receipt}

def spoken_mac_action(text):
 """Only this user's explicit Mac opening request, never content of a document."""
 if not re.search(r'Mac|Mini|电脑',text,re.I) or not re.search(r'打开|启动|显示|\bopen\b|\blaunch\b',text,re.I) or re.search(r'不要|别|不用|不需要|\b(?:not|never|don.t)\b',text,re.I):return None
 url=re.search(r'https://[^\s，,。；;”"<>]+',text)
 if url:return {'operation':'open_url','url':url.group()}
 path=re.search(r'[“"](/[^”"\n]+)[”"]|(/[^\s，,。；;]+)',text)
 if path:return {'operation':'reveal_file' if re.search(r'显示|找到位置|reveal',text,re.I) else 'open_file','path':path.group(1) or path.group(2)}
 for key,alias in [('textedit',r'TextEdit|文本编辑'),('notes',r'备忘录|Notes'),('calendar',r'日历|Calendar'),('preview',r'预览|Preview'),('safari',r'Safari'),('finder',r'访达|Finder')]:
  if re.search(alias,text,re.I):return {'operation':'launch_app','app':key}
 return {'operation':'unsupported'}

def phone_request_text(text):
 # Repeating a request is emphasis, not an address. This local parsing copy
 # never replaces the user's saved transcript or recording.
 parts=[p.strip() for p in re.split(r'[，,。！？!?；;\n]+',text) if p.strip()]
 if len(parts)>1 and len(set(parts))==1:return parts[0]
 stripped=text.strip(' ，,。.!！?？')
 for count in (2,3,4):
  size=len(stripped)//count
  if size>=4 and stripped[:size]*count==stripped:return stripped[:size]
 return text

def restaurant_search(text):
 clean=re.sub(r'^\s*(?:(?:请问|请|麻烦|帮我|给我|重新|再|看看|看下|找找|查找|找|搜索|搜|推荐|一下|我想|我要|想要|我|要|在)\s*)+','',text).strip(' ，。.!！?？')
 marker=re.search(r'附近|周围|周边|nearby|near me',clean,re.I)
 # A locationless restaurant query always requires current location. Only
 # an explicit area anchor permits an unbounded textual location search.
 relative={'','这','这里','我','我的','我现在','当前位置','现在位置'}
 anchor=clean[:marker.start()].strip(' 的') if marker else ''
 explicit=bool(marker and anchor not in relative and re.search(r'(?:大学|学院|学校|广场|中心|机场|车站|码头|酒店|公园|HKUST|[街路市县镇区村港站湾岛山楼园院校厦寺庙城咀])$',anchor,re.I))
 area=re.search(r'(?:^|[，,])(?:请)?(?:帮我)?在([^，,。！？?!]{2,40}?)(?:找|搜|推荐)',text)
 if area:explicit=True;clean=area[1]+' 餐厅'
 if not marker and re.search(r'\brestaurants?\s+(?:in|at)\s+[A-Za-z]',text,re.I):explicit=True
 query=(clean[marker.end():] if marker else clean) if not explicit else clean
 query=re.sub(r'^(?:(?:有没有|有什么|有啥|这里|我的|的)\s*)+','',query).strip() if not explicit else query
 return {'section':'找餐厅','query':query[:200] or '餐厅','nearby':not explicit}

def spoken_phone_action(text):
 if re.search(r'不要|别|不用|不需要',text):return None
 text=phone_request_text(text)
 if re.fullmatch(r'(?:请问|请|帮我看看|看看)?(?:我|这)?(?:附近|周围|周边)(?:有什么地方|有(?:什么|啥)|哪里|哪儿|可以|能)?(?:可以|能|好)?吃(?:的|饭|什么)?(?:地方|餐厅)?[？?。!！ ]*',text):
  return {'section':'找餐厅','query':'餐厅','nearby':True}
 if re.search(r'导航(?:到|去|回)|带我去|(?:navigate|directions)\s+to\b',text,re.I):
  query=re.split(r'导航(?:到|去|回)|带我去|(?:navigate|directions)\s+to\s*',text,maxsplit=1,flags=re.I)[-1].strip(' ，。.!！?？')
  return {'section':'导航','query':query}
 if re.search(r'餐厅|饭店|吃什么|restaurants?|food near me',text,re.I) and re.search(r'找|搜|推荐|附近|search|find|near',text,re.I) and not re.search(r'安排|提醒|记录|完成|会议|改写|翻译|文档',text):
  return restaurant_search(text)
 return None
