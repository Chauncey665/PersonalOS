"""Offline JSON-lines worker. Private text travels through stdin, never arguments/logs."""
import json,os,pathlib,sys,time,contextlib
os.environ['HF_HUB_OFFLINE']='1';os.environ['TRANSFORMERS_OFFLINE']='1';os.environ['HF_HUB_DISABLE_IMPLICIT_TOKEN']='1'
def main():
 import soundfile as sf
 from mlx_audio.tts.utils import load_model
 import mlx.core as mx
 model_path=pathlib.Path(sys.argv[1]);output=pathlib.Path(sys.argv[2]);output.mkdir(parents=True,exist_ok=True,mode=0o700)
 with contextlib.redirect_stdout(sys.stderr):model=load_model(str(model_path))
 for line in sys.stdin:
  try:
   value=json.loads(line)
   if set(value)!={'id','text','speaker','language'} or value['speaker'] not in ('Uncle_Fu',) or not isinstance(value['text'],str) or not 0<len(value['text'])<=650 or not __import__('re').fullmatch('[0-9a-f]{32}',value['id']):raise ValueError('invalid_request')
   started=time.monotonic();chunks=[];first=None
   with contextlib.redirect_stdout(sys.stderr):
    for part in model.generate_custom_voice(text=value['text'],speaker=value['speaker'],language=value['language'],instruct='语气沉稳自然，清晰简洁，不要夸张，不要播音腔。',stream=False,max_tokens=1800):
     if first is None:first=time.monotonic()-started
     mx.eval(part.audio);chunks.append(part.audio);rate=part.sample_rate
   audio=mx.concatenate(chunks);mx.eval(audio);target=output/(value['id']+'.wav');sf.write(str(target),__import__('numpy').array(audio),rate,subtype='PCM_16');target.chmod(0o600)
   print(json.dumps({'ok':True,'id':value['id'],'sample_rate':rate,'seconds':len(audio)/rate,'first_chunk_seconds':round(first,3),'elapsed_seconds':round(time.monotonic()-started,3),'peak_memory_gb':round(mx.get_peak_memory()/2**30,3)}),flush=True)
  except Exception as error:print(json.dumps({'ok':False,'code':type(error).__name__}),flush=True)
if __name__=='__main__':main()
