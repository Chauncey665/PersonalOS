"""Bare Chinese weekdays use the request's local day, never model-made dates."""
import datetime as dt
import pathlib
import sys
import unittest

sys.path.insert(0,str(pathlib.Path(__file__).resolve().parents[1]/'server'))
from calendar_journal import CalendarError, DATE_RE, _date, parse_entry, validate_zone
from deadline_intent import deadline_fact
from reminder_time import ReminderNeedsDetails, ReminderTimeError, parse_reminder_time


def stamp(value,zone='Asia/Hong_Kong'):
    return dt.datetime.fromisoformat(value).replace(tzinfo=validate_zone(zone)).timestamp()


class RelativeWeekdayTests(unittest.TestCase):
    def test_bare_friday_is_upcoming_including_today(self):
        for token in ('周五','星期五','礼拜五','周5','星期5','礼拜5'):
            for today,expected in (('2026-10-28','2026-10-30'),('2026-10-30','2026-10-30'),('2026-10-31','2026-11-06')):
                with self.subTest(token=token,today=today):
                    self.assertEqual(_date(token,dt.date.fromisoformat(today)).isoformat(),expected)
                    self.assertEqual(deadline_fact(token+'前提交报告',stamp(today+'T12:00:00'),'Asia/Hong_Kong'),expected)

    def test_dates_are_shared_by_calendar_reminder_and_deadline(self):
        for token in ('周五','星期五','礼拜五'):
            for created,day in ((stamp('2026-10-28T12:00:00'),'2026-10-30'),(stamp('2026-10-31T12:00:00'),'2026-11-06')):
                with self.subTest(token=token,day=day):
                    entry=parse_entry('安排'+token+'15:00到16:00项目复盘',created)
                    self.assertEqual(entry['status'],'ready',entry)
                    self.assertEqual(entry['start'],day+'T15:00:00+08:00')
                    self.assertEqual(entry['title'],'项目复盘')
                    self.assertEqual(parse_reminder_time(token+'下午三点提醒我提交报告',created),stamp(day+'T15:00:00'))
                    self.assertEqual(deadline_fact(token+'15:00前提交报告',created,'Asia/Hong_Kong'),day+'T15:00:00+08:00')

    def test_same_day_past_time_never_rolls_to_next_week(self):
        created=stamp('2026-10-30T16:00:00')
        for token in ('周五','星期五','礼拜五'):
            with self.subTest(token=token):
                with self.assertRaises(ReminderTimeError) as error:
                    parse_reminder_time(token+'15:00提醒我提交报告',created)
                self.assertEqual(error.exception.code,'past_time')
                with self.assertRaises(ReminderTimeError):
                    deadline_fact(token+'15:00前提交报告',created,'Asia/Hong_Kong')
                entry=parse_entry('安排'+token+'15:00到16:00项目复盘',created)
                self.assertEqual(entry['status'],'needs_details')
                self.assertIsNone(entry['end'])

    def test_explicit_this_and_next_week_stay_monday_based(self):
        today=dt.date(2026,10,31)
        for token,expected in (('本周五','2026-10-30'),('这星期五','2026-10-30'),
                               ('下周五','2026-11-06'),('下个星期一','2026-11-02')):
            self.assertEqual(_date(token,today).isoformat(),expected)
        entry=parse_entry('记下下周一下午三点到四点项目复盘',stamp('2026-10-27T12:00:00'))
        self.assertEqual(entry['status'],'ready',entry)
        self.assertEqual(entry['start'],'2026-11-02T15:00:00+08:00')

    def test_unsupported_longer_phrases_do_not_become_bare_weekdays(self):
        created=stamp('2026-10-28T12:00:00')
        for token in ('下下周五','下下个星期五','上周五','上上礼拜五','每周五',
                      '隔周五','下个星期一二','周八','礼拜五十','一周五'):
            with self.subTest(token=token):
                matches=list(DATE_RE.finditer(token))
                self.assertEqual([x.group() for x in matches],[token])
                with self.assertRaises(CalendarError):
                    _date(matches[0].group(),dt.date(2026,10,28))
                with self.assertRaises(ReminderNeedsDetails):
                    parse_reminder_time(token+'下午三点提醒我提交报告',created)
                with self.assertRaises(CalendarError):
                    deadline_fact(token+'前提交报告',created,'Asia/Hong_Kong')
                self.assertEqual(parse_entry('安排'+token+'15:00到16:00项目复盘',created)['status'],'needs_details')

    def test_completed_bare_weekday_asks_which_week(self):
        for created in (stamp('2026-10-28T12:00:00'),stamp('2026-10-30T17:00:00'),stamp('2026-10-31T12:00:00')):
            for token in ('周五','星期五','礼拜五'):
                entry=parse_entry('我'+token+'15:00到16:00完成了项目复盘',created)
                self.assertEqual(entry['status'],'needs_details',entry)
                self.assertIsNone(entry['start'])
                self.assertIsNone(entry['end'])
        entry=parse_entry('我本周五15:00到16:00完成了项目复盘',stamp('2026-10-31T12:00:00'))
        self.assertEqual(entry['status'],'ready',entry)

    def test_local_timezone_and_year_rollover(self):
        # The same instant is Friday in HK and Thursday in Los Angeles.
        created=stamp('2026-10-30T00:30:00')
        self.assertEqual(deadline_fact('周四前提交报告',created,'Asia/Hong_Kong'),'2026-11-05')
        self.assertEqual(deadline_fact('周四前提交报告',created,'America/Los_Angeles'),'2026-10-29')
        self.assertEqual(deadline_fact('礼拜五前提交报告',stamp('2026-12-31T12:00:00'),'Asia/Hong_Kong'),'2027-01-01')


if __name__=='__main__':unittest.main(verbosity=2)
