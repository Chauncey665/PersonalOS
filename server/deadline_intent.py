"""Deadline facts and unscheduled reminders, without inventing a work interval."""
import datetime as dt
import re
from calendar_journal import DATE_RE, _date, validate_zone

def normalized_time(text):
    text = re.sub(r'(?<![\d.])(\d{1,2})[.．](\d{2})(?![\d.])', r'\1:\2', text)
    for a,b in [('今晚','今天晚上'),('今早','今天早上'),('明晚','明天晚上'),('明早','明天早上'),('昨晚','昨天晚上')]:
        text=text.replace(a,b)
    return re.sub(r'\btonight\b','today evening',text,flags=re.I)

def deadline_request(text):
    return bool(re.search(r'截止|\bddl\b|\bdeadline\b|(?:之前|以前|前).{0,30}(?:完成|做完|交|提交|复习完)|(?:完成|做完|提交).{0,30}(?:之前|以前)|\b(?:due|by|before)\s+(?:today|tomorrow|tonight|\d)',text,re.I))

def deadline_capture_requested(text):
    """A deadline plus a direct task instruction authorizes a todo, not a slot.

    This is only an intent gate. Dates and times still require exact quotation
    and the existing deterministic checks in TodoStore.model_items.
    """
    if not isinstance(text,str) or not 1<=len(text)<=4000 or not deadline_request(text):
        return False
    # A question, hypothetical, explanation or negated instruction does not
    # become a write merely because its quoted example contains a deadline.
    if re.search(r'[？?]|是否|能否|可否|能不能|要不要|怎么|如何|为什么|什么意思|什么时候|'
                 r'解释|翻译|改写|润色|总结|分析|举例|比如|例如|假设|如果|假如|可能|也许|建议|请问|记住|记着|'
                 r'(?:吗|么|呢)[。！!\s]*$|'
                 r'不要|不用|不需要|不想|不打算|先别|别(?:记录|记下|安排|提交|完成|做完)|取消|'
                 r'已(?:经)?(?:完成|做完|提交|交付)|(?:完成|做完|提交|交付)了|'
                 r'\b(?:why|what|how|when|whether|explain|translate|rewrite|summari[sz]e|example|'
                 r'if|maybe|perhaps|could|should|would|not|never|don.t|cancel|already|remember)\b',text,re.I):
        return False
    explicit_record = bool(re.match(r'^\s*(?:(?:请|帮我|麻烦|麻烦你)\s*)*(?:新增|添加|记录|记下|记个|加个待办|'
                                    r'(?:please\s+)?(?:add|record)\b)',text,re.I))
    quoted_data = re.search(r'回复|回信|原文|引用|[“”「」"`]|'
                            r'(?:消息|短信|邮件)(?:的)?(?:内容|原文|里|中|是|写|说)|'
                            r'(?:老师|他|她|对方|同学|朋友|老板|网页|文档)(?:说|写|要求|通知|显示|里|中)|'
                            r'\b(?:reply|message says|email says|he said|she said|quote)\b',text,re.I)
    if quoted_data and not explicit_record:
        return False
    dated = DATE_RE.search(normalized_time(text)) or re.search(r'(?:周|星期)[一二三四五六日天1-7]|'
        r'\d{1,2}[:：.]\d{2}|[\d零〇一二两三四五六七八九十]+[点时]|\b\d+\s*(?:am|pm)\b',text,re.I)
    return explicit_record or bool(dated and re.search(r'完成|做完|提交|交付|交(?:作业|报告|论文|材料|文件)|复习完|写完|读完|看完|准备好|'
                                                       r'\b(?:finish|complete|submit|deliver|hand in)\b',text,re.I))

def unscheduled_reminder(text):
    if not re.search(r'提醒|\bremind\b',text,re.I):return False
    if re.search(r'不要|不用|取消|别|\b(?:not|never|cancel)\b',text,re.I):return False
    return not re.search(r'\d[:：.]\d|[\d零〇一二两三四五六七八九十]+[点时]|[\d一二两三四五六七八九十]+(?:个)?(?:分钟|小时)(?:后|以后)|\b\d+\s*(?:am|pm|minutes?|hours?)\b',text,re.I)

def deadline_fact(text,created,zone_name):
    """Returns a single explicit deadline (date or exact time), never a default hour."""
    value=normalized_time(text);zone=validate_zone(zone_name)
    dates={_date(m.group(),dt.datetime.fromtimestamp(created,zone).date()) for m in DATE_RE.finditer(value)}
    if len(dates)>1:raise ValueError('出现多个截止日期，请分别说明每项待办的截止日期')
    if re.search(r'\d[:：]\d|[\d零〇一二两三四五六七八九十]+[点时]|\b\d+\s*(?:am|pm)\b',value,re.I):
        from reminder_time import parse_reminder_time
        stamp=parse_reminder_time(value.replace('之前','，').replace('以前','，'),created,zone_name)
        return dt.datetime.fromtimestamp(stamp,zone).isoformat()
    return next(iter(dates)).isoformat() if dates else None

def simple_deadline_item(text,created,zone_name):
    """Exact common form avoids asking a small model to copy a date quotation."""
    if not deadline_request(text) or re.search(r'然后|另外|分别|以及|并且|拆分|拆成|子任务',text):return None
    value=deadline_fact(text,created,zone_name)
    if not value:return None
    match=re.search(r'(?:要完成的|完成的|待办|任务)[：:]\s*(.+?)(?:\s*的?\s*(?:ddl|截止|deadline)(?:提醒)?)?[。！!]*$',text,re.I)
    if not match and deadline_capture_requested(text):
        # One deadline followed by one explicit task: the validated date comes
        # from the parser, never from the model's weekday arithmetic.
        match=re.search(r'(?:之前|以前|前)[，,\s]*(?:(?:我)?(?:要|需要|必须|得)\s*)?((?:提交|交付|完成|做完|复习完|写完|读完|准备好|交).+?)[。！!]*$',text)
        if match and re.search(r'和|以及|、|[；;]',match[1]):return None
    if not match:return None
    title=match[1].strip(' ：:，,。')
    title=re.sub(r'\s*的?\s*(?:ddl|截止|deadline)(?:提醒)?$','',title,flags=re.I).strip()
    if not 1<=len(title)<=200:return None
    return {'title':title,'deadline':value,'notes':'原始要求：'+text,'kind':'task'}


def simple_unscheduled_item(text):
    if not unscheduled_reminder(text) or deadline_request(text) or re.search(r'然后|另外|分别|以及|并且|[？?]',text):return None
    title=re.split(r'(?:提醒(?:一下)?(?:我)?(?:要)?|\bremind(?: me)?(?: to)?)\s*',text,maxsplit=1,flags=re.I)[-1].strip(' ，,。！!')
    if title==text.strip(' ，,。！!') or not title:return None
    return {'title':title[:200],'deadline':None,'notes':'原始要求：'+text,'kind':'task'}


def requested_window(text,created,zone_name):
    """Vague execution-day hints constrain scheduling, never invent a deadline."""
    if not unscheduled_reminder(text) or deadline_request(text):return None
    zone=validate_zone(zone_name);value=normalized_time(text)
    matches=list(DATE_RE.finditer(value))
    if len(matches)>1:return None
    day=_date(matches[0].group(),dt.datetime.fromtimestamp(created,zone).date()) if matches else dt.datetime.fromtimestamp(created,zone).date()
    parts=[(r'上午|早上|morning',6,12),(r'下午|afternoon',12,18),(r'晚上|夜里|evening|night',18,24)]
    low,high=0,24
    for pattern,a,b in parts:
        if re.search(pattern,value,re.I):low,high=a,b;break
    if not matches and low==0:return None
    from calendar_journal import _localize
    origin=dt.datetime.combine(day,dt.time())
    return (_localize(origin+dt.timedelta(hours=low),zone).timestamp(),_localize(origin+dt.timedelta(hours=high),zone).timestamp())
