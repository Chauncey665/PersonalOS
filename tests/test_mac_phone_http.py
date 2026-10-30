"""Authenticated request-to-real-file effects; all accounts/files are synthetic."""
import base64,hashlib,io,json,pathlib,secrets,sys,tempfile,threading,unittest,urllib.request,urllib.error
sys.path.insert(0,str(pathlib.Path(__file__).resolve().parents[1]/'server'))
from personal_os import Store,Worker,Handler,APNsProvider
from mac_actions import MacActions
from http.server import ThreadingHTTPServer
class HTTPFlows(unittest.TestCase):
 def setUp(self):
  self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup);self.root=pathlib.Path(self.temp.name).resolve();self.s=Store(self.root/'state');self.tokens=[secrets.token_urlsafe(32) for _ in range(2)];self.owners=[hashlib.sha256(t.encode()).hexdigest() for t in self.tokens];self.calls=[]
  with self.s.db() as c:c.executemany('INSERT INTO devices VALUES(?,?)',[(o,'Synthetic') for o in self.owners])
  (self.s.home/'files-access.json').write_text('{"enabled":true}');(self.s.home/'files-access.json').chmod(0o600);self.s.mac=MacActions(self.s,lambda req:self.calls.append(req) or {'accepted':True},self.root/'PersonalOS Inbox')
  self.server=ThreadingHTTPServer(('127.0.0.1',0),Handler);self.server.store=self.s;self.server.push_provider=APNsProvider(self.root/'absent.json');threading.Thread(target=self.server.serve_forever,daemon=True).start();self.addCleanup(self.server.server_close);self.addCleanup(self.server.shutdown)
 def api(self,path,body=None,who=0):
  headers={'Content-Type':'application/json'}
  if who is not None:headers['Authorization']='Bearer '+self.tokens[who]
  with urllib.request.urlopen(urllib.request.Request('http://127.0.0.1:'+str(self.server.server_port)+path,data=None if body is None else json.dumps(body).encode(),headers=headers),timeout=10) as r:return json.load(r)
 def test_endpoints_require_auth_and_confirmed_app_launch_is_owner_scoped(self):
  for path in ['/mac/status','/mac/actions','/transfer','/documents/import']:
   with self.subTest(path=path),self.assertRaises(urllib.error.HTTPError) as e:self.api(path,None if path.endswith('status') else {},who=None)
   self.assertEqual(e.exception.code,401)
  task=self.api('/mac/actions',{'payload':json.dumps({'operation':'launch_app','app':'safari'})})['id'];self.assertEqual(self.calls,[])
  with self.assertRaises(urllib.error.HTTPError) as e:self.api('/tasks/'+task+'/approve',{},who=1)
  self.assertEqual(e.exception.code,404);self.assertEqual(self.calls,[])
  self.api('/tasks/'+task+'/approve',{});self.assertEqual(len(self.calls),1)
  with self.assertRaises(urllib.error.HTTPError) as e:self.api('/tasks/'+task+'/approve',{})
  self.assertEqual(e.exception.code,409)
 def test_import_edit_preview_confirm_undo_over_real_http(self):
  path=self.root/'notice.md';path.write_text('Original notice 15:00');preview=self.api('/files/read',{'device':'mini','path':str(path)})
  doc=self.api('/documents/import',{'device':'mini','path':str(path),'etag':preview['etag'],'request_id':'selected-mac-doc'})
  self.s.documents.edit(self.owners[0],doc['id'],1,[{'index':0,'text':'Edited notice 15:00'}],'synthetic')
  ident=self.api('/documents/'+doc['id']+'/writeback',{'version':'2'})['id'];self.assertEqual(path.read_text(),'Original notice 15:00')
  self.api('/tasks/'+ident+'/approve',{});self.assertEqual(path.read_text(),'Edited notice 15:00');self.api('/tasks/'+ident+'/undo',{});self.assertEqual(path.read_text(),'Original notice 15:00')
 def test_transfer_idempotent_and_foreign_device_cannot_read_result(self):
  body={'name':'notice.pdf','data':base64.b64encode(b'%PDF Fixture').decode(),'request_id':'phone-transfer-one'};one=self.api('/transfer',body);two=self.api('/transfer',body);self.assertEqual(one['id'],two['id']);self.assertEqual(len(list(self.s.mac.inbox.iterdir())),1)
  with self.assertRaises(urllib.error.HTTPError) as e:self.api('/tasks/'+one['id'],who=1)
  self.assertEqual(e.exception.code,404)
 def test_spoken_actions_route_before_calendar_and_message_time_is_not_a_reminder(self):
  for text,kind in [('在Mac打开Safari','mac_action'),('找附近餐厅','phone_action'),('导航到香港科技大学','phone_action')]:
   ident=self.api('/tasks',{'text':text})['id'];Worker(self.s).execute(self.s.task(ident));row=self.api('/tasks/'+ident);self.assertEqual(json.loads(row['plan'])['kind'],kind)
  self.assertEqual(self.calls,[])
  self.s.brain.transport=lambda *a,**kw:io.BytesIO(json.dumps({'message':{'content':json.dumps({'draft':'请问明天几点方便？'})}}).encode())
  ident=self.api('/tasks',{'text':'帮我回复这条消息：明天提醒我参加会议。'})['id'];self.s.process_reminders();self.assertEqual(self.s.task(ident)['state'],'queued');Worker(self.s).execute(self.s.task(ident));self.assertEqual(self.s.task(ident)['state'],'completed')
  with self.s.db() as c:self.assertEqual(c.execute('SELECT count(*) FROM calendar_entries').fetchone()[0],0)
if __name__=='__main__':unittest.main()
