#!/usr/bin/env python3
"""Explicit optional Qwen ASR setup; never run by a voice request or base install."""
import argparse,importlib.util,json,os,pathlib,platform,subprocess,sys
ROOT=pathlib.Path(__file__).resolve().parents[1];sys.path[:0]=[str(ROOT),str(ROOT/'server')]
from transcribe_qwen import MODEL_REPO,MODEL_REVISION,DATA_FILES,validate_model
import setup_local_speech as download

def main():
 p=argparse.ArgumentParser(description=__doc__);p.add_argument('--home',type=pathlib.Path,default=pathlib.Path.home()/'Library/Application Support/PersonalOS-OpenSource');p.add_argument('--phase',choices=['all','packages','model'],default='all');a=p.parse_args()
 if sys.platform!='darwin' or platform.machine()!='arm64':p.error('MLX ASR requires Apple Silicon macOS')
 from personalos import home,private_json
 target=home(a.home);venv=target/'speech-qwen-venv';model=target/'models/qwen3-asr-1.7b-8bit'
 if a.phase in ('all','packages'):
  if not (venv/'bin/python').exists():subprocess.run([sys.executable,'-m','venv',str(venv)],check=True)
  subprocess.run([str(venv/'bin/python'),'-m','pip','install','mlx-audio==0.6.2','soundfile==0.13.1','opencc-python-reimplemented==0.1.7'],check=True)
 if a.phase in ('all','model'):
  download.REPO=MODEL_REPO;download.REVISION=MODEL_REVISION;download.MODEL=model;download.MODEL_FILES={x:x for x in DATA_FILES}
  download.setup_model();validate_model(model)
  if not (venv/'bin/python').exists():raise ValueError('Install the packages phase before enabling voice')
  private_json(target/'voice.json',{'enabled':True,'engine':'qwen3','model_path':str(model)},replace=True)
  print('Pinned Qwen ASR validated and enabled. Restart PersonalOS; microphone permission is still granted on the iPhone.')
if __name__=='__main__':main()
