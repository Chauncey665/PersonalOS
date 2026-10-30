"""Choose one bounded reminder slot from a fresh, complete calendar snapshot.

No calendar titles, device APIs, models, or persistent state are used here.
snapshot_at is supplied separately by the caller: submission time can proxy an
immediately generated phone snapshot, but it must not refresh an old payload.
"""
import datetime as dt
import math
import re

from calendar_journal import CHINESE_DATE, DATE_RE, SPOKEN_NUMBER, _date, _localize, validate_zone


DAYPART = re.compile(r'凌晨|早上|早晨|清晨|上午|中午|下午|傍晚|晚上|晚间|夜里|\b(?:morning|afternoon|evening|night|noon)\b', re.I)
HOUR_WORD = r'(?:one|two|three|four|five|six|seven|eight|nine|ten|eleven|twelve)'
EXACT_CLOCK = re.compile(
    r'\d\s*[:：.]\s*\d|[\d零〇一二两三四五六七八九十百]+[点时]'
    r'|(?:上午|下午|中午|晚上)\s*[\d零〇一二两三四五六七八九十]'
    r'|\b(?:[ap]m|[ap]\.m\.?)\b'
    r'|\b(?:at|from)\s+(?:\d|'+HOUR_WORD+r'\b)'
    r'|\b'+HOUR_WORD+r'\s+(?:oh|zero|ten|eleven|twelve|thirteen|fourteen|fifteen|sixteen|seventeen|eighteen|nineteen|twenty|thirty|forty|fifty)\b'
    r'|\b(?:morning|afternoon|evening|night|noon)\s+(?:\d|'+HOUR_WORD+r'\b)', re.I)
RELATIVE_DURATION = re.compile(
    r'(?:'+SPOKEN_NUMBER+r'|半|一刻)\s*(?:个)?\s*(?:秒钟?|分钟?|小时|钟头)'
    r'|'+SPOKEN_NUMBER+r'\s*刻钟?'
    r'|(?:'+SPOKEN_NUMBER+r'|半)\s*(?:个)?\s*(?:天|周|星期|月|年)\s*(?:后|内)'
    r'|\b\d+\s*[smh]\b'
    r'|\b(?:\d+|an?|half|'+HOUR_WORD+r'|thirteen|fourteen|fifteen|sixteen|seventeen|eighteen|nineteen|twenty|thirty|forty|fifty)\s+(?:seconds?|minutes?|hours?|secs?|mins?|hrs?|days?|weeks?|months?|years?)\b', re.I)


def _timestamp(value):
    if type(value) not in (int, float):
        raise ValueError('任务或日历快照时间无效。')
    try:
        if not math.isfinite(value): raise ValueError
        return float(value)
    except (ValueError, OverflowError):
        raise ValueError('任务或日历快照时间无效。') from None


def _instant(value):
    if not isinstance(value, str) or not re.fullmatch(
            r'\d{4}-\d{2}-\d{2}T\d{2}:\d{2}(?::\d{2}(?:\.\d{1,6})?)?(?:Z|[+-]\d{2}:\d{2})', value):
        raise ValueError('日历时间必须包含日期和时区。')
    try:
        return dt.datetime.fromisoformat(value.replace('Z', '+00:00')).timestamp()
    except (ValueError, OverflowError, OSError):
        raise ValueError('日历时间无效。') from None


def _target_day(text, created, zone):
    if not isinstance(text, str) or not text.strip() or len(text) > 4000:
        raise ValueError('请提供一条明确的提醒事项。')
    parts = list(DAYPART.finditer(text))
    if len(parts) != 1 or parts[0][0].lower() not in ('下午', 'afternoon'):
        raise ValueError('目前仅支持为一个明确日期的下午选择空档。')
    if EXACT_CLOCK.search(text):
        raise ValueError('这条提醒已有具体时刻，请按明确时刻处理。')
    if RELATIVE_DURATION.search(text):
        raise ValueError('这条提醒包含相对时长，请按明确时长处理。')
    if re.search(r'大概|左右|可能|改天|有空|稍后|一会儿?|过会|之前|之后|提前|以后|不要|取消|不用|不需要|(?:^|请)别|别提醒|假如|如果|\b(?:around|maybe|before|after|unless|if|cancel|not|never|don[’\x27]t)\b', text, re.I):
        raise ValueError('请确认这条提醒的具体日期和时段。')
    if re.search(r'(?:本|这|下|上)(?:个)?(?:周|星期|月|年)|(?:星期|周)[一二三四五六日天末1-7]|(?:今|明|后|去|前)年|今晚|明晚|\b(?:monday|tuesday|wednesday|thursday|friday|saturday|sunday|(?:next|last|this)\s+(?:week|month|year))\b', text, re.I):
        raise ValueError('请使用今天、明天或具体年月日，不会猜测其他日期。')
    try:
        anchor = dt.datetime.fromtimestamp(created, zone).date()
        matches = list(DATE_RE.finditer(text))
        if len(matches) > 1:
            raise ValueError('请一次只为一个明确日期选择提醒空档。')
        if not matches:
            if re.search(SPOKEN_NUMBER+r'[年月日号]|\d{4}[-/]\d', text):
                raise ValueError('日期不完整，请补充具体年月日。')
            # The unqualified "下午" remains anchored to task reception.
            return anchor
        match = matches[0]
        token = match[0]
        if not (token.lower() in ('今天', '明天', 'today', 'tomorrow') or
                re.fullmatch(r'\d{4}-\d{2}-\d{2}', token) or re.fullmatch(CHINESE_DATE, token)):
            raise ValueError('请使用今天、明天或具体年月日。')
        if re.search(r'(?:明|去|前|后)年(?:的)?\s*$|\d$', text[:match.start()]):
            raise ValueError('请使用完整准确的年月日。')
        return _date(token, anchor)
    except (OverflowError, OSError):
        raise ValueError('提醒日期无效。') from None


def choose_reminder_slot(text, created, time_zone, calendar_context, now, snapshot_at):
    """Return a 15-minute afternoon slot, or raise without guessing a free day.

The caller must ensure snapshot_at identifies this exact snapshot. A fresh
submission timestamp is only a proxy when the phone just queried its calendar.
ISO event/range timestamps may use UTC; timeZone declares the local day frame.
"""
    zone = validate_zone(time_zone)
    created, now, snapshot_at = map(_timestamp, (created, now, snapshot_at))
    if created > now or not 0 <= now - snapshot_at <= 300:
        raise ValueError('日历快照已过期或时间无效，请刷新日历后重试。')
    day = _target_day(text, created, zone)
    try:
        snapshot_day = dt.datetime.fromtimestamp(snapshot_at, zone).date()
        current_day = dt.datetime.fromtimestamp(now, zone).date()
        day_start = _localize(dt.datetime.combine(day, dt.time()), zone).timestamp()
        day_end = _localize(dt.datetime.combine(day + dt.timedelta(days=1), dt.time()), zone).timestamp()
        window_start = _localize(dt.datetime.combine(day, dt.time(13)), zone).timestamp()
        window_end = _localize(dt.datetime.combine(day, dt.time(18)), zone).timestamp()
    except (ValueError, OverflowError, OSError):
        raise ValueError('提醒日期或当地时区无法确定，请确认。') from None
    context = calendar_context
    if not isinstance(context, dict) or context.get('truncated') is not False:
        raise ValueError('需要未截断的完整日历快照，才能按空档安排提醒。')
    if context.get('timeZone') != time_zone:
        raise ValueError('日历快照时区与提醒时区不一致，请刷新。')
    if context.get('date') != snapshot_day.isoformat() or snapshot_day != current_day:
        raise ValueError('日历快照日期已过期，请刷新。')
    range_start = _instant(context.get('range_start'))
    range_end = _instant(context.get('range_end'))
    if range_start >= range_end or range_start > day_start or range_end < day_end:
        raise ValueError('日历快照未完整覆盖目标日期，请刷新该日的日历。')
    events = context.get('events')
    if not isinstance(events, list):
        raise ValueError('日历快照缺少完整事件列表。')
    busy = []
    for event in events:
        if not isinstance(event, dict) or type(event.get('isAllDay')) is not bool:
            raise ValueError('日历事件格式不完整，请刷新。')
        start, end = _instant(event.get('start')), _instant(event.get('end'))
        if end <= start:
            raise ValueError('日历事件的起止时间无效，请刷新。')
        if event['isAllDay'] and start < day_end and end > day_start:
            raise ValueError('目标日期已有全天事项，不会自动安排提醒空档。')
        start, end = max(window_start, start - 600), min(window_end, end + 600)
        if start < end:
            busy.append((start, end))
    merged = []
    for start, end in sorted(busy):
        if merged and start <= merged[-1][1]:
            merged[-1] = (merged[-1][0], max(merged[-1][1], end))
        else:
            merged.append((start, end))
    candidate = math.ceil(max(window_start, now + 600) / 300) * 300
    for start, end in merged:
        if candidate + 900 <= start:
            break
        if candidate < end:
            candidate = math.ceil(end / 300) * 300
    if candidate + 900 > window_end:
        raise ValueError('这个下午没有满足条件的 15 分钟空档；不会自动改到次日。')
    return {
        'due': dt.datetime.fromtimestamp(candidate, zone).isoformat(),
        'end': dt.datetime.fromtimestamp(candidate + 900, zone).isoformat(),
        'time_zone': time_zone,
        'date': day.isoformat(),
        'duration_minutes': 15,
        'buffer_minutes': 10,
        'reason': '依据日历空档预留 15 分钟，并避开已有日程前后各 10 分钟。',
    }
