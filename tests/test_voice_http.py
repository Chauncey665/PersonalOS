import hashlib,json,pathlib,sys,tempfile,threading,unittest,urllib.request,urllib.error,uuid
sys.path.insert(0,str(pathlib.Path(__file__).resolve().parents[1]/'server'))
from personal_os import Store,Worker,Handler,ThreadingHTTPServer
class ConversationHTTPTests(unittest.TestCase):
 def setUp(self):
  self.tmp=tempfile.TemporaryDirectory();self.s=Store(self.tmp.name);self.tokens=['fixture-a','fixture-b'];self.owners=[hashlib.sha256(t.encode()).hexdigest() for t in self.tokens]
  with self.s.db() as c:c.executemany('INSERT INTO devices VALUES(?,?)',[(o,'test') for o in self.owners])
  self.server=ThreadingHTTPServer(('127.0.0.1',0),Handler);self.server.store=self.s;self.thread=threading.Thread(target=self.server.serve_forever,daemon=True);self.thread.start()
 def tearDown(self):self.server.shutdown();self.server.server_close();self.thread.join();self.tmp.cleanup()
 def api(self,path,body=None,owner=0):
  headers={'Content-Type':'application/json'}
  if owner is not None:headers['Authorization']='Bearer '+self.tokens[owner]
  req=urllib.request.Request('http://127.0.0.1:'+str(self.server.server_port)+path,data=json.dumps(body).encode() if body is not None else None,headers=headers)
  with urllib.request.urlopen(req,timeout=4) as response:return json.load(response)
 def denied(self,status,*args,**kwargs):
  with self.assertRaises(urllib.error.HTTPError) as error:self.api(*args,**kwargs)
  self.assertEqual(error.exception.code,status)
 def test_retry_owner_scoping_restart_and_continuation(self):
  self.denied(401,'/assistant/sessions/current',owner=None)
  state=self.api('/assistant/sessions',{});sid=state['id'];path='/assistant/sessions/'+sid
  self.denied(404,path,owner=1)
  b={'text':'提醒我拿快递','session_id':sid,'request_id':'first'};ident=self.api('/tasks',b)['id'];self.assertEqual(self.api('/tasks',b)['id'],ident)
  Worker(self.s).execute(self.s.task(ident));self.assertEqual(self.api(path)['status'],'completed')
  self.denied(409,'/tasks',{'text':'设备状态','session_id':sid,'request_id':'stale'})
  # New Store object uses the on-disk session, todo and latest turn without replaying.
  self.s=Store(self.tmp.name);self.server.store=self.s;self.assertEqual(self.api(path)['task_id'],ident)
  self.assertEqual(len(self.s.todos.list(self.owners[0])['items']),1)
  nextid=self.api('/tasks',{'text':'设备状态','session_id':sid,'reply_to':ident,'request_id':'next'})['id'];Worker(self.s).execute(self.s.task(nextid));self.assertIn('可用空间',self.api(path)['speech'])
 def test_private_speech_preferences_and_receipts(self):
  self.assertEqual(self.api('/assistant/speech/preferences')['voice'],'system')
  self.api('/assistant/speech/preferences',{'voice':'Uncle_Fu'});self.assertEqual(self.api('/assistant/speech/preferences',owner=1)['voice'],'system')
  state=self.api('/assistant/sessions',{});sid=state['id'];ident=self.api('/tasks',{'text':'找附近餐厅','session_id':sid})['id'];Worker(self.s).execute(self.s.task(ident));state=self.api('/assistant/sessions/'+sid)
  receipt={'payload':json.dumps({'action_id':state['phone_action']['id'],'result':{'choices':[]}})};path='/assistant/sessions/'+sid+'/receipts';self.denied(404,path,receipt,owner=1);self.assertTrue(self.api(path,receipt)['ok']);self.assertTrue(self.api(path,receipt)['ok'])
  self.assertEqual(self.api('/assistant/sessions/'+sid)['status'],'needs_input')
if __name__=='__main__':unittest.main()
