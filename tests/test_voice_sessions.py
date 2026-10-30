import base64,io,json,pathlib,sys,tempfile,time,unittest,uuid
sys.path.insert(0,str(pathlib.Path(__file__).resolve().parents[1]/'server'))
from personal_os import Store,Worker
from mac_actions import MacActions
from voice_sessions import packed
from todos import TodoError
class SessionTests(unittest.TestCase):
 def setUp(self):
  self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup);self.root=pathlib.Path(self.tmp.name).resolve();self.s=Store(self.root/'state');self.owner='owner-a';self.other='owner-b';self.calls=[]
  with self.s.db() as c:c.executemany('INSERT INTO devices VALUES(?,?)',[(self.owner,'A'),(self.other,'B')])
  p=self.s.home/'files-access.json';p.write_text('{"enabled":true}');p.chmod(0o600)
  self.s.mac=MacActions(self.s,lambda x:self.calls.append(x) or {'accepted':True},self.root/'PersonalOS Inbox');self.session=self.s.sessions.create(self.owner)['id']
 def turn(self,text,extra=None):
  state=self.s.sessions.state(self.owner,self.session);body={'text':text,'session_id':self.session,'reply_to':state['task_id'],'request_id':str(uuid.uuid4())};body.update(extra or {})
  ident=self.s.submit(body,self.owner);Worker(self.s).execute(self.s.task(ident));return ident,self.s.sessions.state(self.owner,self.session)
 def preview(self):
  path=self.root/'report.md';path.write_text('Original paragraph.');meta=self.s.file_access.handle('read',{'device':'mini','path':str(path)})
  doc=self.s.mac.import_file(self.owner,{'device':'mini','path':str(path),'etag':meta['etag'],'request_id':'file'});self.s.documents.edit(self.owner,doc['id'],1,[{'index':0,'text':'Shortened paragraph.'}],'test')
  ident=self.s.submit({'text':'改报告','session_id':self.session},self.owner);row=self.s.task(ident);self.s.update(ident,**self.s.mac.prepare(row,{'operation':'write_document','document_id':doc['id'],'version':2}));self.s.sessions.confirm(row,{},ident);return path,ident
 def test_direct_open_has_real_receipt_and_no_duplicate(self):
  state=self.s.sessions.state(self.owner,self.session);body={'text':'在Mini打开Safari','session_id':self.session,'request_id':'open-once'};ident=self.s.submit(body,self.owner);Worker(self.s).execute(self.s.task(ident));self.assertEqual(self.s.sessions.state(self.owner,self.session)['status'],'completed');self.assertEqual(self.calls,[{'operation':'launch_app','app':'safari'}]);self.assertEqual(self.s.submit(body,self.owner),ident);self.assertEqual(len(self.calls),1)
 def test_legacy_client_still_requires_preview(self):
  ident=self.s.submit({'text':'在Mini打开Safari'},self.owner);Worker(self.s).execute(self.s.task(ident));self.assertEqual(self.s.task(ident)['state'],'awaiting_confirmation');self.assertFalse(self.calls)
 def test_cross_owner_and_stale_parent_denied(self):
  with self.assertRaises(TodoError):self.s.sessions.state(self.other,self.session)
  ident,_=self.turn('设备状态')
  with self.assertRaises(TodoError):self.s.submit({'text':'在Mini打开Safari','session_id':self.session},self.owner)
  with self.assertRaises(ValueError):self.s.task(ident,self.other)
 def test_file_confirmation_write_and_undo(self):
  path,target=self.preview();self.assertEqual(path.read_text(),'Original paragraph.');_,state=self.turn('确认写回');self.assertEqual(state['status'],'completed');self.assertEqual(path.read_text(),'Shortened paragraph.')
  _,state=self.turn('撤销刚才的操作');self.assertEqual(state['status'],'confirmation');self.turn('确认撤销');self.assertEqual(path.read_text(),'Original paragraph.')
 def test_confirmation_cancel_changes_nothing(self):
  path,target=self.preview();self.turn('取消');self.assertEqual(path.read_text(),'Original paragraph.');self.assertEqual(self.s.task(target)['state'],'cancelled')
 def test_confirmation_expiry_and_changed_file_never_write(self):
  path,target=self.preview()
  with self.s.db() as c:
   d=json.loads(c.execute('SELECT data FROM assistant_sessions WHERE id=?',(self.session,)).fetchone()[0]);d['pending']['expires']=time.time()-1;c.execute('UPDATE assistant_sessions SET data=? WHERE id=?',(packed(d),self.session))
  _,state=self.turn('确认');self.assertEqual(state['status'],'needs_input');self.assertEqual(path.read_text(),'Original paragraph.')
 def test_user_changed_original_is_not_overwritten(self):
  path,target=self.preview();path.write_text('New user content')
  with self.assertRaises(ValueError):self.turn('确认写回')
  self.assertEqual(path.read_text(),'New user content')
 def test_unclear_confirmation_can_be_answered_again(self):
  path,_=self.preview();_,state=self.turn('嗯我没听清');self.assertEqual(state['status'],'confirmation');self.turn('确认');self.assertEqual(path.read_text(),'Shortened paragraph.')
 def test_phone_candidates_second_choice_mode_and_receipt(self):
  _,state=self.turn('找附近餐厅');self.assertEqual(state['status'],'phone_action');a=state['phone_action'];r={'action_id':a['id'],'result':{'choices':[{'id':'p1','name':'A餐厅','detail':'约300米'},{'id':'p2','name':'B餐厅','detail':'约500米'}]}}
  self.s.sessions.receipt(self.owner,self.session,r);self.s.sessions.receipt(self.owner,self.session,r)
  _,state=self.turn('第二家，公交过去');self.assertEqual(state['phone_action']['place_id'],'p2');self.assertEqual(state['phone_action']['mode'],'transit')
  self.s.sessions.receipt(self.owner,self.session,{'action_id':state['phone_action']['id'],'result':{'accepted':True}});self.assertEqual(self.s.sessions.state(self.owner,self.session)['status'],'completed')
 def test_colloquial_nearby_food_uses_live_phone_search(self):
  for phrase in ('附近有什么可以吃的？','附近有什么好吃的','附近吃什么','请问附近有什么可以吃的？'):
   with self.subTest(phrase=phrase):
    _,state=self.turn(phrase)
    self.assertEqual(state['phone_action']['operation'],'search_places')
    self.assertTrue(state['phone_action']['nearby']);self.assertEqual(state['phone_action']['query'],'餐厅')
 def test_repeated_nearby_request_preserves_scope_and_original_text(self):
  for text in ('帮我找附近的餐厅。帮我找附近的餐厅。','帮我找附近的餐厅，帮我找附近的餐厅','帮我找附近的餐厅帮我找附近的餐厅','帮我看看附近的餐厅','找附近便宜的餐厅','找意大利餐厅','查一下附近有没有餐厅','帮忙查一下附近的餐厅'):
   with self.subTest(text=text):
    ident,state=self.turn(text)
    self.assertEqual(json.loads(self.s.task(ident)['body'])['text'],text)
    self.assertTrue(state['phone_action']['nearby'])
    self.assertEqual(state['phone_action']['operation'],'search_places')
    if text.count('帮我找附近的餐厅')>1:self.assertEqual(state['phone_action']['query'],'餐厅')
 def test_explicit_area_and_repeated_area_are_kept(self):
  for text in ('找香港科技大学附近的餐厅','找香港科技大学附近的餐厅。找香港科技大学附近的餐厅。','在尖沙咀找餐厅'):
   with self.subTest(text=text):
    _,state=self.turn(text);self.assertFalse(state['phone_action']['nearby'])
    self.assertTrue(any(x in state['phone_action']['query'] for x in ('香港科技大学','尖沙咀')))
 def test_nearby_candidates_require_bounded_distance(self):
  for detail in ('Japan','约11公里','约10001米'):
   _,state=self.turn('帮我找附近的餐厅。帮我找附近的餐厅。')
   self.s.sessions.receipt(self.owner,self.session,{'action_id':state['phone_action']['id'],'result':{'choices':[{'id':'invalid','name':'Unverified place','detail':detail}]}})
   state=self.s.sessions.state(self.owner,self.session)
   self.assertEqual(state['status'],'needs_input');self.assertIsNone(state['phone_action'])
   with self.s.db() as c:data=json.loads(c.execute('SELECT data FROM assistant_sessions WHERE id=?',(self.session,)).fetchone()[0])
   self.assertEqual(data['pending']['type'],'place_query')
 def test_repeated_request_then_second_transit_full_chain(self):
  _,state=self.turn('帮我找附近的餐厅。帮我找附近的餐厅。')
  self.assertTrue(state['phone_action']['nearby'])
  self.s.sessions.receipt(self.owner,self.session,{'action_id':state['phone_action']['id'],'result':{'choices':[{'id':'first','name':'A','detail':'约200米'},{'id':'second','name':'B','detail':'约1.6公里'}]}})
  _,state=self.turn('第二家，公交过去')
  self.assertEqual(state['phone_action']['place_id'],'second');self.assertEqual(state['phone_action']['mode'],'transit')
  self.s.sessions.receipt(self.owner,self.session,{'action_id':state['phone_action']['id'],'result':{'accepted':True}})
  self.assertEqual(self.s.sessions.state(self.owner,self.session)['status'],'completed')
 def test_nearby_retry_never_becomes_global_search(self):
  _,state=self.turn('找附近的餐厅')
  self.s.sessions.receipt(self.owner,self.session,{'action_id':state['phone_action']['id'],'result':{'error':'location_unavailable'}})
  _,state=self.turn('找附近的餐厅。')
  self.assertTrue(state['phone_action']['nearby']);self.assertEqual(state['phone_action']['query'],'餐厅')
 def test_failed_search_accepts_explicit_area_or_new_topic(self):
  _,state=self.turn('找附近的餐厅')
  self.s.sessions.receipt(self.owner,self.session,{'action_id':state['phone_action']['id'],'result':{'error':'location_unavailable'}})
  _,state=self.turn('香港科技大学附近')
  self.assertFalse(state['phone_action']['nearby']);self.assertEqual(state['phone_action']['query'],'香港科技大学附近 餐厅')
  self.s.sessions.receipt(self.owner,self.session,{'action_id':state['phone_action']['id'],'result':{'error':'map_timeout'}})
  _,state=self.turn('设备状态');self.assertEqual(state['status'],'completed');self.assertIn('可用空间',state['speech'])
 def test_new_nearby_query_replaces_prior_single_wrong_candidate(self):
  _,state=self.turn('找附近的餐厅')
  self.s.sessions.receipt(self.owner,self.session,{'action_id':state['phone_action']['id'],'result':{'choices':[{'id':'foreign-place','name':'Unrelated place','detail':'other country'}]}})
  _,state=self.turn('重新找附近的餐厅');self.assertEqual(state['phone_action']['operation'],'search_places');self.assertTrue(state['phone_action']['nearby'])
 def test_client_phases_are_owned_and_do_not_complete_task(self):
  ident,state=self.turn('找附近的餐厅')
  v={'task':ident,'stage':'playback_finished','version':'0.12.3'}
  self.s.sessions.client_event(self.owner,self.session,v)
  self.assertEqual(self.s.sessions.state(self.owner,self.session)['status'],'phone_action')
  for who,value in [(self.other,v),(self.owner,{**v,'task':str(uuid.uuid4())}),(self.owner,{**v,'stage':'arbitrary private text'}),(self.owner,{**v,'latitude':22})]:
   with self.assertRaises(TodoError):self.s.sessions.client_event(who,self.session,value)
 def test_phone_receipt_cannot_forge_transfer_success(self):
  _,state=self.turn('把这个文档传到Mini')
  with self.assertRaises(TodoError):self.s.sessions.receipt(self.owner,self.session,{'action_id':state['phone_action']['id'],'result':{'task_id':str(uuid.uuid4())}})
 def test_draft_followup_stays_draft_and_copies_exact_result(self):
  self.s.brain.transport=lambda *a,**k:io.BytesIO(json.dumps({'message':{'content':json.dumps({'draft':'好的，我会准时参加。'})}}).encode())
  self.turn('帮我回复这条消息：明天15点请来参加会议');self.turn('再自然一点');_,state=self.turn('复制到剪贴板');self.assertEqual(state['phone_action']['text'],'好的，我会准时参加。')
  with self.s.db() as c:self.assertEqual(c.execute('SELECT count(*) FROM calendar_entries').fetchone()[0],0)
 def test_general_memory_and_status_use_existing_tools(self):
  self.s.brain.transport=lambda *a,**k:io.BytesIO(json.dumps({'message':{'content':'已记住。'}}).encode())
  self.turn('记住，我喜欢安静的餐厅');self.assertTrue(self.s.brain.preferences(self.owner));_,state=self.turn('设备状态');self.assertIn('可用空间',state['speech'])
 def test_confirmation_survives_restart_but_cannot_replay(self):
  path,target=self.preview();self.s=Store(self.root/'state');self.s.mac=MacActions(self.s,lambda x:{'accepted':True},self.root/'Inbox')
  self.turn('确认写回');self.assertEqual(path.read_text(),'Shortened paragraph.')
  with self.s.db() as c:self.assertEqual(c.execute('SELECT count(*) FROM mac_receipts WHERE task=?',(target,)).fetchone()[0],1)
  self.assertEqual(self.s.sessions.state(self.owner,self.session)['status'],'completed')
 def test_removed_young_voice_cannot_be_selected_or_synthesized(self):
  self.assertEqual(self.s.speech.preferences(self.owner)['voices'],['system','Uncle_Fu'])
  with self.assertRaises(TodoError):self.s.speech.configure(self.owner,{'voice':'Aiden'})
  with self.assertRaises(TodoError):self.s.speech.request(self.owner,{'voice':'Aiden','text':'test'})
  with self.s.db() as c:c.execute('INSERT INTO speech_preferences VALUES(?,?)',(self.owner,'Aiden'))
  self.s.speech.recover();self.assertEqual(self.s.speech.preferences(self.owner)['voice'],'Uncle_Fu')
 def test_tts_restart_fails_memory_only_jobs_and_expires_audio(self):
  self.s.speech.folder.mkdir();ident='a'*32;self.s.speech.folder.joinpath(ident+'.wav').write_bytes(b'fixture')
  with self.s.db() as c:
   c.execute("INSERT INTO speech_outputs VALUES(?,?,?,?,?,?,?)",(ident,self.owner,'Uncle_Fu','hash','ready',time.time()-90000,'{}'))
   c.execute("INSERT INTO speech_outputs VALUES(?,?,?,?,?,?,?)",('b'*32,self.owner,'Uncle_Fu','hash2','queued',time.time(),'{}'))
  self.s.speech.recover();self.assertFalse((self.s.speech.folder/(ident+'.wav')).exists());self.assertEqual(self.s.speech.status(self.owner,'b'*32)['state'],'failed')
 def test_multiple_shared_documents_ask_before_editing(self):
  docs=[self.s.documents.upload(self.owner,{'name':name+'.txt','data':base64.b64encode(b'Original text').decode(),'request_id':name}) for name in ('one','two')]
  self.s.brain.transport=lambda *a,**k:io.BytesIO(json.dumps({'message':{'content':json.dumps({'changes':[{'index':0,'text':'Short text'}]})}}).encode())
  _,state=self.turn('请把这两份文档改写得简洁一点',{'input_refs':json.dumps({'document_ids':[d['id'] for d in docs]})});self.assertEqual(state['status'],'needs_input')
  self.turn('第二份');self.assertEqual(self.s.documents.info(self.owner,docs[0]['id'])['version'],1);self.assertEqual(self.s.documents.info(self.owner,docs[1]['id'])['version'],2)
 def test_organize_only_exact_named_grant_and_undo(self):
  folder=self.root/'资料盒';folder.mkdir();path=folder/'fixture.txt';path.write_text('text')
  with self.s.db() as c:c.execute('INSERT INTO roots VALUES(?)',(str(folder),))
  self.s.sessions.classify=lambda *a:{'kind':'organize','device':'mini','folder':'desktop','query':'资料盒','instruction':''}
  _,state=self.turn('整理资料盒里的文件');self.assertEqual(state['status'],'confirmation');self.assertTrue(path.exists())
  self.turn('确认整理');self.assertFalse(path.exists());self.turn('撤销刚才的整理');self.turn('确认撤销');self.assertEqual(path.read_text(),'text')
 def test_restart_after_write_recovers_receipt_without_reexecuting(self):
  path,target=self.preview();state=self.s.sessions.state(self.owner,self.session)
  child=self.s.submit({'text':'确认写回','session_id':self.session,'reply_to':state['task_id']},self.owner)
  with self.s.db() as c:
   d=json.loads(c.execute('SELECT data FROM assistant_sessions WHERE id=?',(self.session,)).fetchone()[0]);d['pending']['consumed']=True;c.execute('UPDATE assistant_sessions SET data=? WHERE id=?',(packed(d),self.session))
  self.s.act(target,'approve');self.s.update(child,state='needs_review',result='服务重启中断')
  self.s=Store(self.root/'state');state=self.s.sessions.state(self.owner,self.session)
  self.assertEqual(state['status'],'completed');self.assertEqual(path.read_text(),'Shortened paragraph.');self.assertTrue(any(a['event']=='voice_receipt_recovered' for a in self.s.audits(child)))
 def test_tts_defaults_to_system_and_is_owner_private(self):
  self.assertEqual(self.s.speech.preferences(self.owner)['voice'],'system');self.assertEqual(self.s.speech.request(self.owner,{'text':'测试'})['state'],'system')
  with self.assertRaises(TodoError):self.s.speech.status(self.other,'a'*32)
if __name__=='__main__':unittest.main()
