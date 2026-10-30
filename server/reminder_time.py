"""Exact reminder times only; no default hour, date rollover, or model inference."""
import datetime as dt
import math
import re

from calendar_journal import (
    CalendarError, DATE_RE, DEFAULT_ZONE, SPOKEN_NUMBER, VAGUE, WEEK_DATE,
    _clock, _date, _localize, _number, validate_zone,
)

PERIOD = r'上午|早上|清晨|凌晨|中午|下午|晚上|晚间|夜里|傍晚'
AMPM = r'(?:[ap]m|[ap]\.m\.?)(?![A-Za-z])'
NUMBER_BOUNDARY = r'[\d零〇一二两三四五六七八九十百千万:.：+\-]'
ZH_CLOCK = re.compile(
    r'(?P<period>' + PERIOD + r')?\s*(?<!' + NUMBER_BOUNDARY + r')'
    r'(?P<hour>' + SPOKEN_NUMBER + r')'
    r'(?:[:：](?P<colon>\d{2})(?!\d|[:：]\d)|[点时](?:钟)?'
    r'(?P<minute>半|一刻|三刻|(?:' + SPOKEN_NUMBER + r')分?)?)'
    r'\s*(?P<ampm>' + AMPM + r')?', re.I,
)
EN_CLOCK = re.compile(
    r'(?<![\w:.+\-])(?:at\s+)?(?P<hour>\d{1,2})\s*(?P<ampm>' + AMPM + r')', re.I,
)
RELATIVE = re.compile(
    r'(?<!' + NUMBER_BOUNDARY + r')(?P<zh>' + SPOKEN_NUMBER + r')(?:个)?'
    r'(?P<zh_unit>分钟|小时)(?:以后|之后|后)'
    r'|(?:过|再过)\s*(?P<after>' + SPOKEN_NUMBER + r')(?:个)?(?P<after_unit>分钟|小时)'
    r'|\b(?:in|after)\s+(?P<en>\d+)\s*(?P<en_unit>minutes?|hours?)\b', re.I,
)
UNSUPPORTED_DATE = re.compile(
    r'明年|去年|后年|前年|下下|(?:上|下|本|这|每|隔)(?:个)?(?:周|星期|礼拜|月|年)'
    r'|(?:周|星期|礼拜)[一二三四五六七八九十零〇\d日天]|(?:明|昨|前|后)(?:早|晚)'
    r'|\b(?:tonight|tonite|last\s+(?:week|monday|tuesday|wednesday|thursday|friday|saturday|sunday))\b'
    r'|\b(?:monday|tuesday|wednesday|thursday|friday|saturday|sunday)\b'
    r'|\b(?:next|last|this)\s+(?:week|month|year)\b', re.I,
)


class ReminderTimeError(ValueError):
    def __init__(self, code, message):
        self.code, self.message = code, message
        super().__init__(message)


class ReminderNeedsDetails(ReminderTimeError):
    """No exact reminder time; caller may use an explicitly authorized planner."""
    def __init__(self, code, message=None):
        super().__init__(code if message is not None else 'needs_details',
                         message if message is not None else code)


def _overlap(span, spans):
    return any(span[0] < end and span[1] > start for start, end in spans)


def _reference(created, time_zone):
    if isinstance(created, bool) or not isinstance(created, (int, float)):
        raise ReminderTimeError('invalid_reference', '提醒的提交时间无效，请重新提交。')
    try:
        if not math.isfinite(created): raise ValueError()
        zone = validate_zone(time_zone)
        return dt.datetime.fromtimestamp(created, zone), zone
    except (CalendarError, ValueError, OverflowError, OSError):
        raise ReminderTimeError('invalid_reference', '提醒的提交时间或手机时区无效。') from None


def _date_matches(text, now, protected):
    matches = [match for match in DATE_RE.finditer(text) if not _overlap(match.span(), protected)]
    for match in matches:
        prefix, suffix = text[:match.start()], text[match.end():]
        if re.search(r'(?:明|去|前|后)年(?:的)?\s*$', prefix):
            raise ReminderNeedsDetails('ambiguous_date', '请用完整年月日明确提醒日期。')
        if re.search(r'周|星期|礼拜',match[0]) and not re.fullmatch(WEEK_DATE,match[0]):
            raise ReminderNeedsDetails('ambiguous_date', '星期表述不明确或尚不支持，请用具体年月日确认提醒日期。')
        if re.fullmatch(WEEK_DATE, match[0]):
            if prefix.endswith('下') and not prefix.endswith('记下'):
                raise ReminderNeedsDetails('ambiguous_date', '请用完整年月日明确提醒日期。')
            if re.match(SPOKEN_NUMBER, suffix) and not re.match(SPOKEN_NUMBER + r'(?:[点时]|[:：])', suffix):
                raise ReminderNeedsDetails('ambiguous_date', '星期数字不明确，请确认提醒日期。')
    covered = protected + [match.span() for match in matches]
    if any(not _overlap(match.span(), covered) for match in UNSUPPORTED_DATE.finditer(text)):
        raise ReminderNeedsDetails('ambiguous_date', '请明确提醒日期，例如今天、明天或完整年月日。')
    try:
        dates = {_date(match[0], now.date()) for match in matches}
    except (CalendarError, ValueError, OverflowError):
        raise ReminderTimeError('invalid_date', '提醒日期无效或不明确，请确认日期。') from None
    if len(dates) > 1:
        raise ReminderTimeError('conflicting_date', '提醒中出现不同日期，请一次安排一个提醒。')
    return matches, next(iter(dates)) if dates else now.date()


def _absolute_clocks(text, blocked):
    # A parsed weekday's last digit belongs to the date, not an adjacent
    # clock. Preserve offsets and the saved original transcript.
    chars=list(text)
    for start,end in blocked:
        if re.fullmatch(WEEK_DATE,text[start:end]): chars[start:end]=' '*(end-start)
    text=''.join(chars)
    clocks = []
    for expression in (ZH_CLOCK, EN_CLOCK):
        for match in expression.finditer(text):
            values = match.groupdict()
            clock_start=match.start('period') if values.get('period') else match.start('hour')
            clock_span=(clock_start,match.end())
            if _overlap(clock_span, blocked): continue
            period, ampm = values.get('period') or '', values.get('ampm') or ''
            minute = values.get('colon') or values.get('minute') or ''
            suffix = text[match.end():].lstrip()
            if (values.get('minute') and minute not in ('半', '一刻', '三刻') and not minute.endswith('分')
                    and suffix and not re.match(r'[，,。.!！:：;；?？\-–—]|提醒|通知|叫我|让我|我|去|拿|取|到|至', suffix)):
                raise ReminderNeedsDetails('ambiguous_time', '分钟的表述不明确，请补充完整提醒时刻。')
            if re.match(SPOKEN_NUMBER + r'秒', suffix):
                raise ReminderNeedsDetails('ambiguous_time', '请用小时和分钟明确提醒时刻。')
            try:
                hour = _number(values['hour'])
                if not period and not ampm and not values.get('colon') and 1 <= hour <= 12:
                    raise ReminderNeedsDetails('ambiguous_time', '请明确上午或下午，也可以使用 24 小时时间。')
                value, _ = _clock(values['hour'], minute, period or ampm)
                if period and ampm and _clock(values['hour'], minute, ampm)[0] != value:
                    raise ReminderTimeError('conflicting_time', '提醒的时段与 AM/PM 不一致，请确认时刻。')
                prefix_period = re.search('(' + PERIOD + r')\s*$', text[:match.start()])
                english_prefix = re.search(r'\b(?:in\s+the\s+)?(morning|afternoon|evening|night|noon)\s*(?:at\s*)?$', text[:match.start()], re.I)
                english_suffix = re.match(r'\s*in\s+the\s+(morning|afternoon|evening|night|noon)\b', suffix, re.I)
                for adjacent in (prefix_period, english_prefix, english_suffix):
                    if adjacent:
                        label = adjacent[1].lower()
                        label = {'morning':'上午', 'afternoon':'下午', 'evening':'晚上', 'night':'夜里', 'noon':'中午'}.get(label, label)
                        if _clock(values['hour'], minute, label)[0] != value:
                            raise ReminderTimeError('conflicting_time', '提醒的时段描述不一致，请确认时刻。')
            except CalendarError:
                raise ReminderTimeError('invalid_time', '提醒时刻无效，请确认小时、分钟和时段。') from None
            clocks.append((clock_span, value))
            blocked.append(clock_span)
    return clocks


def parse_reminder_time(text, created, time_zone=DEFAULT_ZONE):
    """Return a future Unix timestamp derived from the original submission time.

    A clock without a date means the submission's local day, never tomorrow.
    A daypart alone raises ReminderNeedsDetails('missing_time', ...). Relative
    integer minutes/hours are elapsed durations, independent of queue delay/DST.
    """
    if not isinstance(text, str) or not 0 < len(text.strip()) <= 4000:
        raise ReminderTimeError('invalid_text', '提醒内容无效，请重新提交。')
    trigger=re.search(r'提醒|\bremind(?:er)?\b',text,re.I)
    if trigger and re.search(r'不要|不用|不需要|取消|别|\b(?:do\s+not|don[’\x27]t|never|cancel)\b',text[:trigger.start()],re.I):
        raise ReminderTimeError('negated_request', '没有创建提醒：这条表达包含否定或取消，请在原任务中操作取消。')
    now, zone = _reference(created, time_zone)
    protected = [match.span() for match in re.finditer(r'“[^”]*”|‘[^’]*’|《[^》]*》|「[^」]*」|"[^"]*"', text)]
    if any(not _overlap(match.span(), protected) for match in VAGUE.finditer(text)):
        raise ReminderNeedsDetails('ambiguous_time', '提醒时间不明确，请补充或选择一个时间。')
    dates, date = _date_matches(text, now, protected)
    relatives = [match for match in RELATIVE.finditer(text) if not _overlap(match.span(), protected)]
    blocked = protected + [match.span() for match in dates] + [match.span() for match in relatives]
    clocks = _absolute_clocks(text, blocked)
    unsupported_relative = re.compile(r'(?:分钟|小时)(?:以后|之后|后)|\b(?:in|after)\s+[+\-]?\d+(?:[.,]\d+)?\s*(?:minutes?|hours?)\b', re.I)
    if any(not _overlap(match.span(), blocked) for match in unsupported_relative.finditer(text)):
        raise ReminderNeedsDetails('ambiguous_time', '提醒时长尚未完整识别，请确认分钟或小时数。')
    if len(relatives) > 1 or len(clocks) > 1:
        raise ReminderNeedsDetails('multiple_times', '检测到多个提醒时刻，请一次安排一个提醒。')
    if relatives:
        match = relatives[0]
        if clocks or dates:
            raise ReminderTimeError('conflicting_time', '相对时长与日期或时刻同时出现，请明确采用哪一种。')
        remaining = text[:match.start()] + text[match.end():]
        if re.search(r'分钟|小时|\b(?:minutes?|hours?)\b|' + PERIOD, remaining, re.I):
            raise ReminderNeedsDetails('ambiguous_time', '提醒时长包含其他时间描述，请补充明确的时长。')
        try:
            number = _number(match['zh'] or match['after'] or match['en'])
            unit = match['zh_unit'] or match['after_unit'] or match['en_unit'].lower()
            seconds = number * (3600 if unit in ('小时', 'hour', 'hours') else 60)
            if number <= 0 or seconds > 366 * 86400:
                raise ValueError()
            due = created + seconds
            dt.datetime.fromtimestamp(due, zone)
        except (CalendarError, ValueError, OverflowError, OSError):
            raise ReminderTimeError('invalid_duration', '提醒时长无效，请使用一年以内的正整数分钟或小时。') from None
        return float(due)
    if not clocks:
        raise ReminderNeedsDetails('missing_time', '尚未确定提醒时刻。')
    try:
        due = _localize(dt.datetime.combine(date, dt.time(*clocks[0][1])), zone).timestamp()
    except (CalendarError, ValueError, OverflowError, OSError):
        raise ReminderNeedsDetails('ambiguous_time', '这个本地时刻无效或存在夏令时歧义，请明确提醒时间。') from None
    if due <= created:
        raise ReminderTimeError('past_time', '提醒时间已经过去，请明确新的日期或时刻。')
    return due
