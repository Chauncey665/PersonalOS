"""Spoken Chinese date/clock rules; no device calendar or ASR model is used."""
import datetime as dt
import pathlib
import sys
import unittest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / 'server'))
from calendar_journal import DEFAULT_ZONE, journal_intent, parse_entry, validate_zone


def anchor(value):
    return dt.datetime.fromisoformat(value).replace(tzinfo=validate_zone(DEFAULT_ZONE)).timestamp()


NOW = anchor('2026-10-27T12:00:00')


class SpokenCalendarDateTests(unittest.TestCase):
    def assert_ready(self, text, start, end, created=NOW, title='项目复盘'):
        entry = parse_entry(text, created)
        self.assertEqual(entry['status'], 'ready', entry)
        self.assertEqual((entry['start'], entry['end']), (start, end))
        self.assertEqual(entry['title'], title)
        self.assertEqual(entry['original_text'], text)
        return entry

    def assert_needs_details(self, text, created=NOW):
        entry = parse_entry(text, created)
        self.assertEqual(entry['status'], 'needs_details', (text, entry))
        self.assertTrue(entry['question'])

    def test_spoken_month_day_year_and_mixed_written_digits(self):
        for date in ('十月二十八号', '十月二十八日', '十月二十八',
                     '二〇二六年十月二十八日', '二零二六年十月二十八号',
                     '2026年十月28号', '二〇二六年10月二十八日', '１０月２８号'):
            with self.subTest(date=date):
                self.assert_ready(date + '下午三点到四点安排项目复盘',
                                  '2026-10-28T15:00:00+08:00', '2026-10-28T16:00:00+08:00')

    def test_spoken_date_can_route_without_an_extra_schedule_verb(self):
        text = '十月二十八号下午三点到四点项目复盘'
        self.assertEqual(journal_intent(text), 'plan')
        self.assert_ready(text, '2026-10-28T15:00:00+08:00', '2026-10-28T16:00:00+08:00')

    def test_zero_padded_spoken_minutes_and_oclock_are_complete_spans(self):
        for start, end in (('三点零五分', '四点钟'), ('三点〇五', '四时'),
                           ('三点05分', '四点钟零五分'), ('三点钟', '四点钟')):
            with self.subTest(start=start, end=end):
                self.assert_ready('明天下午' + start + '到' + end + '，安排项目复盘',
                                  '2026-10-28T15:' + ('00' if start == '三点钟' else '05') + ':00+08:00',
                                  '2026-10-28T16:' + ('05' if end == '四点钟零五分' else '00') + ':00+08:00')

    def test_explicit_current_week_is_monday_based_and_never_rolls_forward(self):
        for date, day in (('本周三', 28), ('这周五', 30), ('本星期六', 31),
                          ('这个星期三', 28), ('这星期四', 29)):
            with self.subTest(date=date):
                self.assert_ready(date + '下午三点到四点安排项目复盘',
                                  f'2026-10-{day:02d}T15:00:00+08:00', f'2026-10-{day:02d}T16:00:00+08:00')
        for date in ('本周一', '这周二'):
            with self.subTest(past_date=date):
                self.assert_needs_details(date + '上午九点到十点安排项目复盘')
        self.assert_ready('我本周一上午九点到十点完成了项目复盘',
                          '2026-10-26T09:00:00+08:00', '2026-10-26T10:00:00+08:00')

    def test_explicit_next_week_crosses_month_and_year_boundaries(self):
        for date in ('下周一', '下星期一', '下个星期一', '下个周1'):
            with self.subTest(date=date):
                self.assert_ready(date + '下午三点到四点安排项目复盘',
                                  '2026-11-02T15:00:00+08:00', '2026-11-02T16:00:00+08:00')
        self.assert_ready('下个星期一下午三点到四点安排项目复盘',
                          '2027-01-04T15:00:00+08:00', '2027-01-04T16:00:00+08:00',
                          created=anchor('2026-12-31T12:00:00'))
        for date in ('本周日', '这周天', '本周7'):
            with self.subTest(sunday=date):
                self.assert_ready(date + '下午三点到四点安排项目复盘',
                                  '2026-11-01T15:00:00+08:00', '2026-11-01T16:00:00+08:00')

    def test_explicit_year_all_day_and_leap_date(self):
        entry = self.assert_ready('二〇二七年一月一日全天安排项目复盘',
                                 '2027-01-01T00:00:00+08:00', '2027-01-02T00:00:00+08:00')
        self.assertTrue(entry['all_day'])
        self.assert_ready('二〇二八年二月二十九号下午三点到四点安排项目复盘',
                          '2028-02-29T15:00:00+08:00', '2028-02-29T16:00:00+08:00')

    def test_invalid_dates_and_years_do_not_reinterpret_a_valid_suffix(self):
        for date in ('十三月二十八号', '十月三十二日', '十一月三十一日',
                     '二〇二六年二月二十九日', '零月二十八日', '十月零日',
                     '二十十月二十八日', '一百三月二十八日', '十月二十八十八日',
                     '二〇二六六年十月二十八日', '二千零二十六年十月二十八日',
                     '二六年十月二十八号'):
            with self.subTest(date=date):
                self.assert_needs_details(date + '下午三点到四点安排项目复盘')

    def test_invalid_spoken_clock_numbers_never_raise_or_shorten(self):
        for clocks in ('二十十点到二十三点', '一百三点到四点', '两十点到二十三点',
                       '三点六十分到四点', '三点零十五分到四点', '三点二三分到四点',
                       '三点一百五分到四点', '零二十三点到四点', '三十点钟到四十点钟'):
            with self.subTest(clocks=clocks):
                self.assert_needs_details('明天下午' + clocks + '安排项目复盘')

    def test_date_or_daypart_conflicts_and_midnight_still_need_details(self):
        for text in ('安排明天十月二十九号下午三点到四点项目复盘',
                     '本周三下个星期一下午三点到四点安排项目复盘',
                     '安排明天下午三点零五分am到四点项目复盘',
                     '安排明天上午三点钟pm到四点钟项目复盘',
                     '安排明天晚上十一点钟到一点钟项目复盘',
                     '安排明天二十三点零五分到零点三十分项目复盘'):
            with self.subTest(text=text): self.assert_needs_details(text)

    def test_missing_time_vague_week_and_missing_year_never_gain_defaults(self):
        for text in ('安排十月二十八号项目复盘', '安排十月二十八号下午三点钟项目复盘',
                     '安排本周三下午三点零五分项目复盘',
                     '安排这周末下午三点到四点项目复盘', '安排下个星期一三点左右到四点项目复盘'):
            with self.subTest(text=text): self.assert_needs_details(text)
        self.assert_needs_details('十月二十八号下午三点到四点安排项目复盘',
                                  created=anchor('2026-12-01T12:00:00'))

    def test_unsupported_date_qualifiers_cannot_be_discarded(self):
        for text in ('安排明年十月二十八日下午三点到四点项目复盘',
                     '我去年的十月二十八号下午三点到四点完成了项目复盘',
                     '下下个星期一下午三点到四点安排项目复盘',
                     '安排下下周三下午三点到四点项目复盘',
                     '下个星期一二下午三点到四点安排项目复盘'):
            with self.subTest(text=text): self.assert_needs_details(text)
        self.assert_ready('记下下周一下午三点到四点项目复盘',
                          '2026-11-02T15:00:00+08:00', '2026-11-02T16:00:00+08:00')

    def test_today_yesterday_and_future_completed_are_not_corrected_by_guess(self):
        self.assert_ready('我昨天上午三点零五分到四点钟完成了项目复盘',
                          '2026-10-26T03:05:00+08:00', '2026-10-26T04:00:00+08:00')
        self.assert_ready('我今天上午三点零五分到四点钟完成了项目复盘',
                          '2026-10-27T03:05:00+08:00', '2026-10-27T04:00:00+08:00')
        self.assert_needs_details('我本周三下午三点到四点完成了项目复盘')

    def test_relative_day_offsets_and_complete_title_spans(self):
        for date, day in (('明天', 28), ('后天', 29), ('大后天', 30)):
            with self.subTest(date=date):
                entry = self.assert_ready(date + '上午九点到十点安排项目复盘',
                                          f'2026-10-{day}T09:00:00+08:00',
                                          f'2026-10-{day}T10:00:00+08:00')
                self.assertEqual(entry['kind'], 'plan')
        for date, day in (('昨天', 26), ('前天', 25), ('大前天', 24)):
            for subject in ('', '我'):
                with self.subTest(date=date, subject=subject):
                    entry = self.assert_ready(subject + date + '上午九点到十点完成了项目复盘',
                                              f'2026-10-{day}T09:00:00+08:00',
                                              f'2026-10-{day}T10:00:00+08:00')
                    self.assertEqual(entry['kind'], 'done')

    def test_relative_days_cross_month_year_and_leap_boundaries(self):
        for date, created, expected, action in (
            ('大后天', '2026-10-30', '2026-11-02', '安排'),
            ('大后天', '2026-12-30', '2027-01-02', '安排'),
            ('大后天', '2028-02-27', '2028-03-01', '安排'),
            ('前天', '2026-11-01', '2026-10-30', '完成了'),
            ('大前天', '2026-11-01', '2026-10-29', '完成了'),
            ('前天', '2027-01-02', '2026-12-31', '完成了'),
            ('大前天', '2027-01-02', '2026-12-30', '完成了'),
            ('大前天', '2028-03-02', '2028-02-28', '完成了')):
            with self.subTest(date=date, created=created):
                self.assert_ready(date + '上午九点到十点' + action + '项目复盘',
                                  expected + 'T09:00:00+08:00', expected + 'T10:00:00+08:00',
                                  created=anchor(created + 'T12:00:00'))

    def test_malformed_relative_day_modifiers_never_shorten_to_known_dates(self):
        for date in ('大大后天', '大大前天', '后后天', '前前天', '前后天',
                     '大昨天', '大明天', '明明天', '昨昨天', '今明天',
                     '大 大后天', '大 后天', '大 前天', '大大大后天'):
            with self.subTest(date=date):
                self.assert_needs_details(date + '上午九点到十点安排项目复盘')
                # A second, valid date must not hide the malformed first one.
                self.assert_needs_details(date + '十月三十号上午九点到十点安排项目复盘')

    def test_relative_day_conflicts_missing_times_and_past_plan_stay_unresolved(self):
        for text in ('大后天十月二十九号上午九点到十点安排项目复盘',
                     '我大前天十月二十五号上午九点到十点完成了项目复盘',
                     '大后天上午九点安排项目复盘', '大后天安排项目复盘',
                     '前天上午九点到十点安排项目复盘',
                     '大前天上午九点到十点安排项目复盘',
                     '我大后天上午九点到十点完成了项目复盘'):
            with self.subTest(text=text): self.assert_needs_details(text)

    def test_relative_day_boundary_changes_preserve_next_week_command_and_future_intent(self):
        self.assert_ready('记下下周一下午三点到四点项目复盘',
                          '2026-11-02T15:00:00+08:00', '2026-11-02T16:00:00+08:00')
        self.assertEqual(journal_intent('明天上午九点到十点完成项目复盘'), 'plan')
        self.assert_ready('大后天上午九点到十点项目复盘',
                          '2026-10-30T09:00:00+08:00', '2026-10-30T10:00:00+08:00')

    def test_english_asr_decimal_ranges_and_dotted_ampm_keep_clean_titles(self):
        for text, start, end, title in (
            ('Tomorrow from 9.30 to 10.30 in the morning, schedule a project review.',
             '09:30', '10:30', 'a project review'),
            ('Tomorrow from 3 to 4 p.m., schedule a calendar test.',
             '15:00', '16:00', 'a calendar test'),
            ('Tomorrow from 9.30 until 10.45 a.m., schedule review.',
             '09:30', '10:45', 'review'),
            ('Schedule review tomorrow from 3.05-4.15 P.M.',
             '15:05', '16:15', 'review'),
            ('Tomorrow from 9 a.m. to 10 a.m., schedule review.',
             '09:00', '10:00', 'review'),
            ('Tomorrow at 9 a.m. until 10 a.m., schedule review.',
             '09:00', '10:00', 'review'),
            ('Schedule review tomorrow from 9 to 10.',
             '09:00', '10:00', 'review'),
            ('Schedule review tomorrow from 9.30 to 10.30.',
             '09:30', '10:30', 'review')):
            with self.subTest(text=text):
                self.assert_ready(text, f'2026-10-28T{start}:00+08:00',
                                  f'2026-10-28T{end}:00+08:00', title=title)

    def test_decimal_clock_spelling_needs_explicit_from_range_not_versions(self):
        for text in ('Schedule the version 1.2-3.4 review tomorrow.',
                     'Schedule the version 9.30 to 10.30 upgrade tomorrow.',
                     'Schedule the version from 9.30 to 10.30 upgrade tomorrow.',
                     'Schedule review tomorrow at 9.30.',
                     'Schedule review tomorrow 9.30 to 10.30.',
                     'Schedule review tomorrow from 9.3 to 10.3.',
                     'Schedule review tomorrow from 9.300 to 10.300.',
                     'Schedule review tomorrow from 9.60 to 10.30.'):
            with self.subTest(text=text): self.assert_needs_details(text)

    def test_decimal_ampm_conflicts_overnight_and_missing_end_still_rejected(self):
        for text in ('Tomorrow from 9.30 to 10.30 p.m. in the morning, schedule review.',
                     'Tomorrow in the afternoon from 3.30 a.m. to 4.30 a.m., schedule review.',
                     'Schedule review tomorrow from 11.30 p.m. to 1.30 a.m.',
                     'Schedule review tomorrow from 13.30 to 14.30 p.m.',
                     'Schedule review tomorrow from 9.30 a.m.'):
            with self.subTest(text=text): self.assert_needs_details(text)

    def test_actual_qwen_english_word_hours_and_full_minute_ranges(self):
        for text, start, end, title in (
            ('Tomorrow, from three to four p.m., schedule a calendar test.',
             '15:00', '16:00', 'a calendar test'),
            ('Tomorrow from nine thirty to ten thirty in the morning, schedule a project review.',
             '09:30', '10:30', 'a project review'),
            ('Tomorrow at nine fifteen until ten forty-five a.m., schedule review.',
             '09:15', '10:45', 'review'),
            ('Tomorrow from nine oh five to ten zero five a.m., schedule review.',
             '09:05', '10:05', 'review'),
            ('Tomorrow from eleven fifty-nine a.m. to twelve p.m., schedule review.',
             '11:59', '12:00', 'review'),
            ('Tomorrow from twelve a.m. to one a.m., schedule review.',
             '00:00', '01:00', 'review'),
            ('Tomorrow at THREE until FOUR P.M., schedule review.',
             '15:00', '16:00', 'review')):
            with self.subTest(text=text):
                self.assert_ready(text, f'2026-10-28T{start}:00+08:00',
                                  f'2026-10-28T{end}:00+08:00', title=title)

    def test_english_word_clock_parser_preserves_number_words_in_titles(self):
        for text, title in (
            ('Schedule Three Body review tomorrow from three to four p.m.', 'Three Body review'),
            ('Schedule “From Three to Four” tomorrow from nine to ten a.m.', '“From Three to Four”'),
            ('Schedule version from three to four tomorrow from nine to ten a.m.', 'version from three to four')):
            with self.subTest(text=text):
                afternoon = 'Three Body' in text
                self.assert_ready(text, '2026-10-28T' + ('15' if afternoon else '09') + ':00:00+08:00',
                                  '2026-10-28T' + ('16' if afternoon else '10') + ':00:00+08:00', title=title)

    def test_english_word_clock_requires_explicit_complete_nonversion_range(self):
        for text in ('Schedule review tomorrow three to four p.m.',
                     'Schedule review tomorrow nine thirty to ten thirty.',
                     'Schedule review tomorrow from nine thirty.',
                     'Schedule review tomorrow at three p.m.',
                     'Schedule review tomorrow from thirteen to fourteen.',
                     'Schedule version from three to four tomorrow.',
                     'Schedule releases from three to four tomorrow.',
                     'Schedule builds at three to four tomorrow.',
                     'Schedule review tomorrow from nine sixty to ten.',
                     'Schedule review tomorrow from nine to ten sixty.',
                     'Schedule review tomorrow from nine to ten thirty sixty.',
                     'Schedule review tomorrow from three to four point five.',
                     'Schedule review tomorrow from three to four hundred.',
                     'Schedule review tomorrow from three to four four.',
                     'Schedule review tomorrow from three to fourty.',
                     'Schedule review tomorrow from nine five to ten five.',
                     'Schedule review tomorrow from nine thirty to ten thirty.',
                     'Schedule reading chapters from three to four tomorrow.',
                     'Schedule reading from three to four chapters tomorrow.',
                     'Schedule testing from three to four versions tomorrow.',
                     'Schedule review tomorrow from three to four hours.',
                     'Schedule review tomorrow from three to four people.',
                     'Schedule review tomorrow from nine a.m. to ten thirtyfive.',
                     'Schedule review tomorrow from nine a.m. to ten thirty-fiveish.',
                     'Schedule review tomorrow from nine a.m. to ten thirty fiveish.'):
            with self.subTest(text=text): self.assert_needs_details(text)

    def test_english_word_clock_conflicting_or_extra_times_need_details(self):
        for text in ('Tomorrow from three to four p.m. in the morning, schedule review.',
                     'Tomorrow in the afternoon from three a.m. to four a.m., schedule review.',
                     'Schedule review tomorrow from eleven p.m. to one a.m.',
                     'Schedule review tomorrow from nine to ten a.m. at eleven.',
                     'Schedule review tomorrow from nine to ten a.m. from eleven to twelve p.m.'):
            with self.subTest(text=text): self.assert_needs_details(text)

    def test_actual_qwen_malformed_chinese_minute_unit_is_not_shortened(self):
        actual = '十月二十八号下午三点半到四点一课安排机器学习复习。'
        self.assert_needs_details(actual)
        entry = parse_entry(actual, NOW)
        self.assertIsNone(entry['start'])
        self.assertIsNone(entry['end'])
        self.assertEqual(entry['original_text'], actual)
        for clocks in ('三点一课到四点', '三点半到四点一克', '三点半到四点一客',
                       '三点半到四点一 课', '三点半到四点钟一课', '三点半到四点零五课'):
            with self.subTest(clocks=clocks):
                self.assert_needs_details('明天下午' + clocks + '安排项目复盘')

    def test_explicit_minutes_and_bare_minutes_with_clear_boundaries_still_work(self):
        for end in ('九点十分安排项目复盘', '九点十安排项目复盘',
                    '九点十，安排项目复盘', '九点十。安排项目复盘'):
            with self.subTest(end=end):
                self.assert_ready('明天上午九点到' + end,
                                  '2026-10-28T09:00:00+08:00', '2026-10-28T09:10:00+08:00')
        self.assert_ready('明天下午三点零五到四点一刻安排项目复盘',
                          '2026-10-28T15:05:00+08:00', '2026-10-28T16:15:00+08:00')
        self.assert_needs_details('明天下午三点到四点一项目复盘')


if __name__ == '__main__':
    unittest.main()
