import json,pathlib,sys,unittest
sys.path.insert(0,str(pathlib.Path(__file__).resolve().parents[1]/'server'))
from spoken_reply import brief,task_speech
from live_info import LiveInfo
from speech_text import natural_text

class SpokenReplyTests(unittest.TestCase):
 def test_pronunciation_numbers_and_language_are_preserved(self):
  self.assertEqual(natural_text('最高降雨概率7%。明天15:00，步行350米。'),'最高降雨概率百分之七。明天十五点整，步行三百五十米。')
  self.assertEqual(natural_text('Rain chance 7% at 15:00.'),'Rain chance 7% at 15:00.')
  self.assertEqual(natural_text('概率27.5%，23:59提醒。'),'概率百分之二十七点五，二十三点五十九分提醒。')
 def test_weather_summary_never_reads_table(self):
  full='2026-10-28 · 手机地区天气\n最高降雨概率 7%；预报存在不确定性。\n时间 | 温度 | 降雨概率\n'+'04:00 | 23.8°C | 7%\n'*24
  answer=brief(full);self.assertIn('较低',answer);self.assertIn('7%',answer);self.assertNotIn('|',answer);self.assertLess(len(answer),70)
 def test_verified_forecast_is_conditional_not_guaranteed(self):
  data={'date':'2026-10-28','fetched_at':'2026-10-28T04:00:00+08:00','hourly':[{'rain_probability_percent':7},{'rain_probability_percent':0}]}
  answer=LiveInfo.speak_weather(data);self.assertIn('今天接下来',answer);self.assertIn('7%',answer);self.assertNotIn('不会下雨',answer)
  data['hourly']=[{'rain_probability_percent':80}];self.assertIn('带伞',LiveInfo.speak_weather(data))
  data['hourly']=[];self.assertIn('不能判断',LiveInfo.speak_weather(data))
 def test_tomorrow_not_called_today(self):
  data={'date':'2026-10-29','fetched_at':'2026-10-28T04:00:00+08:00','hourly':[{'rain_probability_percent':30}]}
  self.assertTrue(LiveInfo.speak_weather(data).startswith('2026-10-29'))
 def test_complete_sentences_no_sources_table_or_new_claims(self):
  answer=brief('第一步先完成复习。然后交作业。第三步跑步。\n[来源](https://example.com)')
  self.assertEqual(answer,'第一步先完成复习。然后交作业。')
  self.assertEqual(brief('x'*500),'详细结果已整理，请在任务详情查看。')
 def test_device_numbers_and_confirmation_preserved(self):
  self.assertIn('278.7',brief('{"free_disk_gb":278.7}'))
  text='将写回报告；原版保留。请说确认写回或取消。'
  self.assertEqual(task_speech({'state':'awaiting_confirmation','result':text}),text)
 def test_task_result_untouched_and_plan_summary_used(self):
  row={'state':'completed','result':'完整结果'*100,'plan':json.dumps({'spoken_reply':'已安排好。'})}
  self.assertEqual(task_speech(row),'已安排好。');self.assertEqual(len(row['result']),400)

if __name__=='__main__':unittest.main()
