"""Pure reminder time parsing; no notifications, database or calendar writes."""
import datetime as dt
import pathlib
import sys
import unittest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / 'server'))
from calendar_journal import validate_zone
from reminder_time import parse_reminder_time, ReminderNeedsDetails, ReminderTimeError


def stamp(value, zone='Asia/Hong_Kong'):
    return dt.datetime.fromisoformat(value).replace(tzinfo=validate_zone(zone)).timestamp()


NOW = stamp('2026-10-27T12:00:00')


class ReminderTimeTests(unittest.TestCase):
    def test_negated_reminder_is_not_scheduled_but_do_not_forget_is_content(self):
        for text in ('不要明天下午三点提醒我拿快递','今天下午三点别提醒我',
                     'Do not remind me tomorrow at 3 pm','Cancel the reminder tomorrow at 3 pm'):
            with self.subTest(text=text),self.assertRaises(ReminderTimeError) as error:
                parse_reminder_time(text,NOW)
            self.assertEqual(error.exception.code,'negated_request')
        self.assert_due('提醒我明天下午三点不要忘记拿快递','2026-10-28T15:00:00')

    def assert_due(self, text, expected, created=NOW, zone='Asia/Hong_Kong'):
        self.assertEqual(parse_reminder_time(text, created, zone), stamp(expected, zone))

    def test_exact_chinese_dates_dayparts_and_24_hour_times(self):
        for text, expected in (
            ('提醒我今天下午三点拿快递', '2026-10-27T15:00:00'),
            ('下午三点提醒我拿快递', '2026-10-27T15:00:00'),
            ('提醒我十五点拿快递', '2026-10-27T15:00:00'),
            ('提醒我15:30拿快递', '2026-10-27T15:30:00'),
            ('明天上午九点半提醒我拿快递', '2026-10-28T09:30:00'),
            ('明天下午三点一刻提醒我拿快递', '2026-10-28T15:15:00'),
            ('提醒我十月二十八号下午三点零五分拿快递', '2026-10-28T15:05:00'),
            ('提醒我下周一下午三点拿快递', '2026-11-02T15:00:00'),
            ('2026-12-01 15:30提醒我拿快递', '2026-12-01T15:30:00'),
            ('明天下午三点提醒我看《三点半》', '2026-10-28T15:00:00')):
            with self.subTest(text=text): self.assert_due(text, expected)

    def test_missing_daypart_only_and_ambiguous_hour_never_default(self):
        for text in ('提醒我下午要拿快递', '提醒我今天下午拿快递', '明天提醒我继续项目',
                     '提醒我拿快递', '三点提醒我拿快递', '明天三点提醒我拿快递',
                     '明天中午提醒我拿快递', 'Remind me tomorrow', 'Remind me tomorrow at 3'):
            with self.subTest(text=text), self.assertRaises(ReminderNeedsDetails): parse_reminder_time(text, NOW)
        with self.assertRaises(ReminderNeedsDetails) as error: parse_reminder_time('提醒我下午要拿快递', NOW)
        self.assertEqual(error.exception.code, 'missing_time')

    def test_relative_minutes_hours_use_creation_time(self):
        for text, seconds in (('十五分钟后提醒我拿快递', 900), ('提醒我15分钟后拿快递', 900),
                              ('过15分钟提醒我拿快递', 900), ('再过十五分钟提醒我拿快递', 900),
                              ('两个小时后提醒我拿快递', 7200), ('一小时之后提醒我喝水', 3600),
                              ('提醒我2小时以后喝水', 7200), ('Remind me in 15 minutes to check the parcel', 900),
                              ('Remind me after 2 hours to check the parcel', 7200)):
            with self.subTest(text=text): self.assertEqual(parse_reminder_time(text, NOW), NOW + seconds)

    def test_relative_duration_is_elapsed_across_dst(self):
        created = stamp('2026-03-08T01:30:00', 'America/New_York')
        self.assert_due('Remind me in 2 hours', '2026-03-08T04:30:00', created, 'America/New_York')

    def test_exact_english_am_pm_and_colon(self):
        for text, expected in (
            ('Remind me tomorrow at 3 pm to pick up the parcel', '2026-10-28T15:00:00'),
            ('Remind me at 3:15 p.m. to check the parcel', '2026-10-27T15:15:00'),
            ('Remind me tomorrow at 9 a.m. to check the parcel', '2026-10-28T09:00:00'),
            ('Remind me tomorrow at 12 AM', '2026-10-28T00:00:00'),
            ('Remind me tomorrow at 12 PM', '2026-10-28T12:00:00'),
            ('Remind me tomorrow at 15:30', '2026-10-28T15:30:00')):
            with self.subTest(text=text): self.assert_due(text, expected)

    def test_date_without_year_does_not_roll_to_next_year(self):
        with self.assertRaises(ReminderTimeError) as error:
            parse_reminder_time('十月二十六号下午三点提醒我取快递', NOW)
        self.assertEqual(error.exception.code, 'past_time')

    def test_past_or_current_clock_does_not_roll_to_tomorrow(self):
        for text in ('今天上午九点提醒我取快递', '昨天十五点提醒我取快递',
                     '提醒我12:00取快递', 'Remind me at 9 AM'):
            with self.subTest(text=text), self.assertRaises(ReminderTimeError) as error:
                parse_reminder_time(text, NOW)
            self.assertEqual(error.exception.code, 'past_time')

    def test_conflicting_dates_clocks_and_relative_absolute_mix_are_refused(self):
        for text in ('明天十月二十九号下午三点提醒我拿快递',
                     '明天下午三点AM提醒我拿快递',
                     '明天下午三点到四点提醒我拿快递',
                     '提醒我15分钟后明天下午三点取快递',
                     '提醒我15分钟后或2小时后取快递',
                     'Remind me tomorrow from 3 pm to 4 pm'):
            with self.subTest(text=text), self.assertRaises(ReminderTimeError): parse_reminder_time(text, NOW)

    def test_invalid_dates_numbers_units_and_malformed_minutes_are_not_shortened(self):
        for text in ('十三月二十八号下午三点提醒我拿快递',
                     '二〇二六年二月二十九号下午三点提醒我拿快递',
                     '明天下午一百三点提醒我拿快递',
                     '明天下午三点六十分提醒我拿快递',
                     '明天下午三点一课提醒我拿快递',
                     '明天下午三点十分三十秒提醒我拿快递',
                     '零分钟后提醒我拿快递', '一百三分钟后提醒我拿快递',
                     'Remind me in 99999999 hours', 'Remind me at 13 pm',
                     '明天下下周三下午三点提醒我拿快递'):
            with self.subTest(text=text), self.assertRaises(ReminderTimeError): parse_reminder_time(text, NOW)

    def test_vague_or_compound_duration_is_not_partially_scheduled(self):
        for text in ('大概15分钟后提醒我拿快递', '提醒我下午三点左右拿快递',
                     '一小时三十分钟后提醒我拿快递', 'Remind me in 1 hour 30 minutes',
                     'Remind me around 3 pm tomorrow'):
            with self.subTest(text=text), self.assertRaises(ReminderNeedsDetails): parse_reminder_time(text, NOW)

    def test_unsupported_date_or_duration_never_falls_back_to_today(self):
        for text in ('下周下午三点提醒我拿快递', '下个月15:00提醒我拿快递',
                     '明晚20:00提醒我拿快递', 'Remind me Friday at 3 PM',
                     'Remind me next month at 3 PM', 'Remind me in -15 minutes at 3 PM',
                     '一个半小时后下午三点提醒我拿快递'):
            with self.subTest(text=text), self.assertRaises(ReminderNeedsDetails): parse_reminder_time(text, NOW)

    def test_adjacent_conflicting_dayparts_are_not_discarded(self):
        for text in ('明天上午下午三点提醒我拿快递',
                     'Remind me tomorrow at 3 PM in the morning',
                     'Remind me tomorrow in the afternoon at 3 AM'):
            with self.subTest(text=text), self.assertRaises(ReminderTimeError): parse_reminder_time(text, NOW)

    def test_missing_details_can_be_constructed_with_a_message(self):
        error = ReminderNeedsDetails('No suitable gap available')
        self.assertEqual(error.code, 'needs_details')
        self.assertEqual(str(error), 'No suitable gap available')

    def test_phone_timezone_and_midnight_are_respected(self):
        created = stamp('2026-10-27T23:55:00')
        self.assert_due('明天零点提醒我拿快递', '2026-10-28T00:00:00', created)
        self.assertEqual(parse_reminder_time('十分钟后提醒我喝水', created), created + 600)
        self.assert_due('明天下午三点提醒我拿快递', '2026-10-28T15:00:00', NOW, 'Asia/Tokyo')

    def test_dst_gap_and_fold_require_explicit_resolution(self):
        for text, created in (
            ('Remind me 2026-03-08 at 02:30', stamp('2026-03-07T12:00:00', 'America/New_York')),
            ('Remind me 2026-11-01 at 01:30', stamp('2026-10-31T12:00:00', 'America/New_York'))):
            with self.subTest(text=text), self.assertRaises(ReminderNeedsDetails):
                parse_reminder_time(text, created, 'America/New_York')

    def test_invalid_reference_and_time_zone_have_fixed_errors(self):
        for created in (None, True, '12', float('nan'), float('inf')):
            with self.subTest(created=created), self.assertRaises(ReminderTimeError):
                parse_reminder_time('明天下午三点提醒我拿快递', created)
        with self.assertRaises(ReminderTimeError): parse_reminder_time('明天下午三点提醒我拿快递', NOW, '../secret')


if __name__ == '__main__': unittest.main(verbosity=2)
