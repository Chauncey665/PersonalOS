"""Owned manual reminder completion over real isolated HTTP; no push or model calls."""
import concurrent.futures
import datetime as dt
import hashlib
import json
from pathlib import Path
import sys
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
from unittest import mock
from zoneinfo import ZoneInfo

sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'server'))
from personal_os import Store, Handler, ThreadingHTTPServer
from push import APNsProvider


class ReminderResolutionTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory()
        self.home=Path(self.temp.name)
        self.store=Store(self.home/'state')
        self.tokens=['reminder-fixture-phone-a','reminder-fixture-phone-b']
        self.owners=[hashlib.sha256(token.encode()).hexdigest() for token in self.tokens]
        with self.store.db() as c:
            for owner in self.owners: c.execute('INSERT INTO devices VALUES(?,?)',(owner,'fixture'))
        self.server=ThreadingHTTPServer(('127.0.0.1',0),Handler)
        self.server.store=self.store
        self.server.push_provider=APNsProvider(self.home/'missing-push.json')
        self.thread=threading.Thread(target=self.server.serve_forever,daemon=True)
        self.thread.start()
        self.base='http://127.0.0.1:'+str(self.server.server_port)
        self.now=time.time()
        self.due=dt.datetime.fromtimestamp(self.now+3600,dt.timezone.utc).replace(microsecond=0)

    def tearDown(self):
        self.server.shutdown();self.server.server_close();self.thread.join(2);self.temp.cleanup()

    def api(self,path,body=None,device=0):
        headers={'Content-Type':'application/json'}
        if device is not None: headers['Authorization']='Bearer '+self.tokens[device]
        request=urllib.request.Request(self.base+path,None if body is None else json.dumps(body).encode(),headers)
        with urllib.request.urlopen(request,timeout=4) as response:
            self.last_status=response.status
            return json.load(response)

    def error(self,status,path,body,device=0):
        with self.assertRaises(urllib.error.HTTPError) as error: self.api(path,body,device)
        self.assertEqual(error.exception.code,status)
        return json.loads(error.exception.read())

    def create(self,request_id=None):
        body={'text':'下午提醒我拿快递','time_zone':'Asia/Hong_Kong'}
        if request_id is not None:body['request_id']=request_id
        ident=self.api('/tasks',body)['id']
        # Fixture represents a reminder created before untimed requests became todos.
        self.store.update(ident,state='needs_details',plan=json.dumps({'kind':'reminder'}),result='请补充时间')
        self.assertEqual(self.store.task(ident)['state'],'needs_details')
        return ident

    def endpoint(self,ident):return '/tasks/'+ident+'/reminder/resolve'
    def body(self,**overrides):
        return {'due':self.due.isoformat().replace('+00:00','Z'),'time_zone':'Asia/Hong_Kong',**overrides}
    def counts(self,ident):
        with self.store.db() as c:
            return tuple(c.execute('SELECT count(*) FROM '+table+' WHERE task=?',(ident,)).fetchone()[0]
                         for table in ('audit','notification_events','notification_outbox'))

    def test_owned_completion_preserves_original_request_and_formats_phone_zone(self):
        ident=self.create('reminder-original')
        before=self.store.task(ident)
        with self.store.db() as c:request_before=tuple(c.execute('SELECT * FROM task_requests WHERE task=?',(ident,)).fetchone())
        row=self.api(self.endpoint(ident),self.body(time_zone='America/Los_Angeles'))
        self.assertEqual(self.last_status,200)
        self.assertEqual(row['id'],ident);self.assertEqual(row['state'],'scheduled')
        self.assertNotIn('owner',row)
        self.assertEqual(row['body'],before['body']);self.assertEqual(row['created'],before['created'])
        self.assertEqual(row['due'],self.due.timestamp())
        local=self.due.astimezone(ZoneInfo('America/Los_Angeles'))
        self.assertIn(local.strftime('%Y-%m-%d %H:%M'),row['result'])
        self.assertIn('America/Los_Angeles',row['result'])
        plan=json.loads(row['plan'])
        self.assertEqual(plan,{'kind':'reminder','strategy':'manual_resolution','due':local.isoformat(),'time_zone':'America/Los_Angeles'})
        with self.store.db() as c:self.assertEqual(tuple(c.execute('SELECT * FROM task_requests WHERE task=?',(ident,)).fetchone()),request_before)
        original=json.loads(before['body'])
        self.assertEqual(self.api('/tasks',original)['id'],ident)
        resolved=[a for a in self.store.audits(ident) if a['event']=='reminder_resolved']
        self.assertEqual(len(resolved),1)
        self.assertNotIn('拿快递',resolved[0]['detail'])
        self.assertNotIn(self.owners[0],json.dumps(row))

    def test_identical_retry_is_transactional_without_duplicate_audit_or_notifications(self):
        ident=self.create();path=self.endpoint(ident)
        first=self.api(path,self.body());counts=self.counts(ident)
        with concurrent.futures.ThreadPoolExecutor(max_workers=5) as pool:
            rows=list(pool.map(lambda _:self.api(path,self.body()),range(8)))
        self.assertTrue(all(row==first for row in rows));self.assertEqual(self.counts(ident),counts)
        # The same instant may be expressed in local offset on a retry.
        local=self.due.astimezone(ZoneInfo('Asia/Hong_Kong')).isoformat()
        self.assertEqual(self.api(path,self.body(due=local)),first)
        self.assertEqual(self.counts(ident),counts)
        self.error(409,path,self.body(due=(self.due+dt.timedelta(minutes=1)).isoformat()))
        self.error(409,path,self.body(time_zone='UTC'))
        self.assertEqual(self.counts(ident),counts)

    def test_racing_different_times_only_resolves_once(self):
        ident=self.create();path=self.endpoint(ident)
        def resolve(minutes):
            try:return self.api(path,self.body(due=(self.due+dt.timedelta(minutes=minutes)).isoformat()))['due']
            except urllib.error.HTTPError as error:return error.code
        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:results=list(pool.map(resolve,(0,1)))
        self.assertEqual(results.count(409),1)
        self.assertIn(self.store.task(ident)['due'],results)
        self.assertEqual(sum(a['event']=='reminder_resolved' for a in self.store.audits(ident)),1)

    def test_authentication_and_owner_isolation_before_any_public_task_lookup(self):
        ident=self.create();path=self.endpoint(ident)
        before=self.counts(ident)
        # The new route must not call the legacy cross-device task reader.
        with mock.patch.object(self.store,'task',side_effect=AssertionError('must use owned transaction')):
            self.error(401,path,self.body(),device=None)
            foreign=self.error(404,path,self.body(),device=1)
            missing=self.error(404,self.endpoint('00000000-0000-4000-8000-000000000000'),self.body())
            self.assertEqual(foreign,missing)
            row=self.api(path,self.body())
            self.assertEqual(row['state'],'scheduled')
        self.assertEqual(self.counts(ident)[0],before[0]+1)
        self.error(404,path,self.body(),device=1)

    def test_unauthenticated_post_has_complete_length_delimited_error_body(self):
        # Authentication intentionally returns before consuming the POST body.
        # A length-delimited reply must not make the client wait for EOF on a
        # connection that may reset while closing its unread request bytes.
        path=self.endpoint('00000000-0000-4000-8000-000000000000')
        for attempt in range(100):
            with self.subTest(attempt=attempt):
                request=urllib.request.Request(self.base+path,json.dumps(self.body()).encode(),{'Content-Type':'application/json'})
                with self.assertRaises(urllib.error.HTTPError) as failure:
                    urllib.request.urlopen(request,timeout=4)
                response=failure.exception
                self.assertEqual(response.code,401)
                payload=response.read()
                self.assertEqual(int(response.headers['Content-Length']),len(payload))
                self.assertEqual(json.loads(payload),{'error':'需要设备认证'})

    def test_strict_fields_invalid_zone_invalid_date_and_past_due_leave_task_untouched(self):
        ident=self.create();path=self.endpoint(ident);before=self.store.task(ident);counts=self.counts(ident)
        invalid=[{}, {'due':self.body()['due']}, self.body(extra='ignored'),self.body(time_zone='../secret'),
                 self.body(time_zone='Not/A_Zone'),self.body(time_zone=1),self.body(due=17),
                 self.body(due='2026-10-27T15:50:00'),self.body(due='2026-02-30T15:50:00+08:00'),
                 self.body(due='tomorrow'),self.body(due='9999-12-31T23:59:59Z'),
                 self.body(due=dt.datetime.fromtimestamp(self.now-1,dt.timezone.utc).isoformat())]
        for body in invalid:
            with self.subTest(body=body):self.error(400,path,body)
        self.assertEqual(self.store.task(ident),before);self.assertEqual(self.counts(ident),counts)

    def test_cancelled_completed_and_other_states_cannot_be_revived(self):
        ident=self.create();path=self.endpoint(ident)
        self.api('/tasks/'+ident+'/cancel',{})
        self.error(409,path,self.body());self.assertEqual(self.store.task(ident)['state'],'cancelled')
        for state in ('queued','running','failed','completed','awaiting_confirmation'):
            with self.subTest(state=state):
                ident=self.create()
                with self.store.db() as c:c.execute('UPDATE tasks SET state=? WHERE id=?',(state,ident))
                self.error(409,self.endpoint(ident),self.body())
                self.assertEqual(self.store.task(ident)['state'],state)
        ident=self.create();self.api(self.endpoint(ident),self.body())
        self.api('/tasks/'+ident+'/cancel',{})
        self.error(409,self.endpoint(ident),self.body())
        self.store.process_reminders(now=self.due.timestamp()+1)
        self.assertEqual(self.store.task(ident)['state'],'cancelled')
        self.assertNotIn('reminder_due',[a['event'] for a in self.store.audits(ident)])

    def test_nonreminder_and_unowned_legacy_tasks_are_not_resolved(self):
        ident=self.api('/tasks',{'text':'我今天写了代码'})['id']
        with self.store.db() as c:c.execute("UPDATE tasks SET state='needs_details' WHERE id=?",(ident,))
        self.error(409,self.endpoint(ident),self.body())
        ident=self.create()
        with self.store.db() as c:c.execute("UPDATE tasks SET owner='' WHERE id=?",(ident,))
        self.error(404,self.endpoint(ident),self.body())

    def test_resolved_due_uses_existing_scheduler_and_only_one_due_event(self):
        # These are synthetic APNs registrations; no dispatcher is started.
        self.store.register_push(self.owners[0],'a'*64,'sandbox')
        self.store.register_push(self.owners[1],'b'*64,'sandbox')
        ident=self.create();before=self.counts(ident)
        row=self.api(self.endpoint(ident),self.body())
        self.assertEqual(self.counts(ident)[1:],before[1:])
        self.api('/tasks/'+ident+'/local-reminder',{'scheduled':'true'})
        self.store.process_reminders(now=row['due']-1)
        self.assertEqual(self.store.task(ident)['state'],'scheduled')
        self.store.process_reminders(now=row['due'])
        self.store.process_reminders(now=row['due']+60)
        completed=self.api('/tasks/'+ident)
        self.assertEqual(completed['state'],'completed');self.assertNotIn('owner',completed)
        self.assertEqual(sum(a['event']=='reminder_due' for a in self.store.audits(ident)),1)
        with self.store.db() as c:
            outbox={r['device']:r['state'] for r in c.execute("SELECT device,state FROM notification_outbox WHERE task=? AND event='reminder_due'",(ident,))}
        self.assertEqual(outbox,{self.owners[0]:'suppressed'})
        self.error(409,self.endpoint(ident),self.body())

if __name__=='__main__':unittest.main()
