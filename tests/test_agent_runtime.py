import io,json,pathlib,sys,tempfile,unittest
from unittest.mock import patch
sys.path.insert(0,str(pathlib.Path(__file__).resolve().parents[1]/'server'))
from agent_runtime import ToolSurface,ProgressGuard,compile_context
from assistant_brain import AssistantBrain,TOOLS,SYSTEM
from personal_os import Store

def reply(calls=None,text='已核对结果。'):
 return {'message':{'role':'assistant','content':text,'tool_calls':[{'function':{'name':n,'arguments':a}} for n,a in (calls or [])]}}
class RuntimeTests(unittest.TestCase):
 def setUp(self):
  self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup);self.s=Store(pathlib.Path(self.temp.name));self.o='runtime-test'
  with self.s.db() as c:c.execute('INSERT INTO devices VALUES(?,?)',(self.o,'test'))
 def brain(self,text,replies):
  requests=[];it=iter(replies)
  def transport(req,**kw):
   requests.append(json.loads(req.data));return io.BytesIO(json.dumps(next(it),ensure_ascii=False).encode())
  brain=AssistantBrain(self.s,'test-local:latest',transport);row=self.s.task(self.s.submit({'text':text},self.o));return brain,row,requests
 def test_device_receipt_uses_request_timezone(self):
  brain,row,_=self.brain('核对磁盘',[])
  body=json.loads(row['body']);body['time_zone']='Asia/Hong_Kong';row['body']=json.dumps(body)
  result=brain.execute_tool('device_status',{},row,{})
  self.assertEqual(result['time_zone'],'Asia/Hong_Kong');self.assertTrue(result['checked_at'].endswith('+08:00'))
 def test_simple_question_reduces_schema_and_keeps_discovery(self):
  surface=ToolSurface(TOOLS,'你好，你怎么看这件事？');names={x['function']['name'] for x in surface.schemas()}
  self.assertIn('load_tools',names);self.assertNotIn('edit_document',names);self.assertNotIn('record_todos',names)
  self.assertLess(len(json.dumps(surface.schemas())),len(json.dumps(TOOLS))*.5)
 def test_unavailable_tools_stay_hidden_after_group_loading(self):
  surface=ToolSurface(TOOLS,'查找资料',unavailable={'files','prepare_organization'})
  surface.load({'group':'files'});names={x['function']['name'] for x in surface.schemas()}
  self.assertNotIn('files',names);self.assertNotIn('prepare_organization',names);self.assertIn('search_documents',names)
 def test_group_load_does_not_expand_document_or_live_authorization(self):
  for surface in [ToolSurface(TOOLS,'修改文档',documents=True),ToolSurface(TOOLS,'天气如何',readonly=True)]:
   with self.assertRaises(ValueError):surface.load({'group':'files'})
   with self.assertRaises(ValueError):surface.load({'group':'memory'})
 def test_loaded_tools_only_become_callable_in_next_round(self):
  brain,row,requests=self.brain('先核对这些资料再回答我',[
   reply([('load_tools',{'group':'tasks'}),('plan_todos',{})]),
   reply([('plan_todos',{})]),reply(text='排程结果需要手机同步。')])
  with patch.object(self.s.planner,'reconcile',return_value={'slots':[]}) as plan:result=brain.run(row)
  self.assertEqual(plan.call_count,1);self.assertIn('tool_not_exposed',json.dumps(requests[1]))
  self.assertIn('plan_todos',{x['function']['name'] for x in requests[1]['tools']})
 def test_same_failed_call_is_not_reexecuted_and_final_round_is_answer_only(self):
  brain,row,requests=self.brain('查找文件资料',[
   reply([('files',{'operation':'list','device':'mini','path':'/not-granted'})]),
   reply([('files',{'operation':'list','device':'mini','path':'/not-granted'})]),
   reply([('files',{'operation':'list','device':'mini','path':'/not-granted'})]),reply(text='读取失败，没有完成文件操作。')])
  with patch.object(brain,'execute_tool',side_effect=ValueError('未授权目录')) as execute, patch.object(self.s.file_access,'devices',return_value={'devices':[{'id':'mini','available':True}]}):result=brain.run(row)
  self.assertEqual(execute.call_count,1);self.assertNotIn('tools',requests[-1]);self.assertFalse(requests[-1]['think'])
  self.assertIn('读取失败',result['result']);self.assertEqual(json.loads(result['plan'])['brain']['no_progress_reuses'],2)
 def test_answer_only_round_cannot_execute_hallucinated_call(self):
  replies=[reply([('device_status',{})])]*4
  brain,row,requests=self.brain('核对设备运行情况',replies)
  with patch.object(brain,'execute_tool',return_value={'available':True}) as execute:result=brain.run(row)
  self.assertEqual(execute.call_count,1);self.assertIn('未能完成',result['result'])
 def test_ping_pong_cache_uses_canonical_arguments(self):
  guard=ProgressGuard();calls=[]
  for name,args in [('a',{'x':1,'y':2}),('b',{}),('a',{'y':2,'x':1}),('b',{})]:guard.run(name,args,lambda:(calls.append(name) or {'ok':True}))
  self.assertEqual(calls,['a','b']);self.assertTrue(guard.force_answer)
 def test_changed_arguments_are_real_progress(self):
  guard=ProgressGuard();calls=[]
  for page in range(4):guard.run('read',{'page':page},lambda:(calls.append(page) or {'ok':True}))
  self.assertEqual(calls,[0,1,2,3]);self.assertFalse(guard.force_answer)
 def test_large_calendar_does_not_erase_recent_turn_or_current_choice(self):
  context={'request':'第二家，公交过去','conversation_state':{'choices':[{'index':2,'name':'海湾小馆'}]},'calendar_context':{'events':['x'*500]*50},'preferences':[],'task_inventory':{'items':[{'title':'作业'+str(x),'notes':'n'*220} for x in range(30)]}}
  past=[{'id':str(x),'text':'关于之前那个项目'+str(x),'result':'核对结果'+str(x),'state':'completed','due':None} for x in range(12)]
  messages,stats=compile_context(context,past,SYSTEM,ToolSurface(TOOLS,'第二家，公交过去').schemas(),8192)
  body=json.loads(messages[1]['content']);self.assertEqual(body['conversation_state']['choices'][0]['name'],'海湾小馆')
  self.assertEqual(messages[-1]['content'],'第二家，公交过去');self.assertGreater(stats['history_turns'],0);self.assertIn('today_calendar',body['calendar_context']['reason']);self.assertTrue(body['task_inventory']['truncated'])
 def test_long_single_recent_reply_has_excerpt_without_deleting_older_short_turns(self):
  context={'request':'刚才提到的项目叫什么？','preferences':[],'conversation_state':{}}
  past=[{'id':'new','text':'项目青岚','result':'很长的结果'*2000,'state':'completed','due':None},{'id':'old','text':'项目叫青岚','result':'已核对这个名字','state':'completed','due':None}]
  messages,stats=compile_context(context,past,SYSTEM,ToolSurface(TOOLS,'你好').schemas(),8192)
  self.assertGreater(stats['history_excerpts'],0);self.assertGreater(stats['history_turns'],0);self.assertIn('青岚',json.dumps(messages,ensure_ascii=False))
if __name__=='__main__':unittest.main()
