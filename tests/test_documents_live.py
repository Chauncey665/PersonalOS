import base64,datetime as dt,hashlib,io,json,pathlib,sys,tempfile,time,unittest,zipfile
from unittest.mock import patch
sys.path.insert(0,str(pathlib.Path(__file__).resolve().parents[1]/'server'))
from personal_os import Store,Worker
from todos import TodoError
from documents import unpack
from live_info import LiveInfo
class DocumentTests(unittest.TestCase):
 def setUp(self):
  self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup);self.s=Store(pathlib.Path(self.temp.name));self.a=hashlib.sha256(b'A').hexdigest();self.b=hashlib.sha256(b'B').hexdigest()
  with self.s.db() as c:c.executemany('INSERT INTO devices VALUES(?,?)',[(self.a,'A'),(self.b,'B')])
 def upload(self,data,name='notes.txt'):
  return self.s.documents.upload(self.a,{'name':name,'data':base64.b64encode(data).decode(),'request_id':str(time.monotonic_ns())})
 def test_text_revision_undo_original_download_and_owner_isolation(self):
  original=b'First line\r\nSecond line\r\n';doc=self.upload(original);changed=self.s.documents.edit(self.a,doc['id'],1,[{'index':1,'text':'Edited second line'}],'fixture')
  self.assertEqual(changed['version'],2);read=self.s.documents.download(self.a,doc['id'],1);self.assertEqual(base64.b64decode(read['data']),original)
  self.assertIn(b'Edited second',base64.b64decode(self.s.documents.download(self.a,doc['id'],2)['data']))
  with self.assertRaises(TodoError):self.s.documents.download(self.b,doc['id'],2)
  with self.assertRaises(TodoError):self.s.documents.edit(self.a,doc['id'],1,[{'index':0,'text':'stale'}],'fixture')
  self.assertEqual(self.s.documents.undo(self.a,doc['id'],2)['version'],1)
  self.assertEqual(self.s.documents.edit(self.a,doc['id'],1,[{'index':0,'text':'Different branch'}],'fixture')['version'],3)
 def test_docx_preserves_other_parts_and_unedited_formatted_paragraphs(self):
  buffer=io.BytesIO();xml=b'<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main" xmlns:mc="urn:mc" xmlns:w14="urn:w14" mc:Ignorable="w14"><w:body><w:p><w:r><w:rPr><w:b/></w:rPr><w:t>Original heading</w:t></w:r></w:p><w:tbl><w:tr><w:tc><w:p><w:r><w:t>Table value</w:t></w:r></w:p></w:tc></w:tr></w:tbl><w:sectPr/></w:body></w:document>'
  with zipfile.ZipFile(buffer,'w') as z:z.writestr('word/document.xml',xml);z.writestr('word/styles.xml',b'<style>preserved</style>')
  doc=self.upload(buffer.getvalue(),'document.docx');edited=self.s.documents.edit(self.a,doc['id'],1,[{'index':0,'text':'New heading'}],'fixture');download=self.s.documents.download(self.a,doc['id'],edited['version']);data=base64.b64decode(download['data'])
  with zipfile.ZipFile(io.BytesIO(data)) as z:self.assertEqual(z.read('word/styles.xml'),b'<style>preserved</style>');self.assertIn(b'xmlns:w14="urn:w14"',z.read('word/document.xml'));self.assertIn(b'w:b',z.read('word/document.xml'))
  self.assertEqual(unpack(data,'.docx')[1],['New heading','Table value'])
 def test_macro_path_traversal_bad_xml_and_nonutf8_rejected_before_import(self):
  for name,xml in [('word/vbaProject.bin',b'x'),('../bad',b'x'),('word/document.xml',b'<!DOCTYPE x [<!ENTITY e "bad">]><x/>')]:
   b=io.BytesIO()
   with zipfile.ZipFile(b,'w') as z:z.writestr('word/document.xml',b'<document/>');z.writestr(name,xml)
   with self.subTest(name=name),self.assertRaises(TodoError):self.upload(b.getvalue(),'bad.docx')
  with self.assertRaises(TodoError):self.upload(b'\xff\xfe')
 def test_attachment_binding_and_current_instruction_gate(self):
  doc=self.upload(b'Original paragraph')
  with self.assertRaises(TodoError):self.s.submit({'text':'edit','document_ids':json.dumps([doc['id']])},self.b)
  ident=self.s.submit({'text':'总结这份文档','document_ids':json.dumps([doc['id']])},self.a);row=self.s.task(ident)
  with self.assertRaises(ValueError):self.s.brain.execute_tool('edit_document',{'id':doc['id'],'version':1,'changes':[{'index':0,'text':'bad'}]},row,{})
  with self.assertRaises(ValueError):self.s.brain.execute_tool('files',{'operation':'read','device':'mini','path':'/Users/example/private.txt'},row,{})
 def test_local_model_json_edit_creates_real_new_file_without_touching_import(self):
  doc=self.upload('这是一份需要润色的文件。'.encode())
  def model(request,**kwargs):
   payload=json.loads(request.data);self.assertEqual(request.full_url,'http://127.0.0.1:11434/api/chat');self.assertFalse(payload['think'])
   return io.BytesIO(json.dumps({'message':{'content':json.dumps({'changes':[{'index':0,'text':'这份文件已完成润色。'}]},ensure_ascii=False)}}).encode())
  self.s.brain.transport=model;ident=self.s.submit({'text':'帮我润色这份文档','document_ids':json.dumps([doc['id']])},self.a);Worker(self.s).execute(self.s.task(ident));r=self.s.task(ident);self.assertEqual(r['state'],'completed');self.assertEqual(json.loads(r['plan'])['documents'][0]['version'],2)
  self.assertEqual(base64.b64decode(self.s.documents.download(self.a,doc['id'],1)['data']),'这是一份需要润色的文件。'.encode())

class LiveTests(unittest.TestCase):
 def setUp(self):
  self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup);self.s=Store(pathlib.Path(self.temp.name));self.a='fixture-owner'
 def test_only_fixed_public_destinations_and_no_secret_search_terms(self):
  live=LiveInfo(self.s,lambda *a,**kw: self.fail('Network must not be reached'))
  for url in ['http://api.open-meteo.com/v1/forecast','https://127.0.0.1/','https://www.bing.com/private','https://user@www.bing.com/search','https://api.open-meteo.com:443/v1/forecast']:
   with self.subTest(url=url),self.assertRaises(TodoError):live.fetch(url,'json')
  with self.assertRaises(TodoError):live.search('private secret','search public weather')
  with self.assertRaises(TodoError):live.weather(self.a)
 def test_location_rounding_owner_isolation_and_weather_freshness(self):
  now=time.time();self.s.live.location(self.a,{'latitude':22.34789,'longitude':114.2891,'time_zone':'Asia/Hong_Kong','captured_at':now})
  with self.s.db() as c:r=c.execute('SELECT latitude,longitude FROM weather_regions WHERE owner=?',(self.a,)).fetchone();self.assertEqual(tuple(r),(22.3,114.3))
  with self.assertRaises(TodoError):self.s.live.weather('another-owner')
  zone=dt.timezone(dt.timedelta(hours=8));label=dt.datetime.now(zone).replace(minute=0,second=0,microsecond=0).strftime('%Y-%m-%dT%H:%M')
  def network(request,**kwargs):
   self.assertTrue(request.full_url.startswith('https://api.open-meteo.com/v1/forecast?'));self.assertNotIn('22.34789',request.full_url)
   return io.BytesIO(json.dumps({'timezone':'Asia/Hong_Kong','current':{'time':label,'temperature_2m':25,'weather_code':3},'hourly':{'time':[label],'temperature_2m':[25],'precipitation_probability':[80],'precipitation':[1]}}).encode())
  self.s.live.transport=network;r=self.s.live.weather(self.a);self.assertEqual(r['hourly'][0]['rain_probability_percent'],80);self.assertIn('source_url',r)
 def test_official_holiday_data_is_not_invented_for_unpublished_year(self):
  live=LiveInfo(self.s,lambda *a,**kw:io.BytesIO(json.dumps({'vcalendar':[{'vevent':[{'dtstart':['20261001',{}],'summary':'National Day'}]}]}).encode()))
  r=live.holidays(2026,'国庆');self.assertEqual(r['events'][0]['date'],'2026-10-01');self.assertEqual(r['events'][0]['name'],'国庆节');self.assertEqual(live.holidays(2030)['events'],[])
 def test_weather_data_cannot_authorize_file_read_or_todo_write(self):
  owner=hashlib.sha256(b'A').hexdigest()
  with self.s.db() as c:c.execute('INSERT INTO devices VALUES(?,?)',(owner,'A'))
  ident=self.s.submit({'text':'今天天气怎么样？'},owner);row=self.s.task(ident)
  with self.assertRaises(ValueError):self.s.brain.execute_tool('files',{'operation':'read','device':'mini','path':'/Users/example/private.txt'},row,{})
if __name__=='__main__':unittest.main()
