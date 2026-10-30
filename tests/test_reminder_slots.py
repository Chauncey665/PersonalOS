"""Calendar gap selection uses synthetic snapshots only; no device calendars."""
import copy
import datetime as dt
import pathlib
import sys
import unittest
from zoneinfo import ZoneInfo

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / 'server'))
from reminder_slots import choose_reminder_slot


ZONE = 'Asia/Hong_Kong'


def stamp(value):
    return dt.datetime.fromisoformat(value).timestamp()


NOW = stamp('2026-10-27T13:24:00+08:00')
UNSET = object()


def event(start, end, all_day=False, **extra):
    return dict(start=start, end=end, isAllDay=all_day, **extra)


def context(events=UNSET, **extra):
    value = {'date': '2026-10-27', 'timeZone': ZONE, 'truncated': False,
             'range_start': '2026-10-27T00:00:00+08:00',
             'range_end': '2026-10-28T00:00:00+08:00', 'events': [] if events is UNSET else events}
    value.update(extra)
    return value


class ReminderSlotTests(unittest.TestCase):
    def choose(self, text='今天下午提醒我办理事项', calendar=None, now=NOW,
               created=NOW, snapshot_at=NOW, zone=ZONE):
        return choose_reminder_slot(text, created, zone,
                                    context() if calendar is None else calendar,
                                    now, snapshot_at)

    def test_real_afternoon_example_selects_1430_with_buffers(self):
        calendar = context([
            event('2026-10-27T12:30:00+08:00', '2026-10-27T14:20:00+08:00', title='private appointment'),
            event('2026-10-27T16:00:00+08:00', '2026-10-27T17:50:00+08:00', title='private meeting')])
        before = copy.deepcopy(calendar)
        result = self.choose(calendar=calendar)
        self.assertEqual(result['due'], '2026-10-27T14:30:00+08:00')
        self.assertEqual(result['end'], '2026-10-27T14:45:00+08:00')
        self.assertEqual(result['duration_minutes'], 15)
        self.assertEqual(result['buffer_minutes'], 10)
        self.assertNotIn('private', str(result))
        self.assertEqual(calendar, before)

    def test_complete_empty_snapshot_proves_free_and_rounds_up(self):
        result = self.choose()
        self.assertEqual(result['due'], '2026-10-27T13:35:00+08:00')
        self.assertEqual(result['end'], '2026-10-27T13:50:00+08:00')
        self.assertEqual(self.choose('下午提醒我办理事项')['due'], result['due'])
        early = stamp('2026-10-27T10:00:00+08:00')
        self.assertEqual(self.choose(now=early, created=early, snapshot_at=early)['due'],
                         '2026-10-27T13:00:00+08:00')

    def test_overlaps_unsorted_events_and_buffers_are_merged(self):
        result = self.choose(calendar=context([
            event('2026-10-27T14:05:00+08:00', '2026-10-27T14:51:00+08:00'),
            event('2026-10-27T12:50:00+08:00', '2026-10-27T14:10:00+08:00'),
            event('2026-10-27T13:00:00+08:00', '2026-10-27T13:40:00+08:00')]))
        self.assertEqual(result['due'], '2026-10-27T15:05:00+08:00')

    def test_exact_gap_and_event_before_window_respect_both_buffers(self):
        result = self.choose(calendar=context([
            event('2026-10-27T12:00:00+08:00', '2026-10-27T13:30:00+08:00'),
            event('2026-10-27T14:05:00+08:00', '2026-10-27T17:00:00+08:00')]))
        self.assertEqual(result['due'], '2026-10-27T13:40:00+08:00')
        self.assertEqual(result['end'], '2026-10-27T13:55:00+08:00')

    def test_tomorrow_and_explicit_dates_require_covered_day(self):
        calendar = context(range_end='2026-10-29T00:00:00+08:00')
        for text in ('明天下午提醒我办理事项', '2026-10-28下午提醒我办理事项',
                     '十月二十八号下午提醒我办理事项', 'Tomorrow afternoon remind me to review.'):
            with self.subTest(text=text):
                self.assertEqual(self.choose(text, calendar)['due'], '2026-10-28T13:00:00+08:00')
        with self.assertRaises(ValueError): self.choose('明天下午提醒我办理事项')

    def test_explicit_day_crosses_year_without_rolling_past_dates_forward(self):
        now = stamp('2026-12-31T13:24:00+08:00')
        calendar = context(date='2026-12-31', range_start='2026-12-31T00:00:00+08:00',
                           range_end='2027-01-02T00:00:00+08:00')
        result = self.choose('明天下午提醒我办理事项', calendar, now, now, now)
        self.assertEqual(result['due'], '2027-01-01T13:00:00+08:00')
        with self.assertRaises(ValueError):
            self.choose('十月二十八号下午提醒我办理事项', calendar, now, now, now)

    def test_all_day_and_cross_midnight_busy_events_block_the_target(self):
        for busy in (
            event('2026-10-27T00:00:00+08:00', '2026-10-28T00:00:00+08:00', True),
            event('2026-10-26T00:00:00+08:00', '2026-10-28T00:00:00+08:00', True),
            event('2026-10-26T23:00:00+08:00', '2026-10-27T18:00:00+08:00')):
            with self.subTest(busy=busy):
                with self.assertRaises(ValueError): self.choose(calendar=context([busy]))
        calendar = context([event('2026-10-26T00:00:00+08:00', '2026-10-27T00:00:00+08:00', True)])
        self.assertEqual(self.choose(calendar=calendar)['due'], '2026-10-27T13:35:00+08:00')

    def test_no_gap_and_elapsed_afternoon_never_roll_to_tomorrow(self):
        calendar = context([event('2026-10-27T13:00:00+08:00', '2026-10-27T18:00:00+08:00')],
                           range_end='2026-10-29T00:00:00+08:00')
        with self.assertRaisesRegex(ValueError, '不会自动改到次日'): self.choose(calendar=calendar)
        late = stamp('2026-10-27T17:41:00+08:00')
        with self.assertRaisesRegex(ValueError, '不会自动改到次日'):
            self.choose(now=late, created=late, snapshot_at=late)

    def test_freshness_is_separate_required_snapshot_time(self):
        self.assertEqual(self.choose(now=NOW+300)['due'], '2026-10-27T13:40:00+08:00')
        for snapshot, now in ((NOW-301, NOW), (NOW+1, NOW), (NOW, NOW+301)):
            with self.subTest(snapshot=snapshot, now=now):
                with self.assertRaises(ValueError): self.choose(now=now, snapshot_at=snapshot)
        with self.assertRaises(ValueError): self.choose(created=NOW+1)
        for bad in (True, 'now', float('nan'), float('inf')):
            with self.subTest(bad=bad):
                with self.assertRaises(ValueError): self.choose(snapshot_at=bad)

    def test_missing_truncated_stale_or_mismatched_context_is_rejected(self):
        for change in ({'truncated': True}, {'truncated': 0}, {'truncated': None},
                       {'date': '2026-10-26'}, {'date': '2026-10-28'},
                       {'timeZone': 'UTC'}, {'events': None}, {'events': {}},
                       {'range_start': '2026-10-27T13:00:00+08:00'},
                       {'range_end': '2026-10-27T18:00:00+08:00'}):
            with self.subTest(change=change):
                with self.assertRaises(ValueError): self.choose(calendar=context(**change))
        for field in ('date', 'timeZone', 'truncated', 'range_start', 'range_end', 'events'):
            calendar = context(); del calendar[field]
            with self.subTest(missing=field):
                with self.assertRaises(ValueError): self.choose(calendar=calendar)
        with self.assertRaises(ValueError): self.choose(calendar={})

    def test_utc_event_instants_use_declared_local_calendar_frame(self):
        calendar = context([event('2026-10-27T04:30:00Z', '2026-10-27T06:20:00Z')],
                           range_start='2026-10-26T16:00:00Z', range_end='2026-10-27T16:00:00Z')
        self.assertEqual(self.choose(calendar=calendar)['due'], '2026-10-27T14:30:00+08:00')

    def test_timezone_anchor_uses_created_local_date(self):
        zone = 'America/Los_Angeles'
        now = stamp('2026-10-27T13:24:00-07:00')
        calendar = context(timeZone=zone, range_start='2026-10-27T00:00:00-07:00',
                           range_end='2026-10-28T00:00:00-07:00')
        result = self.choose(calendar=calendar, now=now, created=now, snapshot_at=now, zone=zone)
        self.assertEqual(result['due'], '2026-10-27T13:35:00-07:00')
        self.assertEqual(dt.datetime.fromtimestamp(now, ZoneInfo(ZONE)).date().isoformat(), '2026-10-28')

    def test_explicit_clocks_multiple_dayparts_and_unsupported_dates_never_gain_slots(self):
        for text in ('今天下午三点提醒我办理事项', '今天下午15:00提醒我办理事项',
                     '今天下午3提醒我办理事项',
                     'Tomorrow afternoon at three remind me to review.',
                     'Tomorrow afternoon from three to four p.m., remind me to review.',
                     'Tomorrow afternoon nine thirty remind me to review.',
                     '今天上午和下午提醒我办理事项', '今天下午明天下午提醒我办理事项',
                     '今天上午提醒我办理事项', '下周末下午提醒我办理事项',
                     '星期三下午提醒我办理事项', '二十八号下午提醒我办理事项',
                     '明年十月二十八号下午提醒我办理事项', '后天下午提醒我办理事项',
                     '今天下午三点左右提醒我办理事项', '今天下午提前十分钟提醒我办理事项'):
            with self.subTest(text=text):
                with self.assertRaises(ValueError): self.choose(text)

    def test_malformed_events_raise_without_reflecting_private_titles(self):
        for busy in ({'start': 'bad', 'end': 'bad', 'isAllDay': False, 'title': 'private diagnostic'},
                     event('2026-10-27T14:00:00+08:00', '2026-10-27T13:00:00+08:00'),
                     event('2026-10-27T13:00:00', '2026-10-27T14:00:00+08:00'),
                     event('2026-10-27T13:00:00+08:00', '2026-10-27T14:00:00+08:00', 0),
                     None):
            with self.subTest(busy=busy):
                with self.assertRaises(ValueError) as error: self.choose(calendar=context([busy]))
                self.assertNotIn('private', str(error.exception))

    def test_negation_cancellation_and_relative_durations_never_fall_back_to_gap(self):
        for text in ('不要下午提醒我拿快递', '别下午提醒我拿快递', '请别下午提醒我拿快递',
                     '不用下午提醒我拿快递', '不需要下午提醒我拿快递', '取消今天下午的提醒',
                     "Don't remind me this afternoon.", 'Do not remind me this afternoon.',
                     'Never remind me this afternoon.', '今天下午半小时后提醒我拿快递',
                     '今天下午一个小时后提醒我拿快递', '今天下午三十分钟后提醒我拿快递',
                     '今天下午一刻钟后提醒我拿快递', '今天下午过一会儿提醒我拿快递',
                     '三天后下午提醒我拿快递', '下午两个月后提醒我拿快递',
                     'This afternoon in twenty minutes remind me to review.',
                     'This afternoon in 15 mins remind me to review.',
                     'This afternoon in 2h remind me to review.',
                     'This afternoon in an hour remind me to review.'):
            with self.subTest(text=text):
                with self.assertRaises(ValueError): self.choose(text)


if __name__ == '__main__':
    unittest.main()
