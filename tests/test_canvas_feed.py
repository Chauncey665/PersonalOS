"""Official Calendar Feed fallback without API tokens or browser credential extraction."""
import datetime as dt
import hashlib
import io
import json
import pathlib
import stat
import sys
import tempfile
import unittest
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / 'server'))
from personal_os import Store
from canvas_sync import CanvasSync, feed_items, feed_url
from todos import TodoError

URL = 'https://canvas.example.edu/feeds/calendars/user_' + 'syntheticABC123_' * 3 + '.ics'
NOW = dt.datetime(2026, 10, 27, tzinfo=dt.timezone.utc).timestamp()
def calendar(events):
    return ('BEGIN:VCALENDAR\r\nVERSION:2.0\r\n' + '\r\n'.join(events) + '\r\nEND:VCALENDAR\r\n').encode()
def event(ident=20, date='20261030T155959Z', extra='', summary='Assignment 3 [TEST1000]'):
    return ('BEGIN:VEVENT\r\nUID:event-assignment-' + str(ident) + '\r\nDTSTART:' + date +
        '\r\nSUMMARY:' + summary + '\r\nURL:https://canvas.example.edu/courses/10/assignments/' + str(ident) +
        '\r\nDESCRIPTION:Line one\\nLine two\r\n' + extra + 'END:VEVENT')

class FeedParserTests(unittest.TestCase):
    def test_dates_unfolding_and_course_assignment_identity(self):
        rows = feed_items(calendar([event(summary='作业\\, 测试 [TEST1000]').replace('Assignment', 'Assign\r\n ment')]), now=NOW)
        self.assertEqual(rows[0]['external_id'], '10:20')
        self.assertEqual(rows[0]['deadline'], '2026-10-30T15:59:59Z')
        self.assertEqual(rows[0]['title'], '作业, 测试')
        self.assertEqual(rows[0]['course'], 'TEST1000')
        self.assertFalse(rows[0]['completed'])
        self.assertNotIn('feed', rows[0]['url'])
        self.assertEqual(feed_items(calendar([event().replace('DTSTART:', 'DTSTART;VALUE=DATE:').replace('20261030T155959Z','20261030')]),now=NOW)[0]['deadline'],'2026-10-30')
        self.assertEqual(feed_items(calendar([event().replace('DTSTART:', 'DTSTART;TZID=Asia/Hong_Kong:').replace('20261030T155959Z','20261030T235959')]),now=NOW)[0]['deadline'],'2026-10-30T23:59:59+08:00')
    def test_prior_assignments_and_nonassignment_events_are_not_new_todos(self):
        raw=calendar([event(date='20261007T040000Z'),event(21).replace('event-assignment-21','event-calendar-21'),event(22,extra='STATUS:CANCELLED\r\n')])
        self.assertEqual(feed_items(raw,now=NOW),[])
        self.assertEqual(len(feed_items(raw,known=['10:20'],now=NOW)),1)
    def test_canvas_source_calendar_deep_link_utc_and_override_formats(self):
        # Primary source: Canvas CalendarEvent::IcalEvent#to_ics.
        raw = event().replace('DTSTART:20261030T155959Z','DTSTART;TZID=UTC:20261030T155900')
        raw = raw.replace('https://canvas.example.edu/courses/10/assignments/20',
                          'https://canvas.example.edu/calendar?include_contexts=course_10&month=10&year=2026#assignment_20')
        row = feed_items(calendar([raw]),now=NOW)[0]
        self.assertEqual(row['url'],'https://canvas.example.edu/courses/10/assignments/20')
        self.assertEqual(row['deadline'],'2026-10-30T15:59:00+00:00')
        self.assertEqual(feed_items(calendar([raw.replace('event-assignment-20','event-assignment-override-99')]),now=NOW)[0]['external_id'],'10:20')
        with self.assertRaises(TodoError):
            feed_items(calendar([raw.replace('include_contexts=course_10','include_contexts=group_10')]),now=NOW)
    def test_bad_dates_html_truncation_and_ambiguity_never_replace_snapshot(self):
        for raw in [b'<html>login required</html>',calendar([event(date='20261030T155959')]),
                    calendar([event(extra='RRULE:FREQ=DAILY\r\n')]),calendar([event(),event(date='20261101T155959Z')]),
                    calendar([event(extra='DTSTART:20261101T155959Z\r\n')]),calendar([event()])[:-20]]:
            with self.subTest(raw_size=len(raw)),self.assertRaises(TodoError): feed_items(raw,now=NOW)
    def test_only_exact_official_user_feed_host_path_and_https(self):
        self.assertEqual(feed_url(URL),URL)
        self.assertEqual(feed_url(URL.replace('https://','webcal://')),URL)
        for bad in ['http://'+URL[8:],URL+'?redirect=evil',URL+'#x',URL.replace('canvas.example.edu','127.0.0.1'),URL.replace('canvas.example.edu','canvas.example.edu.evil.invalid'),URL.replace('/user_','/course_'),URL.replace('canvas.example.edu','user@canvas.example.edu'),URL.replace('canvas.example.edu','canvas.example.edu:443'),URL+'\n']:
            with self.subTest(url_shape=bad.split('/')[0]),self.assertRaises(TodoError): feed_url(bad)

class FeedSyncTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.home=pathlib.Path(self.temp.name);self.store=Store(self.home)
        self.owner=hashlib.sha256(b'feed-fixture-owner').hexdigest();self.other=hashlib.sha256(b'other-owner').hexdigest()
        with self.store.db() as c: c.executemany('INSERT INTO devices VALUES(?,?)',[(self.owner,'A'),(self.other,'B')])
        self.raw=calendar([event(date='20991030T155959Z')]);self.requests=[]
        def transport(request,**kwargs):
            self.assertEqual(request.full_url,URL)
            self.assertEqual(request.get_method(),'GET');self.assertIsNone(request.get_header('Authorization'))
            self.requests.append(True);return io.BytesIO(self.raw)
        self.canvas=CanvasSync(self.store,transport)
    def tearDown(self): self.temp.cleanup()
    def test_existing_api_import_deduplicates_preserves_completion_and_notes(self):
        existing=feed_items(self.raw,now=NOW)[0];existing.update(completed=True,description='本人提交已核实')
        self.store.todos.import_canvas(self.owner,[existing])
        ident=self.store.todos.list(self.owner)['items'][0]['id']
        self.canvas.connect(self.owner,URL)
        result=self.canvas.sync(self.owner)
        self.assertEqual(result['inserted'],0)
        row=self.store.todos.list(self.owner)['items'][0]
        self.assertEqual(row['id'],ident);self.assertTrue(row['completed']);self.assertIn('本人提交已核实',row['notes'])
        self.assertEqual(stat.S_IMODE(self.canvas.path.stat().st_mode),0o600)
        status=self.canvas.status(self.owner)
        self.assertTrue(status['configured']);self.assertEqual(status['mode'],'calendar_feed');self.assertFalse(status['submission_status_available'])
        self.assertNotIn(URL,json.dumps(status))
        self.assertFalse(self.canvas.status(self.other)['configured'])
        with self.assertRaises(TodoError): self.canvas.sync(self.other)
        with self.store.db() as c: self.assertNotIn(URL,'\n'.join(c.iterdump()))
    def test_date_changes_manual_check_and_source_disappearance(self):
        self.canvas.connect(self.owner,URL);self.canvas.sync(self.owner)
        row=self.store.todos.list(self.owner)['items'][0]
        self.store.todos.update(self.owner,row['id'],{'revision':row['revision'],'completed':True,'title':'手动标题'})
        self.raw=calendar([event(date='20991101T155959Z')]);self.canvas.sync(self.owner)
        row=self.store.todos.list(self.owner)['items'][0]
        self.assertTrue(row['completed']);self.assertEqual(row['title'],'手动标题');self.assertEqual(row['deadline'],'2099-11-01T15:59:59Z')
        self.raw=calendar([]);self.canvas.sync(self.owner)
        self.assertEqual(len(self.store.todos.list(self.owner)['items']),1)
    def test_invalid_response_never_saves_credential_or_changes_todos(self):
        self.raw=b'<html>login required</html>'
        with self.assertRaises(TodoError): self.canvas.connect(self.owner,URL)
        self.assertFalse(self.canvas.path.exists())
        self.assertEqual(self.store.todos.list(self.owner)['items'],[])

if __name__ == '__main__': unittest.main()
