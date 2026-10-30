"""Local TTS jobs and authenticated audio; no public or credential-bearing audio URLs."""
import hashlib,json,os,pathlib,queue,re,select,subprocess,threading,time,uuid
from spoken_reply import brief
from speech_text import natural_text,REVISION
from todos import TodoError
class SpeechOutput:
 def __init__(self,store):
  self.s=store;self.folder=store.home/'speech-output';self.jobs=queue.Queue(maxsize=8);self.started=False;self.lock=threading.Lock();self.process=None
 @staticmethod
 def initialize(c):
  c.executescript('''CREATE TABLE IF NOT EXISTS speech_preferences(owner TEXT PRIMARY KEY,voice TEXT NOT NULL);
  CREATE TABLE IF NOT EXISTS speech_outputs(id TEXT PRIMARY KEY,owner TEXT NOT NULL,voice TEXT NOT NULL,text_hash TEXT NOT NULL,state TEXT NOT NULL,created REAL NOT NULL,metrics TEXT NOT NULL DEFAULT '{}');''')
 def recover(self):
  # Queued text is deliberately memory-only. After a restart use system speech.
  with self.s.db() as c:c.execute("UPDATE speech_outputs SET state='failed' WHERE state='queued'")
  with self.s.db() as c:
   c.execute("UPDATE speech_preferences SET voice='Uncle_Fu' WHERE voice='Aiden'")
   for r in c.execute("SELECT id FROM speech_outputs WHERE voice='Aiden'").fetchall():
    if re.fullmatch('[0-9a-f]{32}',r['id']):(self.folder/(r['id']+'.wav')).unlink(missing_ok=True)
   c.execute("DELETE FROM speech_outputs WHERE voice='Aiden'")
  self.cleanup()
 def maintain(self,stop):
  while not stop.wait(300):self.cleanup()
 def config(self):
  try:
   p=self.s.home/'tts.json'
   if p.is_symlink() or p.stat().st_mode&0o077:return None
   c=json.loads(p.read_text())
   if c.get('enabled') is not True or not all(pathlib.Path(c[x]).exists() for x in ('python','model')):return None
   return c
  except (OSError,ValueError,KeyError):return None
 def preferences(self,owner):
  with self.s.db() as c:r=c.execute('SELECT voice FROM speech_preferences WHERE owner=?',(owner,)).fetchone()
  return {'voice':r['voice'] if r else 'system','available':bool(self.config()),'voices':['system','Uncle_Fu']}
 def configure(self,owner,value):
  if set(value)!={'voice'} or value['voice'] not in ('system','Uncle_Fu'):raise TodoError('声音选项无效')
  with self.s.db() as c:c.execute('INSERT INTO speech_preferences VALUES(?,?) ON CONFLICT(owner) DO UPDATE SET voice=excluded.voice',(owner,value['voice']))
  return self.preferences(owner)
 def request(self,owner,value):
  if set(value)-{'session_id','text','voice'}:raise TodoError('播报参数无效')
  if value.get('session_id'):text=self.s.sessions.state(owner,value['session_id'])['speech']
  else:text=value.get('text','')
  if isinstance(text,str) and '最高降雨概率' in text and '|' in text:text=brief(text)
  if not isinstance(text,str) or not 0<len(text)<=650:raise TodoError('播报文字须为1到650字')
  voice=value.get('voice',self.preferences(owner)['voice'])
  if voice not in ('system','Uncle_Fu'):raise TodoError('声音选项无效')
  if voice=='system' or not self.config():return {'id':'','state':'system'}
  text=natural_text(text)
  digest=hashlib.sha256((REVISION+'|'+text).encode()).hexdigest();ident=uuid.uuid4().hex
  self.cleanup()
  with self.s.db() as c:
   old=c.execute("SELECT * FROM speech_outputs WHERE owner=? AND voice=? AND text_hash=? AND created>? AND state IN ('queued','ready') ORDER BY created DESC LIMIT 1",(owner,voice,digest,time.time()-86400)).fetchone()
   if old:return {'id':old['id'],'state':old['state']}
   c.execute('INSERT INTO speech_outputs(id,owner,voice,text_hash,state,created) VALUES(?,?,?,?,?,?)',(ident,owner,voice,digest,'queued',time.time()))
  try:self.jobs.put_nowait((ident,text,voice))
  except queue.Full:
   with self.s.db() as c:c.execute("UPDATE speech_outputs SET state='failed' WHERE id=?",(ident,))
   return {'id':ident,'state':'system'}
  with self.lock:
   if not self.started:self.started=True;threading.Thread(target=self.run,daemon=True).start()
  return {'id':ident,'state':'queued'}
 def status(self,owner,ident):
  with self.s.db() as c:r=c.execute('SELECT id,state,created FROM speech_outputs WHERE id=? AND owner=?',(ident,owner)).fetchone()
  if not r or r['created']<time.time()-86400:raise TodoError('音频已过期',404)
  return {'id':r['id'],'state':r['state']}
 def audio(self,owner,ident):
  if self.status(owner,ident)['state']!='ready':raise TodoError('音频尚未准备好',409)
  p=self.folder/(ident+'.wav')
  if not re.fullmatch('[0-9a-f]{32}',ident) or p.is_symlink() or not p.is_file() or p.stat().st_size>8*1024*1024:raise TodoError('音频不可用',404)
  return p.read_bytes()
 def cleanup(self):
  with self.s.db() as c:
   for r in c.execute('SELECT id FROM speech_outputs WHERE created<?',(time.time()-86400,)).fetchall():
    (self.folder/(r['id']+'.wav')).unlink(missing_ok=True)
   c.execute('DELETE FROM speech_outputs WHERE created<?',(time.time()-86400,))
 def run(self):
  while True:
   ident,text,voice=self.jobs.get()
   try:
    config=self.config()
    if not config:raise ValueError('not_ready')
    self.folder.mkdir(mode=0o700,exist_ok=True)
    if self.process is None or self.process.poll() is not None:
     env=dict(os.environ,HF_HUB_OFFLINE='1',TRANSFORMERS_OFFLINE='1',HF_HUB_DISABLE_IMPLICIT_TOKEN='1')
     self.process=subprocess.Popen([config['python'],str(pathlib.Path(__file__).with_name('synthesize_local.py')),config['model'],str(self.folder)],stdin=subprocess.PIPE,stdout=subprocess.PIPE,stderr=subprocess.DEVNULL,text=True,bufsize=1,env=env)
    language='Chinese' if re.search('[\u4e00-\u9fff]',text) else 'English'
    self.process.stdin.write(json.dumps({'id':ident,'text':text,'speaker':voice,'language':language},ensure_ascii=False)+'\n');self.process.stdin.flush()
    ready,_,_=select.select([self.process.stdout],[],[],90)
    if not ready:raise TimeoutError()
    result=json.loads(self.process.stdout.readline())
    if not result.get('ok') or result.get('id')!=ident:raise ValueError('generation_failed')
    with self.s.db() as c:c.execute("UPDATE speech_outputs SET state='ready',metrics=? WHERE id=?",(json.dumps(result),ident))
   except Exception:
    if self.process is not None:
     self.process.kill();self.process.wait();self.process=None
    with self.s.db() as c:c.execute("UPDATE speech_outputs SET state='failed' WHERE id=?",(ident,))
   finally:self.jobs.task_done()
