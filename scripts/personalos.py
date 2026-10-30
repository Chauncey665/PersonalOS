#!/usr/bin/env python3
"""Portable local setup for PersonalOS. Never downloads models or changes TCC/network policy."""
import argparse,json,os,pathlib,plistlib,shutil,socket,subprocess,sys,urllib.request
ROOT=pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'server'))
from local_model_config import settings
from deployment_config import validate_canvas_base
LABEL='org.personalosassistant.agent'
DEFAULT_HOME=pathlib.Path.home()/'Library/Application Support/PersonalOS-OpenSource'

def home(value):
 p=pathlib.Path(value).expanduser()
 if p.is_symlink() or p.resolve() in (pathlib.Path('/'),pathlib.Path.home()):raise ValueError('Use a dedicated, non-symlink PersonalOS data directory')
 p.mkdir(mode=0o700,parents=True,exist_ok=True);p.chmod(0o700);return p.resolve()

def private_json(p,value,replace=False):
 if p.is_symlink():raise ValueError('Refusing symlink configuration')
 if p.exists() and not replace:return
 temporary=p.with_name('.'+p.name+'.'+os.urandom(8).hex())
 fd=os.open(temporary,os.O_CREAT|os.O_EXCL|os.O_WRONLY,0o600)
 try:
  with os.fdopen(fd,'w') as f:json.dump(value,f,ensure_ascii=False,indent=2);f.write('\n')
  os.replace(temporary,p)
 finally:temporary.unlink(missing_ok=True)

def initialize(target,model,build_helper=False):
 from local_model_config import _model
 _model(model)
 private_json(target/'local-model.json',{'model':model,'num_ctx':32768,'keep_alive':60})
 private_json(target/'dialogue-engine.json',{'enabled':True})
 runtime=target/'runtime'
 if runtime.is_symlink():raise ValueError('Refusing symlink runtime')
 runtime.mkdir(mode=0o700,exist_ok=True)
 for p in list((ROOT/'server').glob('*.py'))+[ROOT/'server/dashboard.html']:
  dest=runtime/p.name
  if dest.is_symlink():raise ValueError('Refusing symlink runtime file')
  shutil.copy2(p,dest);dest.chmod(0o600)
 if build_helper:
  if sys.platform!='darwin':raise ValueError('The native Mac helper requires macOS and Xcode')
  subprocess.run(['xcrun','swiftc','-swift-version','5','-module-cache-path',str(target/'swift-cache'),'-parse-as-library',str(ROOT/'mac/MacActions.swift'),'-o',str(runtime/'personalos-mac-actions')],check=True)
  (runtime/'personalos-mac-actions').chmod(0o700)
 from personal_os import Store
 Store(target)
 return {'initialized':True,'model':settings(target)['model'],'runtime_copied':True,'helper_built':build_helper,'downloads_started':False}

def manifest(target,port):
 return {'Label':LABEL,'ProgramArguments':[sys.executable,str(target/'runtime/personal_os.py'),'--home',str(target),'serve','--port',str(port)],'RunAtLoad':True,'KeepAlive':True,'ThrottleInterval':10,'WorkingDirectory':str(target/'runtime'),'StandardOutPath':str(target/'service.log'),'StandardErrorPath':str(target/'service-error.log'),'Umask':63}

def doctor(target,port):
 model=settings(target)['model'];ollama=False;present=False
 try:
  with urllib.request.urlopen('http://127.0.0.1:11434/api/tags',timeout=3) as r:tags=json.load(r)
  ollama=True;present=any(x.get('name')==model or x.get('model')==model for x in tags.get('models',[]))
 except (OSError,ValueError):pass
 with socket.socket() as s:busy=s.connect_ex(('127.0.0.1',port))==0
 return {'python':sys.version.split()[0],'macos':sys.platform=='darwin','xcrun_available':bool(shutil.which('xcrun')),'ollama_reachable':ollama,'configured_model':model,'model_installed':present,'port_in_use':busy,'runtime_ready':(target/'runtime/personal_os.py').is_file(),'voice_ready':(target/'voice.json').is_file(),'push_configured':(target/'push.json').is_file(),'note':'Presence checks are not permission, signature, phone or APNs delivery acceptance.'}

def main():
 p=argparse.ArgumentParser(description=__doc__);p.add_argument('--home',default=str(DEFAULT_HOME));p.add_argument('--port',type=int,default=8765)
 sub=p.add_subparsers(dest='command',required=True)
 init=sub.add_parser('init');init.add_argument('--model',default='qwen3.5:9b');init.add_argument('--build-helper',action='store_true')
 nickname=sub.add_parser('name');nickname.add_argument('assistant_name')
 sub.add_parser('doctor');sub.add_parser('serve');sub.add_parser('pair');sub.add_parser('revoke')
 grant=sub.add_parser('grant');grant.add_argument('path')
 sub.add_parser('files-enable');sub.add_parser('files-disable')
 canvas=sub.add_parser('configure-canvas');canvas.add_argument('origin')
 service=sub.add_parser('service-install');service.add_argument('--dry-run',action='store_true')
 sub.add_parser('service-uninstall')
 a=p.parse_args()
 try:
  if not 1<=a.port<=65535:raise ValueError('Port must be 1..65535')
  target=home(a.home)
  if a.command=='init':print(json.dumps(initialize(target,a.model,a.build_helper),indent=2))
  elif a.command=='name':
   from assistant_identity import valid_name
   private_json(target/'assistant-profile.json',{'name':valid_name(a.assistant_name)},replace=True);print('Assistant name saved. Restart the service before new conversations.')
  elif a.command=='doctor':print(json.dumps(doctor(target,a.port),indent=2))
  elif a.command=='configure-canvas':
   private_json(target/'integrations.json',{'canvas_base_url':validate_canvas_base(a.origin)},replace=True);print('Canvas origin saved. Restart PersonalOS, then connect in the App; no token is needed in this command.')
  elif a.command=='service-uninstall':
   if sys.platform!='darwin':raise ValueError('LaunchAgent requires macOS')
   subprocess.run(['launchctl','bootout','gui/'+str(os.getuid())+'/'+LABEL],capture_output=True)
   path=pathlib.Path.home()/'Library/LaunchAgents'/str(LABEL+'.plist')
   if path.is_symlink():raise ValueError('Refusing symlink LaunchAgent')
   path.unlink(missing_ok=True);print('PersonalOS login service removed; data preserved.')
  elif a.command=='service-install':
   value=manifest(target,a.port)
   if a.dry_run:print(plistlib.dumps(value).decode());return
   if sys.platform!='darwin':raise ValueError('LaunchAgent requires macOS')
   with socket.socket() as s:
    if s.connect_ex(('127.0.0.1',a.port))==0:raise ValueError('Port is already in use. Stop your foreground PersonalOS or choose another port; nothing was stopped.')
   initialize(target,settings(target)['model'])
   path=pathlib.Path.home()/'Library/LaunchAgents'/str(LABEL+'.plist');path.parent.mkdir(exist_ok=True)
   if path.is_symlink():raise ValueError('Refusing symlink LaunchAgent')
   fd=os.open(path,os.O_CREAT|os.O_TRUNC|os.O_WRONLY,0o600)
   with os.fdopen(fd,'wb') as f:f.write(plistlib.dumps(value))
   subprocess.run(['launchctl','bootstrap','gui/'+str(os.getuid()),str(path)],check=True)
   print('PersonalOS login service installed. Keep this Python interpreter available. Check doctor and service-error.log.')
  else:
   args=[sys.executable,str(ROOT/'server/personal_os.py'),'--home',str(target),a.command]
   if a.command=='serve':args+=['--port',str(a.port)]
   if a.command=='grant':args+=[a.path]
   raise SystemExit(subprocess.call(args))
 except (ValueError,OSError,subprocess.SubprocessError) as e:p.exit(1,'PersonalOS: '+str(e)+'\n')
if __name__=='__main__':main()
