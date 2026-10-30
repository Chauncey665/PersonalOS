#!/usr/bin/env python3
"""Generate unsigned workflows for your bundle ID; optional signing uses YOUR Mac identity."""
import argparse,pathlib,plistlib,re,subprocess
ROOT=pathlib.Path(__file__).resolve().parents[1]
def replace(value,bundle,team):
 if isinstance(value,str):return value.replace('org.personalosassistant.app',bundle)
 if isinstance(value,list):return [replace(x,bundle,team) for x in value]
 if isinstance(value,dict):return {k:(team if k=='TeamIdentifier' else replace(v,bundle,team)) for k,v in value.items()}
 return value

def main():
 p=argparse.ArgumentParser(description=__doc__);p.add_argument('--bundle-id',default='org.personalosassistant.app');p.add_argument('--team-id',required=True);p.add_argument('--output',type=pathlib.Path,default=ROOT/'build/shortcuts');p.add_argument('--sign',action='store_true');a=p.parse_args()
 if not re.fullmatch(r'[A-Za-z][A-Za-z0-9-]*(?:\.[A-Za-z0-9-]+){2,}',a.bundle_id):p.error('Use the exact reverse-DNS bundle ID selected in Xcode')
 if not re.fullmatch(r'[A-Z0-9]{10}',a.team_id):p.error('Use your own ten-character Xcode Team ID')
 a.output.mkdir(parents=True,exist_ok=True)
 for source in (ROOT/'shortcuts').glob('*.plist'):
  data=replace(plistlib.loads(source.read_bytes()),a.bundle_id,a.team_id);target=a.output/(source.stem+'-unsigned.shortcut');target.write_bytes(plistlib.dumps(data,fmt=plistlib.FMT_BINARY))
  if a.sign:subprocess.run(['shortcuts','sign','--mode','anyone','--input',str(target),'--output',str(a.output/(source.stem+'.shortcut'))],check=True)
  print(source.stem+(': signed locally' if a.sign else ': unsigned; sign on your own Mac before iPhone import'))
if __name__=='__main__':main()
