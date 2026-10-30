#!/usr/bin/env python3
"""Check publishable source, not the user's runtime directory. Never print matched secrets."""
import json,pathlib,plistlib,re,subprocess,sys
ROOT=pathlib.Path(__file__).resolve().parents[1]
EXCLUDED={'.git','.venv','venv','__pycache__','build','DerivedData','.build'}
FORBIDDEN_SUFFIXES={'.p8','.p12','.pem','.key','.sqlite','.sqlite3','.db','.ipa','.mobileprovision','.wav','.m4a','.log','.pyc'}
PATTERNS=[('private_tailnet',re.compile(rb'[\w-]+\.tail[0-9a-f]{4,}\.ts\.net',re.I)),('device_udid',re.compile(rb'\b[0-9A-F]{8}-[0-9A-F]{16}\b')),('github_token',re.compile(rb'gh[pousr]_[A-Za-z0-9]{32,}')),('private_key',re.compile(rb'-----BEGIN (?:EC |RSA |OPENSSH )?PRIVATE KEY-----\s+[A-Za-z0-9+/=\r\n]{80,}-----END (?:EC |RSA |OPENSSH )?PRIVATE KEY-----'))]

def files():
 if (ROOT/'.git').exists():
  p=subprocess.run(['git','ls-files','-z'],cwd=ROOT,capture_output=True,check=True)
  if p.stdout:return [ROOT/pathlib.Path(x.decode()) for x in p.stdout.split(b'\0') if x]
 return [p for p in ROOT.rglob('*') if p.is_file() and not any(x in EXCLUDED for x in p.relative_to(ROOT).parts)]

def main():
 errors=[];paths=files()
 for p in paths:
  rel=p.relative_to(ROOT)
  if p.is_symlink():errors.append((str(rel),'symlink'));continue
  if p.suffix in FORBIDDEN_SUFFIXES or any(x in {'xcuserdata','models','backups','ui-review','samples','runtime','state'} for x in rel.parts):errors.append((str(rel),'private_or_generated_file'));continue
  data=p.read_bytes()
  if p.suffix=='.png':
   offset=8
   while offset<len(data):
    n=int.from_bytes(data[offset:offset+4],'big');tag=data[offset+4:offset+8]
    if tag in (b'tEXt',b'iTXt',b'zTXt',b'eXIf'):errors.append((str(rel),'png_metadata'))
    offset+=n+12
  for label,pattern in PATTERNS:
   if pattern.search(data):errors.append((str(rel),label))
 project=(ROOT/'ios/PersonalOS.xcodeproj/project.pbxproj').read_text()
 if re.search(r'DEVELOPMENT_TEAM\s*=\s*(?!"";)[A-Za-z0-9]{10}\s*;',project):errors.append(('ios/project','signing_team'))
 app=(ROOT/'ios/PersonalOS/PersonalOSApp.swift').read_text()
 if 'static let defaultServer = ""' not in app:errors.append(('ios/app','server_must_start_empty'))
 for p in (ROOT/'shortcuts').glob('*.plist'):
  data=plistlib.loads(p.read_bytes())
  if not data.get('WFWorkflowActions'):errors.append((p.name,'empty_workflow'))
  def check_team(v):
   if isinstance(v,dict):
    if v.get('TeamIdentifier'):errors.append((p.name,'personal_shortcut_team'))
    for x in v.values():check_team(x)
   elif isinstance(v,list):
    for x in v:check_team(x)
  check_team(data)
  for action in data['WFWorkflowActions']:
   if not isinstance(action.get('WFWorkflowActionIdentifier'),str):errors.append((p.name,'invalid_action'))
 for required in ['README.md','README.zh-CN.md','docs/INSTALL.md','docs/INSTALL.zh-CN.md','LICENSE','SECURITY.md','THIRD_PARTY_NOTICES.md']:
  if not (ROOT/required).is_file():errors.append((required,'missing_document'))
 # Relative Markdown links must resolve in a fresh checkout.
 for p in ROOT.rglob('*.md'):
  if any(x in EXCLUDED for x in p.relative_to(ROOT).parts):continue
  for target in re.findall(r'\]\(([^)]+)\)',p.read_text()):
   if re.match(r'\w+://|mailto:|#',target):continue
   target=target.split('#')[0]
   if target and not (p.parent/target).exists():errors.append((str(p.relative_to(ROOT)),'broken_relative_link'))
 print(json.dumps({'source_files_checked':len(paths),'passed':not errors,'findings':[{'file':p,'kind':k} for p,k in errors]},ensure_ascii=False,indent=2))
 return int(bool(errors))
if __name__=='__main__':raise SystemExit(main())
