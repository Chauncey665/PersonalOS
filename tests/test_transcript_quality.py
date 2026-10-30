import base64
import datetime as dt
import json
import pathlib
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0,str(pathlib.Path(__file__).resolve().parents[1]/'server'))
from transcript_quality import transcript_problem
from transcribe_local import SpeechError, validate_result
from personal_os import Store, Worker, validate_calendar
from calendar_journal import validate_zone


class TranscriptQualityTests(unittest.TestCase):
    def test_worker_rejects_punctuation_only_noise_transcripts(self):
        for text in ('.','。。。','… !?','♪♫','   '):
            result={'text':text,'language':'zh','segments':[{'text':text,'no_speech_prob':0.01,'avg_logprob':-0.1}]}
            with self.subTest(text=text), self.assertRaises(SpeechError) as error:
                validate_result(result)
            self.assertEqual(error.exception.code,'no_speech')

    def test_extreme_character_fragment_and_compression_repetitions(self):
        examples=[('明天下午三点到四点安排'+'热'*100,'repeated_character'),
                  ('请显示设备状态'+'测试'*15,'repeated_fragment'),
                  ('Schedule review tomorrow from 10 to 11. '+'please '*10,'repeated_fragment'),
                  (('请在明天上午十点到十一点检查PROJECT界面并准备一次详细的项目讨论资料。')*12,'excessive_compression')]
        for text,reason in examples:
            with self.subTest(reason=reason): self.assertEqual(transcript_problem(text),reason)
        self.assertEqual(transcript_problem('安排明天会议：'+'热，'*10),'repeated_character')

    def test_normal_bilingual_calendar_status_and_numeric_repetition_preserved(self):
        samples=['请显示设备状态。','Please show the current device status.',
                 '昨天上午十点到十一点我完成了日历验收','明天下午三点到四点安排PROJECT界面设计',
                 'I finished the calendar test yesterday from 10 to 11.',
                 'Schedule PROJECT review tomorrow from 10 to 11.',
                 '电话号码是11111111111，备用号码是88888888。',
                 '电话号码是一一一一一一一一一一一，备用号码是零零零零零零零零。',
                 'The phone number is 11111111111 and the code is 00000000.',
                 '今天到11点我完成了日记验生']
        for text in samples:
            with self.subTest(text=text): self.assertIsNone(transcript_problem(text))
        self.assertIsNone(transcript_problem('号码是'+('1234567890'*200)))

    def test_runner_rejects_repetition_despite_high_model_confidence(self):
        text='明天下午三点到四点安排'+'热'*100
        result={'text':text,'language':'zh','segments':[{'text':text,'no_speech_prob':0.001,'avg_logprob':-.01}]}
        with self.assertRaises(SpeechError) as error: validate_result(result)
        self.assertEqual(error.exception.code,'no_speech'); self.assertIn('重新说',error.exception.message)
        result.update(text='请显示设备状态。'); result['segments'][0]['text']=result['text']
        self.assertEqual(validate_result(result),{'text':result['text'],'language':'zh'})

    def test_runner_rejects_whole_transcript_with_unreliable_spoken_segment(self):
        good={'text':'请记录我的安排。','no_speech_prob':0.01,'avg_logprob':-0.1}
        for metadata in ({'no_speech_prob':0.99,'avg_logprob':-5.0},
                         {'no_speech_prob':0.6,'avg_logprob':-0.1},
                         {'no_speech_prob':0.01,'avg_logprob':-1.2001}):
            bad={'text':'明天下午三点到四点开会。',**metadata}
            for segments in ([good,bad],[bad,good]):
                with self.subTest(metadata=metadata,bad_first=segments[0] is bad):
                    result={'text':''.join(item['text'] for item in segments),'language':'zh','segments':segments}
                    with self.assertRaises(SpeechError) as error: validate_result(result)
                    self.assertEqual(error.exception.code,'no_speech')
                    self.assertNotIn(bad['text'],error.exception.message)

    def test_runner_requires_typed_finite_segment_confidence(self):
        good={'text':'请记录我的安排。','no_speech_prob':0.01,'avg_logprob':-0.1}
        invalid_values=[None,True,False,'0.1',[],{},float('nan'),float('inf'),float('-inf'),10**1000]
        for key in ('no_speech_prob','avg_logprob'):
            for value in invalid_values:
                with self.subTest(key=key,value=repr(value)):
                    bad={**good,key:value}
                    with self.assertRaises(SpeechError) as error:
                        validate_result({'text':good['text']*2,'language':'zh','segments':[good,bad]})
                    self.assertEqual(error.exception.code,'no_speech')
            bad=dict(good); del bad[key]
            with self.subTest(missing=key),self.assertRaises(SpeechError):
                validate_result({'text':good['text']*2,'language':'zh','segments':[good,bad]})
        for key,value in (('no_speech_prob',-0.1),('no_speech_prob',1.1),('avg_logprob',0.1)):
            with self.subTest(key=key,value=value),self.assertRaises(SpeechError):
                validate_result({'text':good['text'],'language':'zh','segments':[{**good,key:value}]})

    def test_runner_rejects_malformed_result_and_segment_shapes(self):
        good={'text':'显示状态','no_speech_prob':0.01,'avg_logprob':-0.1}
        base={'text':'显示状态','language':'zh','segments':[good]}
        cases=[None,[],{}, {**base,'text':None},{**base,'text':123},{**base,'language':True},
               {**base,'segments':None},{**base,'segments':{}},{**base,'segments':'text'},
               {**base,'segments':[]},{**base,'segments':[{'text':'  '}]},
               {**base,'segments':[good,None]},{**base,'segments':[good,'text']},
               {**base,'segments':[good,{}]},{**base,'segments':[good,{'text':123}]}]
        for index,result in enumerate(cases):
            with self.subTest(case=index),self.assertRaises(SpeechError) as error:
                validate_result(result)
            self.assertEqual(error.exception.code,'no_speech')

    def test_runner_preserves_all_reliable_segments_and_ignores_empty_ones(self):
        text='昨天上午十点到十一点我完成了整理。 Tomorrow from 3 to 4 PM, schedule review.'
        segments=[{'text':'  '},
                  {'text':'昨天上午十点到十一点我完成了整理。','no_speech_prob':0,'avg_logprob':0},
                  {'text':'Tomorrow from 3 to 4 PM, schedule review.','no_speech_prob':0.599,'avg_logprob':-1.2}]
        self.assertEqual(validate_result({'text':text,'language':'zh','segments':segments}),
                         {'text':text,'language':'zh'})

    def test_service_guard_prevents_calendar_or_tools_and_removes_raw_audio(self):
        with tempfile.TemporaryDirectory() as directory:
            store=Store(pathlib.Path(directory)); owner='isolated-quality-fixture'
            audio=base64.b64encode(b'RIFF\x00\x00\x00\x00WAVEisolated').decode()
            with mock.patch.object(store.voice,'_ready',return_value=True), \
                 mock.patch.object(store.calendar,'create',side_effect=AssertionError('must not reach calendar')) as calendar, \
                 mock.patch.object(store,'plan_files',side_effect=AssertionError('must not reach files')) as files, \
                 mock.patch('personal_os.urllib.request.urlopen',side_effect=AssertionError('must not plan')) as planner:
                for index,text in enumerate(('明天下午三点到四点安排'+'热'*100,'整理文件 '+('delete '*10),'请显示设备状态 '+('repeat '*10))):
                    ident=store.voice.submit({'audio_base64':audio,'mime_type':'audio/wav','request_id':'repeat-'+str(index)},owner,validate_calendar)
                    store.update(ident,state='running'); store.voice.runner=mock.Mock(return_value={'text':text,'language':'zh'})
                    Worker(store).execute(store.task(ident)); task=store.task(ident)
                    self.assertEqual(task['state'],'failed'); self.assertIn('重新录音',task['result'])
                    self.assertNotIn(text,task['body']); self.assertFalse(store.voice._job_directory(ident).exists())
                    events=[row['event'] for row in store.audits(ident)]
                    self.assertIn('voice_failed',events); self.assertNotIn('voice_transcribed',events); self.assertNotIn('planning',events)
                    self.assertNotIn(text,json.dumps(store.audits(ident)))
                self.assertEqual(store.calendar.entries(owner)['entries'],[])
                calendar.assert_not_called(); files.assert_not_called(); planner.assert_not_called()

    def test_normal_voice_reaches_existing_status_and_calendar_missing_time_stops(self):
        with tempfile.TemporaryDirectory() as directory:
            store=Store(pathlib.Path(directory)); owner='isolated-quality-fixture'
            audio=base64.b64encode(b'RIFF\x00\x00\x00\x00WAVEisolated').decode()
            created=dt.datetime(2026,10,25,17,tzinfo=validate_zone('Asia/Hong_Kong')).timestamp()
            cases=[('请显示设备状态','completed',False),('Please show the current device status.','completed',False),
                   ('明天下午三点到四点安排PROJECT界面设计','completed',True),('今天到11点我完成了日记验生','needs_details',True)]
            with mock.patch.object(store.voice,'_ready',return_value=True):
                for index,(text,state,is_calendar) in enumerate(cases):
                    ident=store.voice.submit({'audio_base64':audio,'mime_type':'audio/wav','request_id':'normal-'+str(index)},owner,validate_calendar)
                    with store.db() as c: c.execute("UPDATE tasks SET state='running',created=? WHERE id=?",(created,ident))
                    store.voice.runner=mock.Mock(return_value={'text':text,'language':'en' if index==1 else 'zh'})
                    worker=Worker(store); worker.execute(store.task(ident))
                    self.assertEqual(store.task(ident)['state'],'queued')
                    worker.execute(store.task(ident)); task=store.task(ident); self.assertEqual(task['state'],state)
                    if is_calendar:
                        entry=json.loads(task['result'])['calendar_entry']; self.assertEqual(entry['device_sync'],'pending')
                        self.assertEqual(entry['status'],'needs_details' if index==3 else 'ready')
                    else: self.assertIn('platform',json.loads(task['result']))


if __name__=='__main__': unittest.main(verbosity=2)
