#!/usr/bin/env python3
"""Fresh-copy install and real HTTP/file smoke. No model, accounts, SSH or APNs network."""
import argparse,hashlib,json,os,pathlib,plistlib,re,shutil,socket,subprocess,sys,tempfile,time,urllib.error,urllib.request
ROOT=pathlib.Path(__file__).resolve().parents[1]

def main():
 parser=argparse.ArgumentParser(description=__doc__);parser.add_argument('--model',help='Optional already-installed Ollama tag for an actual local tool-call test; never downloaded');a=parser.parse_args()
 with tempfile.TemporaryDirectory(prefix='personalos-clean-install-') as folder:
  temp=pathlib.Path(folder).resolve();repo=temp/'checkout with spaces';state=temp/'private state'
  shutil.copytree(ROOT,repo,ignore=shutil.ignore_patterns('.git','build','DerivedData','__pycache__','.venv','venv'))
  cli=repo/'scripts/personalos.py'
  def run(*args):return subprocess.run([sys.executable,str(cli),'--home',str(state),*args],cwd=repo,check=True,capture_output=True,text=True).stdout
  result=json.loads(run('init',*(['--model',a.model] if a.model else [])));assert result['downloads_started'] is False
  manifest=plistlib.loads(run('service-install','--dry-run').encode());assert manifest['Label']=='org.personalosassistant.agent';assert str(state) in manifest['ProgramArguments']
  assert (state/'local-model.json').stat().st_mode&0o077==0
  selected=temp/'selected';selected.mkdir();(selected/'example.txt').write_text('synthetic document\n')
  run('grant',str(selected))
  with socket.socket() as sock:sock.bind(('127.0.0.1',0));port=sock.getsockname()[1]
  process=subprocess.Popen([sys.executable,str(cli),'--home',str(state),'--port',str(port),'serve'],cwd=repo,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
  base='http://127.0.0.1:'+str(port)
  def api(path,body=None,token=''):
   req=urllib.request.Request(base+path,None if body is None else json.dumps(body,ensure_ascii=False).encode(),{'Content-Type':'application/json','Authorization':'Bearer '+token})
   with urllib.request.urlopen(req,timeout=4) as r:return json.load(r)
  def wait(ident,token,wanted):
   for _ in range(1500 if a.model else 100):
    value=api('/tasks/'+ident,token=token)
    if value['state'] in wanted:return value
    time.sleep(.1)
   raise AssertionError('Task did not finish')
  try:
   for _ in range(100):
    try:
     with urllib.request.urlopen(base,timeout=1) as response:assert response.status==200
     break
    except OSError:time.sleep(.05)
   else:raise AssertionError('Server did not start')
   tokens=[]
   for name in ('Synthetic A','Synthetic B'):
    code=run('pair').strip().split('：')[-1];tokens.append(api('/pair',{'code':code,'name':name})['token'])
   if a.model:
    ident=api('/tasks',{'text':'请查看本机的磁盘空间还剩多少，只说结果。'},tokens[0])['id'];value=wait(ident,tokens[0],{'completed','failed'})
    assert value['state']=='completed',value['result']
    tools=json.loads(value['plan']).get('brain',{}).get('tools',[])
    assert any(x.get('tool')=='device_status' and x.get('ok') for x in tools),'No verified real device tool receipt: '+value['result'][:300]
   body={'text':'设备状态','request_id':'smoke-once'}
   ident=api('/tasks',body,tokens[0])['id'];assert api('/tasks',body,tokens[0])['id']==ident
   value=wait(ident,tokens[0],{'completed','failed'});assert value['state']=='completed';assert json.loads(value['result'])['free_disk_gb']>=0
   assert not api('/tasks',token=tokens[1]);assert not api('/audit',token=tokens[1])
   try:api('/tasks/'+ident,token=tokens[1])
   except urllib.error.HTTPError as e:assert e.code==404
   else:raise AssertionError('Cross-device task access')
   ident=api('/tasks',{'text':'整理选定文件','root':str(selected)},tokens[0])['id'];assert wait(ident,tokens[0],{'awaiting_confirmation','failed'})['state']=='awaiting_confirmation'
   api('/tasks/'+ident+'/approve',{},tokens[0]);assert (selected/'PersonalOS-txt/example.txt').read_text()=='synthetic document\n'
   api('/tasks/'+ident+'/undo',{},tokens[0]);assert (selected/'example.txt').read_text()=='synthetic document\n'
   ident=api('/tasks',{'text':'明天15:00到16:00，安排示例会议','time_zone':'UTC'},tokens[0])['id'];value=wait(ident,tokens[0],{'completed','needs_details','failed'});assert value['state']=='completed',value['state']
   entry=api('/calendar/entries',token=tokens[0])['entries'][0];assert entry['device_sync']=='pending'
   assert json.loads(value['plan'])['outcome']['status']=='waiting_calendar'
   api('/calendar/entries/'+entry['id']+'/ack',{'revision':str(entry['revision']),'status':'written'},tokens[0])
   assert json.loads(api('/tasks/'+ident,token=tokens[0])['plan'])['outcome']['status']=='calendar_synced'
   report={'passed':True,'fresh_copy_with_spaces':True,'base_install_no_downloads':True,'login_service_manifest_only':True,'real_authenticated_loopback_http':True,'two_device_isolation':True,'request_retry_idempotent':True,'real_file_move_and_undo':True,'calendar_ack_protocol_synthetic':True,'physical_iphone_test':False,'model_inference_test':bool(a.model)}
  finally:
   process.terminate()
   try:process.wait(timeout=5)
   except subprocess.TimeoutExpired:process.kill();process.wait()
 print(json.dumps(report,indent=2))
if __name__=='__main__':main()
