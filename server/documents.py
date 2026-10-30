"""Immutable, owner-scoped document revisions. Never overwrites imported files."""
import base64,hashlib,io,json,os,pathlib,re,time,uuid,zipfile
from xml.dom import minidom
from xml.parsers.expat import ExpatError
from file_access import check_content
from todos import TodoError
W='http://schemas.openxmlformats.org/wordprocessingml/2006/main'
MAX_FILE=5*1024*1024

def unpack(data,extension):
 if extension in ('.txt','.md'):
  try:text=data.decode('utf-8-sig')
  except UnicodeError:raise TodoError('当前文本文件需要使用UTF-8编码') from None
  if '\x00' in text:raise TodoError('这不是可编辑的文本文件')
  paragraphs=text.splitlines();document=None
 else:
  try:
   z=zipfile.ZipFile(io.BytesIO(data));infos=z.infolist()
   if len(infos)>1000 or sum(i.file_size for i in infos)>25*1024*1024 or any(i.file_size>10*1024*1024 for i in infos):raise TodoError('Word文件展开后过大')
   for info in infos:
    name=info.filename.lower()
    if info.flag_bits&1:raise TodoError('不接收加密Word文件')
    if name.startswith('/') or '\\' in name or '..' in pathlib.PurePosixPath(name).parts or 'vbaproject' in name or name.startswith(('word/activex/','word/embeddings/')):raise TodoError('不接收包含宏、嵌入程序或异常路径的Word文件')
   xml=z.read('word/document.xml')
   if len(xml)>2*1024*1024 or b'<!DOCTYPE' in xml.upper() or b'<!ENTITY' in xml.upper():raise TodoError('Word内容格式不安全或过大')
   document=minidom.parseString(xml);paragraphs=[]
   for p in document.getElementsByTagNameNS(W,'p'):
    paragraphs.append(''.join(child.data for t in p.getElementsByTagNameNS(W,'t') for child in t.childNodes if child.nodeType==child.TEXT_NODE))
  except (zipfile.BadZipFile,KeyError,ValueError,ExpatError):raise TodoError('Word文件无法读取，请使用普通.docx文件') from None
 if len(paragraphs)>2000 or sum(map(len,paragraphs))>100000:raise TodoError('文档较长，请先选取要修改的部分')
 check_content('\n'.join(paragraphs).encode())
 return document,paragraphs

class Documents:
 def __init__(self,store):self.store=store;self.home=store.home/'documents';self.home.mkdir(mode=0o700,exist_ok=True);self.home.chmod(0o700)
 @staticmethod
 def initialize(c):
  c.executescript('''CREATE TABLE IF NOT EXISTS documents(id TEXT PRIMARY KEY,owner TEXT NOT NULL,name TEXT NOT NULL,extension TEXT NOT NULL,current_version INTEGER NOT NULL,created REAL NOT NULL);
  CREATE TABLE IF NOT EXISTS document_versions(document_id TEXT NOT NULL,version INTEGER NOT NULL,parent INTEGER,sha256 TEXT NOT NULL,path TEXT NOT NULL,created REAL NOT NULL,task TEXT NOT NULL,PRIMARY KEY(document_id,version));
  CREATE TABLE IF NOT EXISTS document_requests(owner TEXT NOT NULL,request_id TEXT NOT NULL,digest TEXT NOT NULL,document_id TEXT NOT NULL,PRIMARY KEY(owner,request_id));''')
 def _owned(self,c,owner,ident):
  r=c.execute('SELECT * FROM documents WHERE owner=? AND id=?',(owner,ident)).fetchone()
  if not owner or not r:raise TodoError('未找到属于当前设备的文档',404)
  return r
 def _bytes(self,row):
  path=self.home/row['path']
  if path.is_symlink() or path.parent!=self.home or not path.is_file() or path.stat().st_size>MAX_FILE:raise TodoError('文档版本不可用')
  data=path.read_bytes()
  if hashlib.sha256(data).hexdigest()!=row['sha256']:raise TodoError('文档版本校验失败，未覆盖文件')
  return data
 def _save(self,data):
  if len(data)>MAX_FILE:raise TodoError('修改后的文档超过5MB')
  name=uuid.uuid4().hex+'.data';path=self.home/name
  fd=os.open(str(path),os.O_WRONLY|os.O_CREAT|os.O_EXCL,0o600)
  with os.fdopen(fd,'wb') as f:f.write(data);f.flush();os.fsync(f.fileno())
  return name,hashlib.sha256(data).hexdigest()
 def upload(self,owner,body):
  if not isinstance(body,dict) or set(body)!={'name','data','request_id'} or not all(isinstance(v,str) for v in body.values()):raise TodoError('文档上传格式无效')
  name=body['name'];ext=pathlib.Path(name).suffix.lower()
  if ext not in ('.txt','.md','.docx') or not 1<=len(name)<=160 or '/' in name or '\\' in name or any(ord(c)<32 for c in name):raise TodoError('先支持.txt、.md和.docx文档')
  if not re.fullmatch(r'[A-Za-z0-9_-]{1,128}',body['request_id']) or len(body['data'])>7*1024*1024:raise TodoError('上传请求编号或大小无效')
  try:data=base64.b64decode(body['data'],validate=True)
  except ValueError:raise TodoError('文档编码无效') from None
  if not data or len(data)>MAX_FILE:raise TodoError('文档须为1字节到5MB')
  _,paragraphs=unpack(data,ext);digest=hashlib.sha256(name.encode()+data).hexdigest()
  with self.store.db() as c:
   c.execute('BEGIN IMMEDIATE');old=c.execute('SELECT * FROM document_requests WHERE owner=? AND request_id=?',(owner,body['request_id'])).fetchone()
   if old:
    if old['digest']!=digest:raise TodoError('相同上传编号对应不同文件',409)
    ident=old['document_id']
   else:
    ident=str(uuid.uuid4());path,sha=self._save(data);now=time.time()
    c.execute('INSERT INTO documents VALUES(?,?,?,?,1,?)',(ident,owner,name,ext,now));c.execute('INSERT INTO document_versions VALUES(?,1,NULL,?,?,?,\'\')',(ident,sha,path,now));c.execute('INSERT INTO document_requests VALUES(?,?,?,?)',(owner,body['request_id'],digest,ident));self.store._audit(c,'','document_imported',{'id':ident,'bytes':len(data),'paragraphs':len(paragraphs)})
  return self.info(owner,ident)
 def info(self,owner,ident):
  with self.store.db() as c:
   r=self._owned(c,owner,ident);v=c.execute('SELECT * FROM document_versions WHERE document_id=? AND version=?',(ident,r['current_version'])).fetchone();_,paragraphs=unpack(self._bytes(v),r['extension'])
   src=c.execute('SELECT device,path FROM document_sources WHERE document_id=? AND owner=?',(ident,owner)).fetchone()
   return {'id':ident,'name':r['name'],'version':r['current_version'],'sha256':v['sha256'],'paragraph_count':len(paragraphs),'bytes':len(self._bytes(v)),'source_device':src['device'] if src else None,'source_path':src['path'] if src else None}
 def read(self,owner,ident,start=0,limit=30):
  if type(start) is not int or type(limit) is not int or start<0 or not 1<=limit<=40:raise TodoError('文档段落范围无效')
  with self.store.db() as c:
   r=self._owned(c,owner,ident);v=c.execute('SELECT * FROM document_versions WHERE document_id=? AND version=?',(ident,r['current_version'])).fetchone();_,ps=unpack(self._bytes(v),r['extension'])
  rows=[];budget=10000
  for n in range(start,min(len(ps),start+limit)):
   text=ps[n][:3000]
   if len(text)>budget:break
   rows.append({'index':n,'text':text,'truncated':len(text)<len(ps[n])});budget-=len(text)
  return {**self.info(owner,ident),'paragraphs':rows,'truncated':start+len(rows)<len(ps)}
 def edit(self,owner,ident,version,changes,task):
  if type(version) is not int or not isinstance(changes,list) or not 1<=len(changes)<=30:raise TodoError('文档修改提案格式无效')
  with self.store.db() as c:
   c.execute('BEGIN IMMEDIATE');r=self._owned(c,owner,ident)
   if r['current_version']!=version:raise TodoError('文档已有新版本，请重新读取后修改',409)
   old=c.execute('SELECT * FROM document_versions WHERE document_id=? AND version=?',(ident,version)).fetchone();data=self._bytes(old);document,ps=unpack(data,r['extension']);seen=set()
   for change in changes:
    if not isinstance(change,dict) or set(change)!={'index','text'} or type(change['index']) is not int or not 0<=change['index']<=len(ps) or change['index'] in seen or not isinstance(change['text'],str) or len(change['text'])>3000:raise TodoError('修改段落编号、长度或内容无效')
    seen.add(change['index']);check_content(change['text'].encode())
    if r['extension']=='.docx' and '\n' in change['text']:raise TodoError('Word修改请逐段提供，不在同一段落插入换段指令')
   if all(x['index']<len(ps) and ps[x['index']]==x['text'] for x in changes):raise TodoError('提案没有实际修改，不会生成空版本')
   if document is None:
    newline='\r\n' if b'\r\n' in data else '\n';out=list(ps)
    for change in changes:
     if change['index']==len(ps):out.append(change['text'])
     else:out[change['index']]=change['text']
    result=(newline.join(out)+(newline if data.endswith((b'\n',b'\r')) else '')).encode()
   else:
    paragraphs=document.getElementsByTagNameNS(W,'p')
    for change in changes:
     if change['index']==len(ps):
      body=document.getElementsByTagNameNS(W,'body')[0];p=document.createElementNS(W,'w:p');run=document.createElementNS(W,'w:r');t=document.createElementNS(W,'w:t');run.appendChild(t);p.appendChild(run);end=next((n for n in body.childNodes if n.nodeType==n.ELEMENT_NODE and n.namespaceURI==W and n.localName=='sectPr'),None);body.insertBefore(p,end) if end is not None else body.appendChild(p)
     else:
      p=paragraphs[change['index']];texts=p.getElementsByTagNameNS(W,'t')
      if texts:
       t=texts[0]
       for node in texts:
        for child in list(node.childNodes):node.removeChild(child)
      else:
       run=document.createElementNS(W,'w:r');t=document.createElementNS(W,'w:t');run.appendChild(t);p.appendChild(run)
     t.setAttribute('xml:space','preserve');t.appendChild(document.createTextNode(change['text']))
    xml=document.toxml(encoding='UTF-8');output=io.BytesIO()
    with zipfile.ZipFile(io.BytesIO(data)) as original,zipfile.ZipFile(output,'w',zipfile.ZIP_DEFLATED) as changed:
     for item in original.infolist():changed.writestr(item,xml if item.filename=='word/document.xml' else original.read(item))
    result=output.getvalue()
   unpack(result,r['extension']);path,sha=self._save(result);next_version=c.execute('SELECT max(version)+1 FROM document_versions WHERE document_id=?',(ident,)).fetchone()[0];now=time.time()
   c.execute('INSERT INTO document_versions VALUES(?,?,?,?,?,?,?)',(ident,next_version,version,sha,path,now,task));c.execute('UPDATE documents SET current_version=? WHERE id=? AND owner=?',(next_version,ident,owner));self.store._audit(c,task,'document_edited',{'document_id':ident,'from':version,'to':next_version,'paragraphs':len(changes),'original_preserved':True})
  return self.info(owner,ident)
 def undo(self,owner,ident,version):
  with self.store.db() as c:
   c.execute('BEGIN IMMEDIATE');r=self._owned(c,owner,ident)
   if type(version) is not int or r['current_version']!=version:raise TodoError('文档版本已变化，未撤销较新的修改',409)
   row=c.execute('SELECT parent FROM document_versions WHERE document_id=? AND version=?',(ident,version)).fetchone()
   if not row['parent']:raise TodoError('原始版本没有可撤销的修改')
   c.execute('UPDATE documents SET current_version=? WHERE id=? AND owner=?',(row['parent'],ident,owner));self.store._audit(c,'','document_revision_undone',{'document_id':ident,'version':version,'restored':row['parent']})
  return self.info(owner,ident)
 def download(self,owner,ident,version,offset=0):
  if type(version) is not int or type(offset) is not int or offset<0:raise TodoError('文档下载范围无效')
  with self.store.db() as c:
   r=self._owned(c,owner,ident);v=c.execute('SELECT * FROM document_versions WHERE document_id=? AND version=?',(ident,version)).fetchone()
   if not v:raise TodoError('文档版本不存在',404)
   data=self._bytes(v)
  if offset>len(data):raise TodoError('文档下载范围越界')
  chunk=data[offset:offset+262144]
  name=pathlib.Path(r['name']).stem+('-PersonalOS-v'+str(version) if version>1 else '')+r['extension']
  return {'id':ident,'version':version,'name':name,'sha256':v['sha256'],'total':len(data),'offset':offset,'data':base64.b64encode(chunk).decode(),'next_offset':offset+len(chunk) if offset+len(chunk)<len(data) else None}
