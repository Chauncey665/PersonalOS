"""Public edition accepts user-selected endpoints without inheriting a private deployment."""
import io,json,pathlib,sys,tempfile,unittest
sys.path.insert(0,str(pathlib.Path(__file__).resolve().parents[1]/'server'))
from personal_os import Store
from canvas_sync import CanvasSync,feed_url,feed_items
from deployment_config import validate_canvas_base,valid_remote
from file_access import RemoteFileAccess
from remote_sources import ProjectRemoteSource
class PublicSetupTests(unittest.TestCase):
 def setUp(self):
  self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup);self.root=pathlib.Path(self.tmp.name).resolve()
 def test_canvas_uses_only_locally_selected_origin(self):
  p=self.root/'integrations.json';p.write_text(json.dumps({'canvas_base_url':'https://canvas.school.example.edu/'}));p.chmod(0o600)
  store=Store(self.root);self.assertEqual(store.canvas.base,'https://canvas.school.example.edu')
  url='https://canvas.school.example.edu/feeds/calendars/user_'+'a'*24+'.ics'
  self.assertEqual(feed_url(url,store.canvas.base),url)
  with self.assertRaises(ValueError):feed_url(url.replace('canvas.school','other.school'),store.canvas.base)
  calls=[]
  def read(req,**kw):calls.append(req.full_url);return io.BytesIO(b'{"id":42}')
  c=CanvasSync(store,transport=read);c.get('/api/v1/users/self/profile','synthetic-token')
  self.assertEqual(calls,['https://canvas.school.example.edu/api/v1/users/self/profile'])
  ics='BEGIN:VCALENDAR\nBEGIN:VEVENT\nUID:event-assignment-7\nURL:https://canvas.school.example.edu/courses/3/assignments/7\nSUMMARY:Example task\nDTSTART:20300101T120000Z\nEND:VEVENT\nEND:VCALENDAR'
  self.assertEqual(feed_items(ics,base_url=c.base)[0]['url'],'https://canvas.school.example.edu/courses/3/assignments/7')
 def test_canvas_config_is_scoped_and_fail_closed(self):
  for value in ['http://canvas.example.edu','https://127.0.0.1','https://canvas.example.edu@localhost','https://canvas.example.edu/a','https://mac.example.ts.net']:
   with self.assertRaises(ValueError):validate_canvas_base(value)
  p=self.root/'integrations.json';p.write_text('{"canvas_base_url":"https://canvas.school.edu"}');p.chmod(0o644)
  with self.assertRaises(ValueError):Store(self.root)
 def test_remote_target_is_user_selected_and_strictly_readonly(self):
  key=self.root/'dedicated-key';key.write_text('synthetic key, not used by SSH');key.chmod(0o600)
  known=self.root/'pinned-hosts';known.write_text('synthetic test only');known.chmod(0o600)
  config={'host':'air.example.ts.net','user':'alice','remote_home':'/Users/alice','enabled':True,'key_path':str(key),'known_hosts_path':str(known)}
  p=self.root/'air-files-source.json';p.write_text(json.dumps(config));p.chmod(0o600)
  self.assertEqual(RemoteFileAccess(p)._config()['remote_home'],'/Users/alice')
  project={**config,'remote_root':'/Users/alice/Documents/Example'};p.write_text(json.dumps(project))
  source=ProjectRemoteSource(p);parsed,status=source._config();self.assertEqual(status,'configured');self.assertEqual(source._arguments(parsed)[-1],'alice@air.example.ts.net')
  export={'version':1,'remote_root':project['remote_root'],'fetched_at':'2030-01-01T00:00:00Z','hits':[],'scanned':0,'truncated':False}
  self.assertEqual(source._validate_export(export,project['remote_root']),[])
  self.assertFalse(valid_remote({**config,'host':'example.com'},'remote_home'));self.assertFalse(valid_remote({**config,'user':'root'},'remote_home'))
if __name__=='__main__':unittest.main()
