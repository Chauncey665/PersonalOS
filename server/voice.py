"""Private, persistent local audio queue. Never reads global configuration on import."""
import base64
import binascii
import hashlib
import json
import os
import pathlib
import re
import selectors
import shutil
import signal
import stat
import subprocess
import time
import uuid
from calendar_journal import validate_zone, DEFAULT_ZONE
from transcript_quality import transcript_problem

MAX_AUDIO_BYTES = 8 * 1024 * 1024
MAX_BASE64_BYTES = 4 * ((MAX_AUDIO_BYTES + 2) // 3)
MAX_BODY_BYTES = MAX_BASE64_BYTES + 65536
MAX_SECONDS = 90
MAX_PENDING = 20
MODEL_DIRECTORY = 'models/whisper-large-v3-turbo-mlx'
QWEN_MODEL_DIRECTORY = 'models/qwen3-asr-1.7b-8bit'
MIMES = {'audio/mp4':'m4a', 'audio/m4a':'m4a', 'audio/x-m4a':'m4a', 'audio/wav':'wav', 'audio/x-wav':'wav', 'audio/wave':'wav'}
ERRORS = {
    'voice_unavailable':'Mac 的本地语音识别尚未启用或配置不完整。',
    'invalid_audio':'无法识别录音，请提交 M4A 或 WAV 文件。',
    'audio_too_large':'录音超过 8 MiB，请缩短后重试。',
    'audio_too_long':'录音超过 90 秒，请分成较短的任务。',
    'no_speech':'录音中没有清晰的任务，请重新录制。',
    'voice_timeout':'本地语音识别超时，请缩短录音后重试。',
    'voice_failed':'本地语音识别未完成；未执行任务，请重新录制。',
    'voice_interrupted':'服务重启中断语音识别；未执行任务，请重新提交录音。',
    'queue_full':'等待识别的语音任务过多，请稍后重试。',
    'unreliable_transcript':'识别结果出现异常重复，未执行任务。请重新录音说一遍。',
}


class VoiceError(ValueError):
    def __init__(self, code, status=400):
        self.code, self.status = code, status
        self.message = ERRORS.get(code, ERRORS['voice_failed'])
        super().__init__(self.message)


def _regular(path, private=False):
    info = path.lstat()
    return stat.S_ISREG(info.st_mode) and (not private or not info.st_mode & 0o077)


def bounded_transcribe(argv, directory, timeout=150, limit=65536):
    """One fixed offline subprocess, bounded diagnostics, killed as a process group."""
    environment = {'PATH':'/usr/bin:/bin', 'HOME':str(directory), 'TMPDIR':str(directory),
                   'LANG':'en_US.UTF-8', 'PYTHONNOUSERSITE':'1', 'HF_HUB_OFFLINE':'1',
                   'TRANSFORMERS_OFFLINE':'1', 'HF_HUB_DISABLE_TELEMETRY':'1', 'DO_NOT_TRACK':'1'}
    process = subprocess.Popen(argv, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                               stderr=subprocess.PIPE, env=environment, start_new_session=True)
    streams = selectors.DefaultSelector(); output = {'stdout':bytearray(), 'stderr':bytearray()}
    try:
        for stream, name in ((process.stdout,'stdout'),(process.stderr,'stderr')):
            os.set_blocking(stream.fileno(),False); streams.register(stream,selectors.EVENT_READ,name)
        end = time.monotonic() + timeout
        while streams.get_map():
            remaining = end-time.monotonic()
            if remaining <= 0: raise VoiceError('voice_timeout',408)
            for key, _ in streams.select(min(remaining,.25)):
                block = os.read(key.fileobj.fileno(),8192)
                if not block: streams.unregister(key.fileobj); continue
                output[key.data].extend(block)
                if sum(map(len,output.values())) > limit: raise VoiceError('voice_failed')
        remaining = end-time.monotonic()
        if remaining <= 0: raise VoiceError('voice_timeout',408)
        try: returncode = process.wait(timeout=remaining)
        except subprocess.TimeoutExpired: raise VoiceError('voice_timeout',408) from None
        if returncode:
            try: code = json.loads(output['stderr']).get('code')
            except (ValueError,TypeError,AttributeError): code = None
            raise VoiceError(code if code in {'invalid_audio','audio_too_large','audio_too_long','no_speech'} else 'voice_failed')
        try:
            result = json.loads(output['stdout'])
            text, language = result['text'], result.get('language','')
            if not isinstance(text,str) or not 0 < len(text.strip()) <= 4000: raise ValueError()
            if not isinstance(language,str) or not re.fullmatch(r'[A-Za-z-]{0,20}',language): raise ValueError()
            return {'text':text.strip(),'language':language}
        except (ValueError,TypeError,KeyError): raise VoiceError('voice_failed') from None
    finally:
        try: os.killpg(process.pid,signal.SIGKILL)
        except ProcessLookupError: pass
        process.wait(); streams.close(); process.stdout.close(); process.stderr.close()


class VoiceService:
    def __init__(self, store, runner=None):
        self.store = store; self.home = store.home
        self.config_path = self.home/'voice.json'; self.directory = self.home/'voice'
        self.engine = 'whisper'
        try:
            if _regular(self.config_path,True) and self.config_path.stat().st_size <= 4096:
                config = json.loads(self.config_path.read_text())
                if isinstance(config,dict) and config.get('engine') == 'qwen3': self.engine = 'qwen3'
        except (OSError,ValueError,TypeError): pass
        self.python = self.home/'speech-venv/bin/python'
        self.script = self.home/'runtime/transcribe_local.py'
        self.model = self.home/MODEL_DIRECTORY
        self.required_model_files = ('config.json','weights.safetensors','personalos-model-manifest.json')
        if self.engine == 'qwen3':
            self.python = self.home/'speech-qwen-venv/bin/python'
            self.script = self.home/'runtime/transcribe_qwen.py'
            self.model = self.home/QWEN_MODEL_DIRECTORY
            self.required_model_files = ('chat_template.json','config.json','generation_config.json',
                                         'model.safetensors','model.safetensors.index.json','tokenizer_config.json',
                                         'vocab.json','merges.txt','preprocessor_config.json','personalos-model-manifest.json')
        self.runner = runner or bounded_transcribe

    def _configuration(self, enabled):
        result = {'enabled':enabled, 'model_path':str(self.model)}
        if self.engine == 'qwen3': result['engine'] = 'qwen3'
        return result

    def _ready(self):
        try:
            if not _regular(self.config_path,True) or self.config_path.stat().st_size > 4096: return False
            config = json.loads(self.config_path.read_text())
            if not isinstance(config,dict) or type(config.get('enabled')) is not bool: return False
            if config != self._configuration(True): return False
            # A venv python is normally a trusted local symlink to the interpreter.
            if not self.python.is_file() or not os.access(self.python,os.X_OK): return False
            if not _regular(self.script) or self.script.stat().st_mode & 0o022: return False
            if self.model.is_symlink() or not self.model.is_dir(): return False
            for name in self.required_model_files:
                if not _regular(self.model/name): return False
            if self.engine == 'qwen3':
                for name in ('transcribe_local.py','transcript_quality.py'):
                    dependency = self.home/'runtime'/name
                    if not _regular(dependency) or dependency.stat().st_mode & 0o022: return False
            return True
        except (OSError,ValueError,TypeError): return False

    def status(self):
        ready = self._ready(); state = 'ready' if ready else 'not_configured'
        return {'voice_configured':ready,'voice_status':state,'voice_source_status':state,
                'voice_source':'local','voice_engine':self.engine,'voice_max_bytes':MAX_AUDIO_BYTES,'voice_max_seconds':MAX_SECONDS}

    def enable(self, enabled=True):
        temporary = self.home/('.voice-'+uuid.uuid4().hex+'.tmp')
        descriptor = os.open(str(temporary),os.O_WRONLY|os.O_CREAT|os.O_EXCL,0o600)
        with os.fdopen(descriptor,'w') as stream:
            json.dump(self._configuration(enabled),stream)
        try:
            if enabled:
                # Validate fixed local assets before publishing the configuration.
                original = self.config_path; self.config_path = temporary
                try:
                    if not self._ready(): raise VoiceError('voice_unavailable',503)
                finally: self.config_path = original
            os.replace(temporary,self.config_path)
        finally: temporary.unlink(missing_ok=True)
        self.store.audit('','voice_enabled' if enabled else 'voice_disabled',{'source':'local','max_bytes':MAX_AUDIO_BYTES,'max_seconds':MAX_SECONDS})

    def _job_directory(self, ident):
        if not isinstance(ident,str) or not re.fullmatch(r'[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}',ident):
            raise VoiceError('voice_failed')
        return self.directory/ident

    def cleanup(self, ident):
        directory = self._job_directory(ident)
        if directory.is_symlink(): directory.unlink()
        elif directory.exists(): shutil.rmtree(directory)

    def submit(self, body, device, calendar_validator):
        if not isinstance(body,dict) or set(body)-{'audio_base64','mime_type','request_id','calendar_context','time_zone','session_id','reply_to','input_refs'} or any(not isinstance(v,str) for v in body.values()):
            raise ValueError('语音字段无效')
        from voice_sessions import validate_fields
        validate_fields(body)
        request_id = body.get('request_id','')
        if not re.fullmatch(r'[A-Za-z0-9_-]{1,128}',request_id): raise ValueError('语音 request_id 格式无效')
        kind = MIMES.get(body.get('mime_type'))
        if not kind: raise VoiceError('invalid_audio')
        raw = body.get('audio_base64','')
        if len(raw) > MAX_BASE64_BYTES: raise VoiceError('audio_too_large',413)
        if 'time_zone' in body: validate_zone(body['time_zone'])
        calendar = None
        if 'calendar_context' in body:
            calendar = json.dumps(calendar_validator(body['calendar_context']),ensure_ascii=False,sort_keys=True,separators=(',',':'))
        try: audio = base64.b64decode(raw,validate=True)
        except (ValueError,binascii.Error): raise VoiceError('invalid_audio') from None
        if not audio: raise VoiceError('invalid_audio')
        if len(audio)>MAX_AUDIO_BYTES: raise VoiceError('audio_too_large',413)
        if kind=='wav' and not (audio[:4]==b'RIFF' and audio[8:12]==b'WAVE'): raise VoiceError('invalid_audio')
        if kind=='m4a' and not (len(audio)>=16 and audio[4:8]==b'ftyp'): raise VoiceError('invalid_audio')
        identity={'source':'voice','audio_hash':hashlib.sha256(audio).hexdigest(),'kind':kind,'calendar':calendar}
        # Preserve the digest for old clients that omit time_zone.
        if 'time_zone' in body: identity['time_zone']=body['time_zone']
        for key in ('session_id','reply_to','input_refs'):
            if key in body:identity[key]=body[key]
        digest = hashlib.sha256(json.dumps(identity,sort_keys=True).encode()).hexdigest()
        ident = str(uuid.uuid4()); job = self._job_directory(ident)
        try:
            with self.store.db() as c:
                c.execute('BEGIN IMMEDIATE')
                previous = c.execute('SELECT task,body_hash FROM task_requests WHERE device=? AND request_id=?',(device,request_id)).fetchone()
                if previous:
                    if previous['body_hash']!=digest:
                        error = VoiceError('voice_failed',409); error.message='这份录音已提交过，但附带的日历内容发生变化。请先在历史中查看原任务。'; raise error
                    return previous['task']
                if not self._ready(): raise VoiceError('voice_unavailable',503)
                count = c.execute("SELECT count(*) FROM voice_jobs WHERE state IN ('queued','transcribing')").fetchone()[0]
                if count >= MAX_PENDING: raise VoiceError('queue_full',429)
                if self.directory.is_symlink(): raise VoiceError('voice_unavailable',503)
                self.directory.mkdir(mode=0o700,exist_ok=True); self.directory.chmod(0o700)
                job.mkdir(mode=0o700)
                descriptor = os.open(str(job/'input.audio'),os.O_WRONLY|os.O_CREAT|os.O_EXCL,0o600)
                with os.fdopen(descriptor,'wb') as stream: stream.write(audio); stream.flush(); os.fsync(stream.fileno())
                task = {'text':'语音任务（等待本地转写）','_voice_id':ident}
                for key in ('session_id','reply_to','input_refs'):
                    if key in body:task[key]=body[key]
                self.store.sessions.attach(c,device,task,ident)
                if 'time_zone' in body: task['time_zone']=body['time_zone']
                if calendar is not None: task['calendar_context']=calendar
                c.execute('INSERT INTO tasks(id,body,state,result,plan,created,due,owner) VALUES(?,?,?,?,?,?,?,?)',(ident,json.dumps(task,ensure_ascii=False),'queued','','{}',time.time(),None,device))
                c.execute('INSERT INTO voice_jobs(task,state,mime,bytes,created) VALUES(?,?,?,?,?)',(ident,'queued',kind,len(audio),time.time()))
                c.execute('INSERT INTO task_requests VALUES(?,?,?,?)',(device,request_id,ident,digest))
                self.store._audit(c,ident,'voice_queued',{'source':'local','format':kind,'bytes':len(audio)})
            return ident
        except Exception:
            self.cleanup(ident); raise

    def transcribe(self, row):
        ident = row['id']; job = self._job_directory(ident)
        try:
            with self.store.db() as c:
                c.execute('BEGIN IMMEDIATE')
                changed = c.execute("UPDATE voice_jobs SET state='transcribing' WHERE task=? AND state='queued'",(ident,)).rowcount
                if not changed: raise VoiceError('voice_interrupted')
                self.store._audit(c,ident,'voice_transcribing',{'source':'local'})
            if not self._ready(): raise VoiceError('voice_unavailable',503)
            audio = job/'input.audio'
            if job.is_symlink() or not _regular(audio,True) or not 0<audio.stat().st_size<=MAX_AUDIO_BYTES: raise VoiceError('invalid_audio')
            result = self.runner([str(self.python),str(self.script),'--input',str(audio),'--model',str(self.model)],job,timeout=150)
            text, language = result['text'],result.get('language','')
            if not isinstance(text,str) or not 0<len(text.strip())<=4000 or not isinstance(language,str) or not re.fullmatch(r'[A-Za-z-]{0,20}',language): raise VoiceError('voice_failed')
            # Re-check even if the installed runner is older or a transport is substituted.
            # A rejected transcript must never enter the policy/tool queue or task body.
            if transcript_problem(text): raise VoiceError('unreliable_transcript')
            body = json.loads(row['body']); body.pop('_voice_id',None); body['text']=text.strip(); body['source']='voice'
            with self.store.db() as c:
                c.execute('BEGIN IMMEDIATE')
                c.execute("UPDATE tasks SET body=?,state='queued',result='录音已在 Mac 本地转写，等待执行。' WHERE id=? AND state='running'",(json.dumps(body,ensure_ascii=False),ident))
                c.execute("UPDATE voice_jobs SET state='transcribed' WHERE task=?",(ident,))
                self.store._audit(c,ident,'voice_transcribed',{'source':'local','language':language,'characters':len(text.strip())})
        except Exception as error:
            failure = error if isinstance(error,VoiceError) else VoiceError('voice_failed')
            with self.store.db() as c:
                c.execute("UPDATE voice_jobs SET state='failed' WHERE task=?",(ident,))
                c.execute("UPDATE tasks SET state='failed',result=? WHERE id=?",(failure.message,ident))
                self.store._audit(c,ident,'voice_failed',{'code':failure.code}); self.store._notify(c,ident,'failed')
        finally:
            try: self.cleanup(ident)
            except OSError:
                # Never overwrite an already persisted transcript/task transition.
                # Startup recovery retries deletion of every terminal job directory.
                self.store.audit(ident,'voice_cleanup_failed',{'code':'cleanup_retry_on_restart'})

    def recover(self):
        """Called only after binding the service port; interrupted ASR is never replayed."""
        keep = set()
        with self.store.db() as c:
            c.execute('BEGIN IMMEDIATE')
            for row in c.execute('SELECT v.task,v.state,t.state AS task_state FROM voice_jobs v JOIN tasks t ON t.id=v.task').fetchall():
                if row['state']=='queued' and row['task_state']=='queued':
                    path = self._job_directory(row['task'])/'input.audio'
                    if not path.parent.is_symlink() and path.is_file() and not path.is_symlink(): keep.add(row['task']); continue
                if row['state'] in ('queued','transcribing') and row['task_state'] not in ('cancelled','completed'):
                    c.execute("UPDATE tasks SET state='failed',result=? WHERE id=?",(ERRORS['voice_interrupted'],row['task']))
                    c.execute("UPDATE voice_jobs SET state='failed' WHERE task=?",(row['task'],))
                    self.store._audit(c,row['task'],'voice_failed',{'code':'voice_interrupted'}); self.store._notify(c,row['task'],'failed')
        if self.directory.is_symlink(): return
        if self.directory.exists():
            for path in self.directory.iterdir():
                if path.name in keep: continue
                if path.is_dir() and not path.is_symlink(): shutil.rmtree(path)
                else: path.unlink()
