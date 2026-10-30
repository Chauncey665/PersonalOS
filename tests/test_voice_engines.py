import json, pathlib, sys, tempfile, unittest
from types import SimpleNamespace
sys.path.insert(0,str(pathlib.Path(__file__).resolve().parents[1]/"server"))
from voice import VoiceService,QWEN_MODEL_DIRECTORY

class VoiceEngineTests(unittest.TestCase):
 def setUp(self):
  self.temp=tempfile.TemporaryDirectory();self.home=pathlib.Path(self.temp.name)
  self.store=SimpleNamespace(home=self.home,audit=lambda *args:None)
 def tearDown(self):self.temp.cleanup()
 def config(self,**extra):
  value={"enabled":True,"engine":"qwen3","model_path":str(self.home/QWEN_MODEL_DIRECTORY)};value.update(extra)
  p=self.home/"voice.json";p.write_text(json.dumps(value));p.chmod(0o600)
 def assets(self,service):
  service.python.parent.mkdir(parents=True);service.python.symlink_to(sys.executable)
  service.script.parent.mkdir(parents=True,exist_ok=True)
  for name in ["transcribe_qwen.py","transcribe_local.py","transcript_quality.py"]:
   p=service.script.parent/name;p.write_text("# test-only");p.chmod(0o600)
  service.model.mkdir(parents=True)
  for name in service.required_model_files:(service.model/name).write_text("{}")
 def test_old_configuration_keeps_whisper(self):
  service=VoiceService(self.store);self.assertEqual(service.engine,"whisper")
  self.assertEqual(service._configuration(True),{"enabled":True,"model_path":str(service.model)})
 def test_fixed_qwen_paths_and_disable_enable_keep_engine(self):
  self.config();service=VoiceService(self.store);self.assets(service)
  self.assertEqual(service.engine,"qwen3");self.assertTrue(service._ready())
  self.assertEqual(service.python,self.home/"speech-qwen-venv/bin/python")
  service.enable(False);self.assertFalse(service._ready())
  service.enable();self.assertTrue(service._ready())
  self.assertEqual(json.loads(service.config_path.read_text())["engine"],"qwen3")
 def test_paths_or_fields_cannot_select_arbitrary_programs(self):
  for field,value in [("model_path","/tmp/not-authorized"),("python_path","/bin/sh"),("engine","remote")]:
   with self.subTest(field=field):
    self.config(**{field:value});service=VoiceService(self.store);self.assertFalse(service._ready())
    self.assertNotEqual(str(service.python),"/bin/sh")
 def test_missing_dependency_or_public_config_fails_closed(self):
  self.config();service=VoiceService(self.store);self.assets(service)
  dependency=service.script.parent/"transcribe_local.py";dependency.unlink()
  self.assertFalse(service._ready())
  dependency.write_text("# test-only");dependency.chmod(0o600)
  self.assertTrue(service._ready())
  service.config_path.chmod(0o644);self.assertFalse(service._ready())
 def test_symlink_model_file_is_not_accepted(self):
  self.config();service=VoiceService(self.store);self.assets(service)
  p=service.model/"model.safetensors";p.unlink();p.symlink_to(service.model/"config.json")
  self.assertFalse(service._ready())
 def test_every_required_qwen_file_is_checked_before_accepting_audio(self):
  self.config();service=VoiceService(self.store);self.assets(service)
  for name in ('chat_template.json','config.json','generation_config.json','merges.txt',
               'model.safetensors','model.safetensors.index.json','preprocessor_config.json',
               'tokenizer_config.json','vocab.json','personalos-model-manifest.json'):
   with self.subTest(name=name):
    file=service.model/name;file.unlink()
    self.assertFalse(service._ready())
    file.write_text('{}')
    self.assertTrue(service._ready())
 def test_enable_requires_boolean_not_integer(self):
  self.config();service=VoiceService(self.store);self.assets(service)
  for invalid in (1,0,'true',None):
   with self.subTest(enabled=invalid):
    self.config(enabled=invalid);self.assertFalse(service._ready())

if __name__=="__main__":unittest.main()
