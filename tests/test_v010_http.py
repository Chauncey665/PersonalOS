"""Actual authenticated HTTP boundary and immutable document transfer coverage."""
import base64,datetime as dt,hashlib,io,json,pathlib,secrets,sys,tempfile,threading,time,unittest,urllib.request,urllib.error,zipfile
sys.path.insert(0,str(pathlib.Path(__file__).resolve().parents[1]/'server'))
from personal_os import Store,Handler,APNsProvider
from http.server import ThreadingHTTPServer
class FeatureHTTP(unittest.TestCase):
 def setUp(self):
  self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup);self.s=Store(pathlib.Path(self.tmp.name));self.tokens=[secrets.token_urlsafe(32) for _ in range(2)];self.owners=[hashlib.sha256(t.encode()).hexdigest() for t in self.tokens]
  with self.s.db() as c:c.executemany('INSERT INTO devices VALUES(?,?)',[(o,'HTTP QA') for o in self.owners])
  self.server=ThreadingHTTPServer(('127.0.0.1',0),Handler);self.server.store=self.s;self.server.push_provider=APNsProvider(pathlib.Path(self.tmp.name)/'no-push.json');threading.Thread(target=self.server.serve_forever,daemon=True).start();self.addCleanup(self.server.server_close);self.addCleanup(self.server.shutdown)
 def api(self,path,body=None,index=0):
  headers={'Content-Type':'application/json'}
  if index is not None:headers['Authorization']='Bearer '+self.tokens[index]
  with urllib.request.urlopen(urllib.request.Request('http://127.0.0.1:'+str(self.server.server_port)+path,data=None if body is None else json.dumps(body).encode(),headers=headers),timeout=5) as response:return json.load(response)
 def test_new_routes_require_auth_and_isolate_location_and_planner(self):
  for path in ['/planning/status','/location/status','/assistant/latest-result']:
   with self.subTest(path=path),self.assertRaises(urllib.error.HTTPError) as e:self.api(path,index=None)
   self.assertEqual(e.exception.code,401)
  self.api('/planning/settings',{'payload':json.dumps({'enabled':True})});self.assertFalse(self.api('/planning/status',index=1)['settings']['enabled'])
  self.api('/location',{'payload':json.dumps({'latitude':22.34789,'longitude':114.2891,'time_zone':'Asia/Hong_Kong','captured_at':time.time()})})
  self.assertTrue(self.api('/location/status')['available']);self.assertFalse(self.api('/location/status',index=1)['available']);self.api('/location/clear',{});self.assertFalse(self.api('/location/status')['available'])
 def test_document_large_json_upload_chunk_transfer_and_other_owner_cannot_read_or_use(self):
  buffer=io.BytesIO()
  with zipfile.ZipFile(buffer,'w') as z:z.writestr('word/document.xml','<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"><w:body><w:p><w:r><w:t>Fixture text</w:t></w:r></w:p></w:body></w:document>');z.writestr('word/media/image.png',b'x'*300000)
  original=buffer.getvalue();body={'name':'fixture.docx','request_id':'stable-upload','data':base64.b64encode(original).decode()};doc=self.api('/documents',body);self.assertEqual(self.api('/documents',body)['id'],doc['id'])
  offset=0;output=b''
  while True:
   chunk=self.api('/documents/'+doc['id']+'/download?version=1&offset='+str(offset));output+=base64.b64decode(chunk['data'])
   if chunk['next_offset'] is None:break
   offset=chunk['next_offset']
  self.assertGreater(offset,0);self.assertEqual(output,original);self.assertEqual(hashlib.sha256(output).hexdigest(),doc['sha256'])
  for path,body in [('/documents/'+doc['id'],None),('/documents/'+doc['id']+'/download?version=1',None),('/tasks',{'text':'改这份文档','document_ids':json.dumps([doc['id']])})]:
   with self.subTest(path=path),self.assertRaises(urllib.error.HTTPError) as e:self.api(path,body,index=1)
   self.assertEqual(e.exception.code,404)
  ident=self.api('/tasks',{'text':'修改这份文档','document_ids':json.dumps([doc['id']])})['id'];self.s.update(ident,state='completed',result='Private document revision result')
  self.assertFalse(any(t['id']==ident for t in self.api('/tasks',index=1)));self.assertNotEqual(self.api('/assistant/latest-result',index=1)['id'],ident)
  self.assertEqual(self.api('/assistant/latest-result')['id'],ident)
 def test_http_undo_revision_conflict_keeps_current_version(self):
  doc=self.api('/documents',{'name':'fixture.txt','request_id':'undo-upload','data':base64.b64encode(b'Original').decode()});ident=doc['id'];self.s.documents.edit(self.owners[0],ident,1,[{'index':0,'text':'Modified'}],'synthetic')
  with self.assertRaises(urllib.error.HTTPError) as e:self.api('/documents/'+ident+'/undo',{'version':'1'})
  self.assertEqual(e.exception.code,409);self.assertEqual(self.api('/documents/'+ident)['version'],2)
  self.assertEqual(self.api('/documents/'+ident+'/undo',{'version':'2'})['version'],1)
if __name__=='__main__':unittest.main()
