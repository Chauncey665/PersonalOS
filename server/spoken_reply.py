"""Short, factual speech; full task results remain unchanged."""
import json,re

def brief(text,plan=None,question=''):
 if isinstance(plan,str):
  try:plan=json.loads(plan)
  except (ValueError,TypeError):plan={}
 if isinstance(plan,dict) and isinstance(plan.get('spoken_reply'),str):return plan['spoken_reply'][:240]
 try:
  v=json.loads(text)
  if isinstance(v,dict):
   if 'free_disk_gb' in v:return 'Mac 在线，可用空间 '+str(v['free_disk_gb'])+' GB。'
   if isinstance(v.get('message'),str):return brief(v['message'])
   if isinstance(v.get('hits'),list):return '找到 '+str(len(v['hits']))+' 份资料，详细结果已保留。'
   return '结果已整理，请在任务详情查看。'
 except (ValueError,TypeError):pass
 # Upgrade already-saved weather replies without running the weather tool again.
 rain=re.search(r'最高降雨概率\s*([\d.]+)%',text)
 if rain and '天气' in text:
  chance=float(rain[1]);level='下雨机会较低' if chance<20 else '可能下雨，出门建议带伞' if chance<60 else '下雨机会较高，出门建议带伞'
  return '查询时段'+level+'，最高降雨概率'+rain[1]+'%。'
 text=re.sub(r'\[([^\]]+)\]\([^)]*\)',r'\1',str(text))
 text=re.sub(r'https?://\S+','',text)
 lines=[re.sub(r'^\s*(?:[#*\-]\s+|\d+[.)、]\s+)?','',x).strip() for x in text.splitlines() if x.strip() and '|' not in x and not re.search(r'^(?:来源|查询时间|位置更新|预报更新)[：:]',x.strip())]
 clean='。'.join(x.rstrip('。') for x in lines)
 clean=re.sub(r'[#*`]+','',clean).strip()
 if not clean:return '这次没有可用结果，请再说一遍。'
 sentences=re.findall(r'.+?(?:[。！？!?](?:\s|$)?|(?<=\D)\.(?:\s|$)|$)',clean)
 answer=''
 for sentence in sentences[:2]:
  if len(answer+sentence)>180:break
  answer+=sentence
 if answer:return answer
 # Do not speak fragments of a long unpunctuated answer or an entire table.
 return '详细结果已整理，请在任务详情查看。'

def task_speech(row):
 if row.get('state')=='awaiting_confirmation':return row.get('result','')[:600]
 return brief(row.get('result',''),row.get('plan'))
