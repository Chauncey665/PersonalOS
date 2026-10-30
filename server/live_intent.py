"""Read-only factual intent recognition; history supplies topic, never live facts."""
import re

WEATHER = r'天气|气温|温度|多少度|几度|热不热|冷不冷|会冷吗|会热吗|下雨|降雨|带伞|要伞|晴天|阴天|weather|temperature|\brain\b|\braining\b|umbrella'
HOLIDAY = r'过节|放假|公众假期|节假日|春节|中秋|端午|国庆|圣诞|元旦|清明|重阳|\bholidays?\b'
PUBLIC = r'新闻|股价|汇率|航班状态|最新比分|实时路况'

def kind(text):
    if re.search(r'体温|烤箱|空调设|CPU|显卡|摄氏度转|华氏|API|模型|代码|文档|解释|什么意思|翻译|research',text,re.I):return None
    if re.search(WEATHER,text,re.I):return 'weather'
    if re.search(HOLIDAY,text,re.I):return 'public_holidays'
    if re.search(PUBLIC,text,re.I):return 'web_search'
    return None

def readonly(text):
    return bool(kind(text)) and not re.search(r'安排|新增|添加|记录|改写|修改|删除|整理|提醒|记住|忘记|schedule|remember|remind|edit|delete',text,re.I)

def followup(text):
    return bool(re.fullmatch(r'(?:那|那么|然后|现在|那现在|那就)?(?:明天|后天|今天|今晚|明晚|下午|晚上|还会吗|还下吗|还热吗|还冷吗|现在呢)(?:呢|怎么样|会怎样|还会下吗)?[？?。!！ ]*',text))
