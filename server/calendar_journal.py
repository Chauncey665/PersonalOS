"""Device-owned calendar proposals. Rules extract facts; only phone acknowledgments claim writes."""
import base64
import binascii
import datetime as dt
import hashlib
import json
import math
import re
import time
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

DEFAULT_ZONE = 'Asia/Hong_Kong'
# Consume the whole numeric token before validating it, so an invalid phrase
# such as 一百三点 cannot be silently shortened to 三点.
SPOKEN_NUMBER = r'[\d零〇一二两三四五六七八九十百千万]+'
CHINESE_DATE = r'(?:(?:'+SPOKEN_NUMBER+r')年)?(?:'+SPOKEN_NUMBER+r')月(?:'+SPOKEN_NUMBER+r')[日号]?'
BARE_WEEK_DATE = r'(?:周|星期|礼拜)[一二三四五六日天1-7]'
WEEK_DATE = r'(?:(?:本|这(?:个)?|下(?:个)?)(?:周|星期|礼拜)|(?:周|星期|礼拜))[一二三四五六日天1-7]'
# Capture unsupported repeated prefixes as one token, so 下下周五 cannot
# degrade into 下周五 or 周五 in any consumer of DATE_RE/_date. 记下 is a
# command: in 记下下周五 the second 下 begins the actual date phrase.
WEEK_TOKEN = (r'(?!(?<=记)下下)(?:(?<=记下)|(?=[上下本这每隔大])|(?<=周[一二三四五六日天1-7])|(?<=星期[一二三四五六日天1-7])|(?<=礼拜[一二三四五六日天1-7])|(?<![A-Za-z0-9_上下本这每隔大个一二两三四五六七八九十百千万零〇]))'
              r'(?:[上下本这每隔大一二两三四五六七八九十\d]+(?:个)?\s*)?(?:周|星期|礼拜)'
              r'[一二三四五六七八九十零〇\d日天]'
              r'(?:'+SPOKEN_NUMBER+r'(?![\d零〇一二两三四五六七八九十百千万点时:：]))?')
# Consume the whole relative-day phrase, including malformed/repeated prefixes.
# Only the exact spellings in _date are supported; 大大后天 must not become 后天.
RELATIVE_DAY = r'(?:[大今明昨前后]\s*)+天'
AMPM_TOKEN = r'(?:[ap]m|[ap]\.m\.?)(?![A-Za-z])'
ENGLISH_ONES = r'(?:one|two|three|four|five|six|seven|eight|nine)'
ENGLISH_HOUR = r'(?:'+ENGLISH_ONES+r'|ten|eleven|twelve)'
ENGLISH_MINUTE = r'(?:(?:oh|zero)\s+'+ENGLISH_ONES+r'|ten|eleven|twelve|thirteen|fourteen|fifteen|sixteen|seventeen|eighteen|nineteen|(?:twenty|thirty|forty|fifty)(?:[ -]'+ENGLISH_ONES+r')?)'
ENGLISH_TIME_CONTINUATION = r'(?:(?:zero|oh|'+ENGLISH_HOUR+r'|thirteen|fourteen|fifteen|sixteen|seventeen|eighteen|nineteen|twenty|thirty|forty|fifty|sixty|seventy|eighty|ninety|hundred|thousand)|(?:half|quarter|point|o.clock|[ap])\b)'
DATE_TOKEN = r'(?:\d{4}-\d{2}-\d{2}|'+CHINESE_DATE+r'|'+RELATIVE_DAY+r'|刚刚|刚才|'+WEEK_TOKEN+r'|\b(?:(?:the )?day after tomorrow|tomorrow|yesterday|today|just now|next\s+(?:monday|tuesday|wednesday|thursday|friday|saturday|sunday))\b)'
DATE_RE = re.compile(DATE_TOKEN,re.I)
BLOCKED = re.compile(r'不要|别(?:安排|记录|记下)|取消|没有|没(?:做|写|完成|去|参加)|不(?:会|想|打算|安排|做|写|完成|去|参加|记录|用|需要)|未(?:完成|做|参加)|如果|假如|假设|(?:什么|是否|能否|吗|？|\?)|\b(?:if|maybe|might|perhaps|would|not|never|don.t|didn.t|won.t|can.t|cannot|haven.t|hasn.t|wasn.t|isn.t|wouldn.t|shouldn.t|couldn.t|cancel|what|when|whether|could\s+i|should\s+i)\b',re.I)
DONE = re.compile(r'(?:我|今天|昨天|大?前天|刚刚|刚才).{0,100}?(?:做了|写了|完成了?|结束了|去了|参加了|看了|跑了|练了|读了|修复了|处理了|提交了|部署了)|(?:记录|记下|记入日历).{0,80}(?:已完成|已经做|做过)|\bI\s+(?:(?:have|just|already)\s+)*(?:finished|completed|did|wrote|worked|went|attended|read|fixed|deployed)\b',re.I)
PLAN = re.compile(r'(?:安排|记入日历|记到日历|日历记录|打算|我要|我会|我计划|我准备)|\b(?:schedule|add\s+.+?\s+to\s+(?:my\s+)?calendar|I\s+(?:will|plan\s+to|am\s+going\s+to))\b',re.I)
FUTURE = re.compile(r'明天|后天|下(?:个)?(?:周|星期)|\btomorrow\b|\bnext\s+(?:monday|tuesday|wednesday|thursday|friday|saturday|sunday)\b',re.I)
VAGUE = re.compile(r'大概|左右|可能|也许|有空|改天|找时间|稍后|一会儿|下周末|最近|\b(?:around|about|sometime|later|roughly|approximately|soon)\b',re.I)
MULTIPLE = re.compile(r'[；;、\n]|(?:然后|另外|还要|再去|接着|分别|以及|并且|同时|和)|\b(?:and|also|after\s+that)\b',re.I)


class CalendarError(ValueError):
    def __init__(self,message,status=400): self.message=message; self.status=status; super().__init__(message)


def validate_zone(value):
    if not isinstance(value,str) or not 1<=len(value)<=100 or '..' in value or value.startswith('/'):
        raise CalendarError('时区无效，请使用手机当前的 IANA 时区。')
    try: return ZoneInfo(value)
    except (ZoneInfoNotFoundError,ValueError): raise CalendarError('时区无效，请使用手机当前的 IANA 时区。') from None


def journal_intent(text):
    """Return done/plan, blocked, or None. A blocked journal never falls into a file tool."""
    done=bool(DONE.search(text)); plan=bool(PLAN.search(text))
    # A reminder remains a reminder unless recording/scheduling was explicitly requested.
    reminder='提醒' in text or bool(re.search(r'\bremind\b',text,re.I))
    if meeting_reminder_intent(text): return 'blocked' if BLOCKED.search(text) else 'plan'
    starts_date=bool(re.match(r'^\s*(?:on\s+)?'+DATE_TOKEN,text,re.I))
    has_time=bool(re.search(r'\d[:：]\d|[零〇一二两三四五六七八九十\d]+[点时]|\bfrom\s+\d|全天|\ball[ -]day\b',text,re.I))
    dated=(bool(FUTURE.search(text)) or (starts_date and has_time)) and not reminder
    candidate=done or plan or dated
    if not candidate: return None
    if BLOCKED.search(text): return 'blocked'
    if reminder and not done and not re.search(r'日历|记下|记录|\bcalendar\b',text,re.I): return None
    if reminder and not done and not plan: return None
    if plan and dated: return 'plan'
    return 'done' if done else 'plan'


def meeting_reminder_intent(text):
    """One explicitly dated meeting interval plus an attendance reminder."""
    return bool(re.search(r'提醒(?:一下)?我(?:参加|出席|去)|\bremind\s+me\s+to\s+(?:attend|join)\b',text,re.I)
        and DATE_RE.search(text) and re.search(r'到|至|\bto\b|–',text,re.I)
        and re.search(r'会议|简报会|会[，,。\s]|meeting|briefing|conference',text,re.I))


def meeting_title(title):
    title=re.sub(r'[，,\s]*提醒(?:一下)?我(?:参加|出席|去)(?:一下)?[。.!！\s]*$','',title)
    title=re.sub(r'[,\s]*remind\s+me\s+to\s+(?:attend|join)[.!\s]*$','',title,flags=re.I)
    # Spoken self-correction belongs to the same named event. Keep the user's
    # final name rather than an earlier ASR fragment, without rewriting times.
    if '就是' in title:
        title=title.rsplit('就是',1)[1].strip(' ，,。')
        clauses=[x.strip() for x in re.split('[，,]',title) if x.strip()]
        if clauses and re.search(r'会|meeting|briefing',clauses[-1],re.I):title=clauses[-1]
    return re.sub(r'^有\s*','',title).strip(' ，,。.!！')


def _localize(naive,zone):
    candidates={}
    for fold in (0,1):
        aware=naive.replace(tzinfo=zone,fold=fold)
        if aware.astimezone(dt.timezone.utc).astimezone(zone).replace(tzinfo=None)==naive:
            candidates[aware.timestamp()]=aware
    if len(candidates)!=1: raise CalendarError('这个时间处于夏令时跳时或重复时段，请明确日期、时间和时区偏移。')
    return next(iter(candidates.values()))


def _number(value):
    if re.fullmatch(r'\d+',value): return int(value)
    digits={'零':0,'〇':0,'一':1,'二':2,'两':2,'三':3,'四':4,'五':5,'六':6,'七':7,'八':8,'九':9}
    if value in digits: return digits[value]
    tens=re.fullmatch(r'([二三四五六七八九]?)十([一二三四五六七八九]?)',value)
    if tens: return (digits[tens[1]] if tens[1] else 1)*10+(digits[tens[2]] if tens[2] else 0)
    zero_padded=re.fullmatch(r'[零〇]([零〇一二三四五六七八九])',value)
    if zero_padded: return digits[zero_padded[1]]
    raise CalendarError('日期或时间数字不明确，请补充。')


def _year(value):
    if re.fullmatch(r'\d{4}',value): return int(value)
    if re.fullmatch(r'[零〇一二三四五六七八九]{4}',value):
        return int(''.join(str('零一二三四五六七八九'.index(character.replace('〇','零'))) for character in value))
    raise CalendarError('年份不明确，请使用完整四位年份。')


def _date(token,today):
    low=token.lower()
    offsets={'今天':0,'昨天':-1,'前天':-2,'大前天':-3,'明天':1,'后天':2,'大后天':3,'刚刚':0,'刚才':0,'today':0,'yesterday':-1,'tomorrow':1,'the day after tomorrow':2,'day after tomorrow':2,'just now':0}
    if low in offsets: return today+dt.timedelta(days=offsets[low])
    if re.fullmatch(RELATIVE_DAY,token): raise CalendarError('相对日期不明确，请用具体年月日确认。')
    if re.fullmatch(r'\d{4}-\d{2}-\d{2}',token): return dt.date.fromisoformat(token)
    month=re.fullmatch(r'(?:('+SPOKEN_NUMBER+r')年)?('+SPOKEN_NUMBER+r')月('+SPOKEN_NUMBER+r')[日号]?',token)
    if month: return dt.date(_year(month[1]) if month[1] else today.year,_number(month[2]),_number(month[3]))
    if re.fullmatch(WEEK_DATE,token):
        weekday='一二三四五六日'.index(token[-1].replace('天','日')) if not token[-1].isdigit() else int(token[-1])-1
        if re.fullmatch(BARE_WEEK_DATE,token):
            return today+dt.timedelta(days=(weekday-today.weekday())%7)
        weeks=1 if token.startswith('下') else 0
        return today+dt.timedelta(days=weeks*7-today.weekday()+weekday)
    if re.search(r'周|星期|礼拜',token):
        raise CalendarError('星期表述不明确或尚不支持，请用具体年月日确认。')
    weekdays=['monday','tuesday','wednesday','thursday','friday','saturday','sunday']
    if not re.fullmatch(r'next\s+(?:'+ '|'.join(weekdays)+r')',low):
        raise CalendarError('日期表述不明确，请用具体年月日确认。')
    weekday=weekdays.index(low.split()[-1])
    return today+dt.timedelta(days=7-today.weekday()+weekday)


def _clock(hour,minute='',period='',inherited=''):
    h=_number(hour); m=30 if minute=='半' else 15 if minute=='一刻' else 45 if minute=='三刻' else _number(minute.rstrip('分')) if minute else 0
    p=(period or inherited).lower().replace('.','')
    if p in ('am','pm') and not 1<=h<=12: raise CalendarError('AM/PM 的小时应为 1 到 12，请确认时间。')
    if p in ('上午','早上','清晨','凌晨') and h>12: raise CalendarError('时刻与上午或凌晨的描述不一致，请确认。')
    if p=='夜里' or (p in ('晚上','晚间','傍晚') and (h<=5 or h==12)):
        raise CalendarError('晚间时刻可能跨午夜，请用完整日期和 24 小时时间确认。')
    if p in ('下午','晚上','晚间','傍晚','pm') and h<12: h+=12
    # Chinese “早上十一点到十二点” ends at noon. Only explicit
    # 凌晨 / 12 AM identifies midnight; it must not turn noon into 00:00.
    if p in ('凌晨','am') and h==12: h=0
    if p=='中午' and h<11: h+=12
    if not 0<=h<=23 or not 0<=m<=59: raise CalendarError('时间超出有效范围，请补充。')
    return (h,m),p


def _english_clock_number(value):
    """Convert only a captured, bounded clock token; never rewrite task titles."""
    if not value: return ''
    if not re.fullmatch(ENGLISH_HOUR+'|'+ENGLISH_MINUTE,value,re.I):
        raise CalendarError('英文时间数字不明确，请补充完整起止时间。')
    words='zero one two three four five six seven eight nine ten eleven twelve thirteen fourteen fifteen sixteen seventeen eighteen nineteen'.split()
    numbers={word:index for index,word in enumerate(words)}
    numbers.update(oh=0,twenty=20,thirty=30,forty=40,fifty=50)
    return str(sum(numbers[word] for word in re.split(r'[\s-]+',value.lower())))


def _times(text,date_spans):
    # A parsed weekday ending in 五 must not be mistaken for an extra digit
    # of the adjacent clock in 周五15:00. Keep all original span offsets.
    chars=list(text)
    for start,end in date_spans:
        if re.fullmatch(WEEK_DATE,text[start:end]): chars[start:end]=' '*(end-start)
    text=''.join(chars)
    ranges=[]; blocked=list(date_spans); period=''
    quoted=[match.span() for match in re.finditer(r'“[^”]*”|‘[^’]*’|《[^》]*》|「[^」]*」|"[^"]*"',text)]
    english=re.compile(r'(?<![\d:.])(?:from\s+)?(\d{1,2})(?:[:.](\d{2}))?\s*('+AMPM_TOKEN+r')?\s*(?:to|until|–|-)\s*(\d{1,2})(?:[:.](\d{2}))?\s*('+AMPM_TOKEN+r')?(?!\d|[.:]\d)',re.I)
    spoken=re.compile(r'\b(?:from|at)\s+('+ENGLISH_HOUR+r')\b(?:\s+('+ENGLISH_MINUTE+r')\b)?\s*('+AMPM_TOKEN+r')?\s*(?:to|until|–|-)\s*('+ENGLISH_HOUR+r')\b(?:\s+('+ENGLISH_MINUTE+r')\b)?\s*('+AMPM_TOKEN+r')?',re.I)
    matches=[(match,False) for match in english.finditer(text)]
    matches.extend((match,True) for match in spoken.finditer(text))
    for match,is_spoken in sorted(matches,key=lambda item:item[0].start()):
        if any(match.start()<end and match.end()>start for start,end in blocked): continue
        if is_spoken:
            if any(match.start()<end and match.end()>start for start,end in quoted): continue
            if re.search(r'\b(?:versions?|releases?|builds?)\s*$',text[:match.start()],re.I): continue
            # Do not accept just "four" from "four hundred", "four point five",
            # or an unsupported/incomplete minute phrase after the endpoint.
            if re.match(r'\s*(?:-?\s*'+ENGLISH_TIME_CONTINUATION+r'|\d|[.:-]\d)',text[match.end():],re.I):
                raise CalendarError('英文时间数字不明确，请补充完整起止时间。')
        values={group:(_english_clock_number(match[group]) if is_spoken else match[group] or '') for group in (1,2,4,5)}
        dotted=any(match[group] and text[match.start(group)-1]=='.' for group in (2,5))
        if dotted:
            # Whisper may render 9:30 as 9.30. Only an explicit from-range
            # can use this spelling; standalone/version decimals stay text.
            if not re.match(r'from\s+',match[0],re.I): continue
            if re.search(r'\b(?:versions?|releases?|builds?)\s*$',text[:match.start()],re.I): continue
        daypart_match=re.search(r'(?:(?:in\s+the)\s+)?(上午|早上|清晨|凌晨|中午|下午|晚上|晚间|夜里|傍晚|\b(?:morning|afternoon|evening|night|noon))\s*$',text[:match.start()],re.I)
        suffix=re.match(r'\s*in\s+the\s+(morning|afternoon|evening|night|noon)\b',text[match.end():],re.I)
        labels=[part[1].lower() for part in (daypart_match,suffix) if part]
        dayparts=[{'morning':'上午','afternoon':'下午','evening':'晚上','night':'夜里','noon':'中午'}.get(label,label) for label in labels]
        daypart=dayparts[0] if dayparts else ''
        # New word ranges require an explicit clock signal. "Chapters from
        # three to four" must not become 03:00–04:00; numeric rules stay intact.
        if is_spoken and not (match[3] or match[6] or dayparts):
            raise CalendarError('英文单词时间请明确 AM/PM 或上午、下午等时段。')
        # The trailing AM/PM commonly qualifies both endpoints, e.g. 3 to 4 PM.
        first,p=_clock(values[1],values[2],match[3] or daypart or match[6] or '')
        second,_=_clock(values[4],values[5],match[6] or '',p)
        # An explicit adjoining daypart must not be silently discarded by the
        # numeric-range branch. Conflicting AM/PM requires clarification.
        for explicit_daypart in dayparts:
            expected_first,_=_clock(values[1],values[2],explicit_daypart)
            expected_second,_=_clock(values[4],values[5],explicit_daypart)
            if first!=expected_first or second!=expected_second:
                raise CalendarError('时段描述与 AM/PM 不一致，请确认完整的起止时间。')
        first_end=match.end(3) if match[3] else match.end(2) if match[2] else match.end(1)
        second_end=match.end(6) if match[6] else match.end(5) if match[5] else match.end(4)
        if suffix: second_end=match.end()+suffix.end()
        first_start=daypart_match.start() if daypart_match else match.start(1)
        ranges.extend([(first_start,first_end,first),(match.start(4),second_end,second)]); blocked.append((match.start(),max(match.end(),second_end)))
    for match in re.finditer(r'\b(?:from|at)\s+'+ENGLISH_HOUR+r'\b',text,re.I):
        if any(match.start()<end and match.end()>start for start,end in blocked+quoted): continue
        if re.search(r'\b(?:versions?|releases?|builds?)\s*$',text[:match.start()],re.I): continue
        raise CalendarError('英文时间范围不完整，请补充明确的开始和结束时间。')
    clock=re.compile(r'(上午|早上|清晨|凌晨|中午|下午|晚上|晚间|夜里|傍晚)?\s*(?<![\d零〇一二两三四五六七八九十百千万:：.])('+SPOKEN_NUMBER+r')(?:[:：](\d{2})(?!\d)|[点时](?:钟)?(半|一刻|三刻|(?:'+SPOKEN_NUMBER+r')分?)?)\s*('+AMPM_TOKEN+r')?',re.I)
    for match in clock.finditer(text):
        clock_start=match.start(1) if match[1] else match.start(2)
        if any(clock_start<end and match.end()>start for start,end in blocked): continue
        minute=match[4] or ''
        if minute and minute not in ('半','一刻','三刻') and not minute.endswith('分'):
            suffix=text[match.end():].lstrip()
            # A bare minute needs a clear boundary. In particular, 四点一课 is
            # neither 04:01 nor 四点一刻; preserve it for human clarification.
            if suffix and not re.match(r'(?:[，,。.!！:：;；?？\-–—]|到|至|安排|记下|记录|记入日历|记到日历|我|完成|结束|\b(?:to|until)\b)',suffix,re.I):
                raise CalendarError('分钟的表述不明确，请用“几点几分”补充完整起止时间。')
        value,period=_clock(match[2],match[3] or match[4] or '',match[1] or match[5] or '',period)
        if match[1] and match[5]:
            explicit,_=_clock(match[2],match[3] or match[4] or '',match[5])
            if explicit!=value: raise CalendarError('时段描述与 AM/PM 不一致，请确认完整的起止时间。')
        ranges.append((clock_start,match.end(),value)); blocked.append((clock_start,match.end()))
    single=re.compile(r'(?<![\d:.])\b(?:at\s+)?(\d{1,2})\s*('+AMPM_TOKEN+r')|\bat\s+(\d{1,2})\b(?![\d:.])',re.I)
    for match in single.finditer(text):
        if any(match.start()<end and match.end()>start for start,end in blocked): continue
        value,_=_clock(match[1] or match[3],'',match[2] or '')
        ranges.append((match.start(),match.end(),value))
    ranges.sort()
    connected=len(ranges)==2 and text[ranges[0][1]:ranges[1][0]].strip().lower() in ('到','至','-','–','—','to','until')
    return [value for _,_,value in ranges],connected,[(start,end) for start,end,_ in ranges]


def short_title(text,date_spans,time_spans,all_day=False):
    """Remove only parsed time spans and explicit command prefixes, without inference."""
    protected=[match.span() for match in re.finditer(r'“[^”]*”|‘[^’]*’|《[^》]*》|「[^」]*」|"[^"]*"',text)]
    def quoted(start,end): return any(start<right and end>left for left,right in protected)
    spans=[]
    date_removed=False
    for start,end in date_spans:
        # Preserve dates that form part of an identifier or a quoted project title.
        if quoted(start,end): continue
        if re.search(r'[A-Za-z0-9_]+[-_/]?$',text[:start]) or re.match(r'[-_/]?[A-Za-z_]',text[end:]):
            continue
        if date_removed: continue # A repeated date word may be part of the activity's name.
        spans.append((start,end))
        date_removed=True
    if time_spans:
        start,end=time_spans[0][0],time_spans[-1][1]
        if not quoted(start,end): spans.append((start,end))
    if all_day:
        spans.extend(match.span() for match in re.finditer(r'全天|\ball[ -]day\b',text,re.I) if not quoted(*match.span()))
    expanded=[]
    for start,end in spans:
        preposition=re.search(r'(?:\b(?:on|for|from|at)|在|于|从)\s*$',text[:start],re.I)
        if preposition and not quoted(preposition.start(),end): start=preposition.start()
        expanded.append((start,end))
    keep=[True]*len(text)
    for start,end in expanded:
        keep[start:end]=[False]*(end-start)
    title=' '.join(''.join(character if keep[index] else ' ' for index,character in enumerate(text)).split())
    title=title.strip(' \t，,。.!！:：;；-–')
    prefix=re.compile(r'^(?:(?:请)?(?:帮我)?(?:安排|记下|记录|记入日历|记到日历)(?:一下)?\s*|(?:我\s*)?(?:(?:已经|刚刚|刚才)\s*)?(?:完成了|做完了|结束了)\s*|我\s*(?:计划|打算|准备|要|会)(?:去)?\s*|I\s+(?:(?:have|just|already)\s+)*(?:finished|completed)\s+|(?:please\s+)?(?:schedule|record|log)\s+|I\s+(?:plan\s+to|will|am\s+going\s+to)\s+)',re.I)
    # Strip one command prefix only. A remaining "Schedule" or "安排" may be
    # the actual project name (e.g. "Schedule Manager" or "安排大师").
    title=prefix.sub('',title,count=1)
    title=title.strip(' \t，,。.!！:：;；-–')
    # Retain the original if stripping leaves no meaningful title; never fabricate one.
    if not title or not any(character.isalnum() for character in title): return text.strip()[:300]
    return title[:300]


def _multiple_title_clauses(title):
    """A comma after a removed time range is punctuation, not a second activity."""
    protected=[match.span() for match in re.finditer(r'“[^”]*”|‘[^’]*’|《[^》]*》|「[^」]*」|"[^"]*"',title)]
    clauses=[]; start=0
    for index,character in enumerate(title):
        if character in ',，' and not any(left<=index<right for left,right in protected):
            clauses.append(title[start:index]); start=index+1
    clauses.append(title[start:])
    return sum(any(character.isalnum() for character in clause) for clause in clauses)>1


def parse_entry(text,created,time_zone=DEFAULT_ZONE):
    zone=validate_zone(time_zone); kind=journal_intent(text)
    if kind not in ('done','plan'): raise CalendarError('这句话不是明确的已做事项或日程安排。')
    now=dt.datetime.fromtimestamp(created,zone)
    entry={'kind':kind,'title':text.strip()[:300],'start':None,'end':None,'time_zone':time_zone,
           'all_day':bool(re.search(r'全天|\ball[ -]day\b',text,re.I)),'status':'needs_details','question':'请补充明确的日期、开始时间和结束时间；全天事项请明确选择全天。','original_text':text}
    if len(text.strip())>300:
        entry['question']='请给这件事补充一个不超过 300 字的标题，并确认时间。'; return entry
    if VAGUE.search(text) or MULTIPLE.search(text):
        entry['question']='这句话包含模糊时间或多件事，请一次确认一件事的标题和准确起止时间。'; return entry
    try:
        dates=list(DATE_RE.finditer(text))
        if kind=='done' and any(re.fullmatch(BARE_WEEK_DATE,match[0]) for match in dates):
            raise CalendarError('已完成事项只说星期无法确定是哪一周，请补充本周、具体日期或昨天等日期。')
        for match in dates:
            prefix=text[:match.start()]
            # These qualifiers are not part of the supported grammar. Do not
            # silently turn "明年十月" into October of the current year, or
            # shorten "下下个星期一" to next week.
            if re.search(r'(?:明|去|前|后)年(?:的)?\s*$',prefix):
                raise CalendarError('请用完整四位年份确认日期。')
            if re.fullmatch(WEEK_DATE,match[0]):
                if prefix.endswith('下') and not prefix.endswith('记下'):
                    raise CalendarError('请用具体年月日确认这次安排。')
                suffix=text[match.end():]
                if re.match(SPOKEN_NUMBER,suffix) and not re.match(SPOKEN_NUMBER+r'(?:[点时]|[:：])',suffix):
                    raise CalendarError('星期数字不明确，请确认具体日期。')
        values={_date(match[0],now.date()) for match in dates}
        if len(values)!=1:
            entry['question']='请明确这件事的具体日期；多件事请分开记录。'; return entry
        date=next(iter(values)); clocks,connected,time_spans=_times(text,[match.span() for match in dates])
        if entry['all_day']:
            if clocks: raise CalendarError('同时出现全天和具体时刻，请确认要采用哪一种。')
            start=_localize(dt.datetime.combine(date,dt.time()),zone)
            end=_localize(dt.datetime.combine(date+dt.timedelta(days=1),dt.time()),zone)
        else:
            if len(clocks)!=2:
                if clocks:
                    value=_localize(dt.datetime.combine(date,dt.time(*clocks[0])),zone); entry['start']=value.isoformat()
                entry['question']='请补充明确的开始和结束时间；不会自动添加默认时长。' if len(clocks)<2 else '检测到多个时刻，请一次确认一件事的起止时间。'
                return entry
            if not connected:
                entry['question']='两个时刻没有明确连接为同一事项的起止范围，请逐件确认。'; return entry
            start=_localize(dt.datetime.combine(date,dt.time(*clocks[0])),zone)
            end=_localize(dt.datetime.combine(date,dt.time(*clocks[1])),zone)
        if end.timestamp()<=start.timestamp(): raise CalendarError('结束时间必须晚于开始时间；跨午夜请补充完整日期。')
        if kind=='done' and end.timestamp()>created+60: raise CalendarError('已做事项的结束时间仍在未来，请确认完成时间。')
        if kind=='plan' and start.timestamp()<created-60: raise CalendarError('计划开始时间已经过去，请确认日期或作为历史事项补录。')
        title=short_title(text,[match.span() for match in dates],time_spans,entry['all_day'])
        if meeting_reminder_intent(text): title=meeting_title(title)
        if _multiple_title_clauses(title):
            entry['question']='时间范围之外仍有多个事项，请逐件确认标题和起止时间。'; return entry
        entry.update(start=start.isoformat(),end=end.isoformat(),status='ready',question='',
                     title=title)
    except (ValueError,OverflowError) as error:
        entry['question']=error.message if isinstance(error,CalendarError) else '日期或时间无效，请补充准确的日期和起止时间。'
    return entry


def _iso(value,zone):
    if not isinstance(value,str) or not re.fullmatch(r'\d{4}-\d{2}-\d{2}T\d{2}:\d{2}(?::\d{2}(?:\.\d{1,6})?)?(?:Z|[+-]\d{2}:\d{2})',value):
        raise CalendarError('请提供含时区的 ISO8601 开始和结束时间。')
    try:
        parsed=dt.datetime.fromisoformat(value.replace('Z','+00:00')); local=parsed.astimezone(zone)
    except (ValueError,OverflowError): raise CalendarError('日期或时间无效。') from None
    if parsed.replace(tzinfo=None)!=local.replace(tzinfo=None) or parsed.utcoffset()!=local.utcoffset():
        raise CalendarError('时间偏移与所选时区不一致，或处于夏令时跳时，请重新确认。')
    return parsed


def resolved_fields(body):
    required={'title','start','end','time_zone','all_day','kind','revision'}
    if not isinstance(body,dict) or set(body)!=required or any(not isinstance(v,str) for v in body.values()):
        raise CalendarError('请提交标题、起止时间、时区、全天标记、事项类型及当前版本。')
    title=body['title'].strip()
    if not 0<len(title)<=300 or any(ord(c)<32 for c in title): raise CalendarError('日历标题无效。')
    if body['kind'] not in ('done','plan') or body['all_day'] not in ('true','false'): raise CalendarError('日历类型或全天标记无效。')
    zone=validate_zone(body['time_zone']); start=_iso(body['start'],zone); end=_iso(body['end'],zone)
    if end.timestamp()<=start.timestamp(): raise CalendarError('结束时间必须晚于开始时间。')
    if body['all_day']=='true' and (start.time()!=dt.time() or end.time()!=dt.time()):
        raise CalendarError('全天事项的开始和结束应为当地午夜，结束日期不包含在事项内。')
    return {'title':title,'start':start.isoformat(),'end':end.isoformat(),'time_zone':body['time_zone'],
            'all_day':body['all_day']=='true','kind':body['kind']}


def _revision(value):
    if not isinstance(value,str) or not re.fullmatch(r'[1-9]\d{0,8}',value): raise CalendarError('日历版本无效，请刷新后重试。')
    return int(value)


class CalendarJournal:
    def __init__(self,store): self.store=store

    @staticmethod
    def initialize(c):
        columns={row['name'] for row in c.execute('PRAGMA table_info(tasks)')}
        if 'owner' not in columns: c.execute("ALTER TABLE tasks ADD COLUMN owner TEXT NOT NULL DEFAULT ''")
        # Never guess an owner when an old task has no request mapping or ambiguous mappings.
        c.execute("UPDATE tasks SET owner=(SELECT min(device) FROM task_requests WHERE task=tasks.id) WHERE owner='' AND (SELECT count(DISTINCT device) FROM task_requests WHERE task=tasks.id)=1")
        c.executescript('''CREATE TABLE IF NOT EXISTS calendar_entries(
            id TEXT PRIMARY KEY,owner TEXT NOT NULL,kind TEXT,title TEXT,start TEXT,end TEXT,time_zone TEXT,
            all_day INTEGER,status TEXT,revision INTEGER,question TEXT,device_sync TEXT,sync_error TEXT,
            original_text TEXT,created REAL,updated REAL,last_op TEXT,last_hash TEXT,previous_revision INTEGER);
            CREATE INDEX IF NOT EXISTS calendar_owner_sync_created ON calendar_entries(owner,device_sync,created,id);''')

    @staticmethod
    def public(row):
        fields=('id','kind','title','start','end','time_zone','all_day','status','revision','question','device_sync','sync_error','original_text')
        entry={name:row[name] for name in fields}; entry['all_day']=bool(entry['all_day'])
        if entry['kind']=='plan' and entry['status']=='ready' and meeting_reminder_intent(entry['original_text']):entry['reminder_at']=entry['start']
        return entry

    @staticmethod
    def _owned(c,ident,owner):
        row=c.execute('SELECT * FROM calendar_entries WHERE id=? AND owner=?',(ident,owner)).fetchone()
        if not owner or not row: raise CalendarError('未找到属于当前设备的日历事项。',404)
        return row

    def _task_result(self,c,entry):
        if entry['status']=='cancelled':
            message='已从手机 PersonalOS 日历移除。' if entry['device_sync']=='removed' else '已取消，等待手机同步移除 PersonalOS 日历中的事项。'; state='cancelled'
        elif entry['status']=='needs_details': message=entry['question']; state='needs_details'
        else:
            message='已写入手机 PersonalOS 日历。' if entry['device_sync']=='written' else '已整理日程，待写入手机 PersonalOS 日历。'
            state='completed'
            if entry.get('reminder_at') and dt.datetime.fromisoformat(entry['reminder_at']).timestamp()>time.time():
                state='scheduled';message+=' 开始时提醒你参加。'
        if entry['device_sync']=='error': message='手机日历同步暂未完成，请在手机查看并处理提示。'
        c.execute('UPDATE tasks SET state=?,result=?,plan=? WHERE id=?',(state,json.dumps({'message':message,'calendar_entry':entry},ensure_ascii=False),json.dumps({'calendar_entry':entry},ensure_ascii=False),entry['id']))
        if state=='scheduled':c.execute('UPDATE tasks SET due=? WHERE id=?',(dt.datetime.fromisoformat(entry['reminder_at']).timestamp(),entry['id']))

    def create(self,row):
        owner=row.get('owner','')
        if not owner: raise CalendarError('此任务没有可确认的手机归属，请从手机重新提交。')
        body=json.loads(row['body']); fields=parse_entry(body['text'],row['created'],body.get('time_zone',DEFAULT_ZONE))
        with self.store.db() as c:
            c.execute('BEGIN IMMEDIATE')
            existing=c.execute('SELECT * FROM calendar_entries WHERE id=?',(row['id'],)).fetchone()
            if existing: return self.public(self._owned(c,row['id'],owner))
            c.execute('INSERT INTO calendar_entries VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)',(
                row['id'],owner,fields['kind'],fields['title'],fields['start'],fields['end'],fields['time_zone'],int(fields['all_day']),fields['status'],1,fields['question'],'pending','',fields['original_text'],row['created'],time.time(),'create','',0))
            entry=self.public(self._owned(c,row['id'],owner)); self._task_result(c,entry)
            self.store._audit(c,row['id'],'calendar_proposed',{'status':entry['status'],'revision':1,'kind':entry['kind']})
            self.store._notify(c,row['id'],'completed' if entry['status']=='ready' else 'awaiting_confirmation')
            return entry

    def entries(self,owner,cursor='',limit=100):
        if not owner: raise CalendarError('需要设备认证。',401)
        try:
            limit=int(limit)
            if not 1<=limit<=100: raise ValueError()
        except (ValueError,TypeError): raise CalendarError('分页数量必须为 1 到 100。') from None
        args=[owner]; where=''
        rank="CASE WHEN device_sync IN ('pending','error') THEN 0 ELSE 1 END"
        if cursor:
            try:
                if len(cursor)>256: raise ValueError()
                value=json.loads(base64.urlsafe_b64decode(cursor+'='*(-len(cursor)%4)))
                if not isinstance(value,list) or len(value)!=3 or type(value[0]) is not int or value[0] not in (0,1) or type(value[1]) not in (int,float) or not math.isfinite(value[1]) or not isinstance(value[2],str) or not re.fullmatch(r'[0-9a-f-]{36}',value[2]): raise ValueError()
                where=' AND ('+rank+',created,id) > (?,?,?)'; args.extend(value)
            except (ValueError,TypeError,KeyError,binascii.Error): raise CalendarError('分页标记无效，请刷新日历事项。') from None
        with self.store.db() as c:
            rows=c.execute('SELECT *,'+rank+' AS sync_rank FROM calendar_entries WHERE owner=?'+where+' ORDER BY sync_rank,created,id LIMIT ?',args+[limit+1]).fetchall()
        visible=rows[:limit]; next_cursor=None
        if len(rows)>limit:
            last=visible[-1]; next_cursor=base64.urlsafe_b64encode(json.dumps([last['sync_rank'],last['created'],last['id']],separators=(',',':')).encode()).decode().rstrip('=')
        links=self.store.planner.links(owner) if hasattr(self.store,'planner') else {}
        return {'entries':[{**self.public(row),**({'todo_id':links[row['id']],'auto_manual':row['last_op']=='resolve'} if row['id'] in links else {})} for row in visible],'next_cursor':next_cursor}

    def resolve(self,ident,owner,body):
        fields=resolved_fields(body); revision=_revision(body['revision'])
        digest=hashlib.sha256(json.dumps(fields,sort_keys=True,separators=(',',':')).encode()).hexdigest()
        with self.store.db() as c:
            c.execute('BEGIN IMMEDIATE'); row=self._owned(c,ident,owner)
            if row['revision']!=revision:
                if row['last_op']=='resolve' and row['previous_revision']==revision and row['revision']==revision+1 and row['last_hash']==digest: return self.public(row)
                raise CalendarError('事项已更新，请刷新后再保存。',409)
            if row['status']=='cancelled': raise CalendarError('事项已取消，请重新提交新事项。',409)
            c.execute("UPDATE calendar_entries SET title=?,start=?,end=?,time_zone=?,all_day=?,kind=?,status='ready',revision=revision+1,question='',device_sync='pending',sync_error='',updated=?,last_op='resolve',last_hash=?,previous_revision=? WHERE id=?",(
                fields['title'],fields['start'],fields['end'],fields['time_zone'],int(fields['all_day']),fields['kind'],time.time(),digest,revision,ident))
            entry=self.public(self._owned(c,ident,owner)); self._task_result(c,entry)
            self.store._audit(c,ident,'calendar_resolved',{'revision':entry['revision']}); return entry

    def cancel(self,ident,owner,body):
        if not isinstance(body,dict) or set(body)!={'revision'}: raise CalendarError('请提供当前版本以取消事项。')
        revision=_revision(body['revision'])
        with self.store.db() as c:
            c.execute('BEGIN IMMEDIATE'); row=self._owned(c,ident,owner)
            if row['status']=='cancelled' and revision in (row['revision'],row['previous_revision']): return self.public(row)
            if revision!=row['revision']: raise CalendarError('事项已更新，请刷新后再取消。',409)
            c.execute("UPDATE calendar_entries SET status='cancelled',revision=revision+1,device_sync='pending',sync_error='',question='',updated=?,last_op='cancel',last_hash='',previous_revision=revision WHERE id=?",(time.time(),ident))
            entry=self.public(self._owned(c,ident,owner)); self._task_result(c,entry)
            self.store._audit(c,ident,'calendar_cancelled',{'revision':entry['revision']}); return entry

    def ack(self,ident,owner,body):
        if not isinstance(body,dict) or set(body)-{'revision','status','message'} or any(not isinstance(value,str) for value in body.values()): raise CalendarError('日历同步回执无效。')
        revision=_revision(body.get('revision')); state=body.get('status'); message=body.get('message','')
        if state not in ('written','removed','error') or len(message)>400: raise CalendarError('日历同步回执无效。')
        with self.store.db() as c:
            c.execute('BEGIN IMMEDIATE'); row=self._owned(c,ident,owner)
            if row['revision']!=revision: raise CalendarError('事项版本已改变，请刷新后同步。',409)
            if row['status'] not in ('ready','cancelled') or (state=='written' and row['status']!='ready') or (state=='removed' and row['status']!='cancelled'):
                raise CalendarError('同步回执与当前事项状态不一致。',409)
            if row['device_sync']==state or (state=='error' and row['device_sync'] in ('written','removed')): return self.public(row)
            error=' '.join(message.split()) if state=='error' else ''
            c.execute('UPDATE calendar_entries SET device_sync=?,sync_error=?,updated=? WHERE id=?',(state,error,time.time(),ident))
            entry=self.public(self._owned(c,ident,owner)); self._task_result(c,entry)
            self.store._audit(c,ident,'calendar_sync_ack',{'revision':revision,'status':state}); return entry
