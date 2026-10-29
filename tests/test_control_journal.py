"""Control-only regression suite: server evidence, zero-duration markers, journal security."""
import unittest
from datetime import datetime
from zoneinfo import ZoneInfo
from unittest.mock import patch
from pathlib import Path
import test_control as fixture
from app import main
from app.inventory_activity import init_activity_db, day_bounds
from app.control_journal import journal_query

class ControlJournalTests(unittest.TestCase):
    setUp=fixture.ControlTests.setUp
    login=fixture.ControlTests.login
    sql=fixture.ControlTests.sql

    def start(self):
        init_activity_db()
        self.t=datetime(2026,10,29,10,15,30,tzinfo=ZoneInfo('Europe/Rome')).timestamp()

    def event(self,uid=2,name='teststaff',store='spinza',when=None,kind='access',status=200):
        self.start() if not hasattr(self,'t') else None
        return self.sql('INSERT INTO control_activity_events(user_id,username,store,area,happened,kind,description,method,path,status) VALUES(?,?,?,?,?,?,?,?,?,?)',(uid,name,store,'bibite',self.t if when is None else when,kind,'Apertura pagina: inventario','GET','/inventario',status))

    def detail(self,**kwargs):
        args={'day':'2026-10-29','store':'spinza','user_id':2};args.update(kwargs)
        return self.client.get('/admin/fasce-orarie/dettaglio',params=args)

    def test_server_access_without_javascript_and_no_self_monitoring(self):
        self.start();self.login(2)
        self.assertEqual(self.client.get('/inventario').status_code,200)
        self.assertEqual(len(self.sql("SELECT * FROM control_activity_events WHERE user_id=2 AND kind='access'")),1)
        self.assertEqual(self.client.get('/admin/fasce-orarie').status_code,403)
        self.assertEqual(len(self.sql('SELECT * FROM control_activity_events')),1)
        self.assertEqual(self.client.post('/items/999999/delete',data={},follow_redirects=False).status_code,303)
        self.assertEqual(len(self.sql("SELECT * FROM control_activity_events WHERE method='POST'")),1)
        self.client.cookies.clear();self.assertEqual(self.client.get('/inventario',follow_redirects=False).status_code,303)
        self.assertEqual(len(self.sql('SELECT * FROM control_activity_events')),2)

    def test_zero_duration_entry_is_visible_and_clickable(self):
        self.start();self.event()
        page=self.client.get('/admin/fasce-orarie?day=2026-10-29&store=spinza')
        self.assertEqual(page.status_code,200)
        self.assertIn('activity-instant',page.text)
        self.assertIn('10:15:30',page.text)
        self.assertIn('data-user="2"',page.text)
        self.assertIn('Ingresso istantaneo',page.text)
        self.assertIn('activity-journal-dialog',page.text)
        self.assertIn('0 min',page.text) # a visible point must not invent duration
        response=self.detail(start=self.t,end=self.t)
        self.assertEqual(response.status_code,200)
        self.assertEqual(len(response.json()['records']),1)

    def test_short_exit_closes_measured_interval(self):
        self.start();self.login(2)
        import uuid
        data={'token':str(uuid.uuid4()),'store':'spinza','area':'bibite','state':'active'}
        with patch('app.inventory_activity.activity_now',return_value=self.t):self.client.post('/inventario/attivita',data=data,headers={'X-Inventory-Activity':'1'})
        data['state']='paused'
        with patch('app.inventory_activity.activity_now',return_value=self.t+2):self.client.post('/inventario/attivita',data=data,headers={'X-Inventory-Activity':'1'})
        rows=self.sql('SELECT * FROM inventory_activity_intervals')
        self.assertEqual(sum(r['ended']-r['started'] for r in rows),2)

    def test_combined_journal_historical_logs_and_permissions(self):
        self.start();self.event();self.event(uid=3,name='otherstaff',store='reburger_maggio')
        utc=datetime.fromtimestamp(self.t,ZoneInfo('UTC')).strftime('%Y-%m-%d %H:%M:%S')
        self.sql("INSERT INTO logs(ts,store,username,action,category,name,delta,area) VALUES(?,?,?,?,?,?,?,?)",(utc,'spinza','teststaff','CARICO','Bibite','Cola',3,'bibite'))
        records=self.detail().json()['records']
        self.assertEqual({r['source'] for r in records},{'request','log'})
        self.assertEqual(len(records),2)
        self.assertEqual(next(r['delta'] for r in records if r['source']=='log'),3)
        self.assertTrue(all(r['username']=='teststaff' and r['store']=='spinza' for r in records))
        self.login(2);self.assertEqual(self.detail().status_code,403)
        self.client.cookies.clear();self.assertEqual(self.detail().status_code,403)
        self.login();self.assertEqual(self.detail(store='invalid').status_code,400)
        self.assertEqual(self.detail(start=self.t,end=self.t-1).status_code,400)
        self.assertEqual(self.detail(start='nan',end=self.t).status_code,400)

    def test_actions_errors_pagination_and_csv(self):
        self.start()
        for i in range(105):self.event(when=self.t+i)
        first=self.detail();self.assertEqual(len(first.json()['records']),100);self.assertTrue(first.json()['has_more'])
        second=self.detail(page=2);self.assertEqual(len(second.json()['records']),5);self.assertFalse(second.json()['has_more'])
        csv=self.detail(download='csv');self.assertEqual(csv.status_code,200);self.assertEqual(len(csv.text.splitlines()),106)
        self.login(2)
        self.assertEqual(self.client.post('/items/add',data={'category':'Bibite','name':'New Cola','qty':2},follow_redirects=False).status_code,303)
        self.assertEqual(self.client.post('/items/add',data={},follow_redirects=False).status_code,422)
        rows=self.sql("SELECT kind,status FROM control_activity_events WHERE method='POST' ORDER BY id")
        self.assertEqual([(r['kind'],r['status']) for r in rows],[('action',303),('error',422)])

    def test_historical_log_only_creates_window_and_data_is_escaped(self):
        self.start()
        self.sql("INSERT INTO logs(ts,store,username,action,category,name,delta) VALUES(?,?,?,?,?,?,?)",('2026-10-29 09:15:30','spinza','teststaff','EDIT','Bibite','<script>alert(1)</script>',0))
        page=self.client.get('/admin/fasce-orarie?day=2026-10-29&store=spinza');self.assertIn('activity-instant',page.text)
        record=self.detail().json()['records'][0];self.assertEqual(record['item'],'<script>alert(1)</script>')
        script=Path(main.BASE_DIR,'static/js/inventory-dashboard.js').read_text(encoding='utf-8-sig')
        self.assertIn('el.textContent=text',script)
        init_activity_db();init_activity_db()
        self.assertEqual(len(self.sql('SELECT * FROM logs')),1)

    def test_admin_cross_store_target_and_logout_identity(self):
        self.start()
        did=self.sql("INSERT INTO closures(store,closure_date,uploaded_by,filename) VALUES(?,?,?,?)",('reburger_maggio','2000-01-01','otherstaff','test.pdf'))
        self.assertEqual(self.client.post(f'/chiusure/{did}/delete',follow_redirects=False).status_code,303)
        row=self.sql("SELECT store,description FROM control_activity_events WHERE method='POST' ORDER BY id DESC LIMIT 1")[0]
        self.assertEqual(row['store'],'reburger_maggio')
        self.assertIn('test.pdf',row['description'])
        self.assertEqual(self.client.get('/logout',follow_redirects=False).status_code,303)
        row=self.sql("SELECT username,kind FROM control_activity_events ORDER BY id DESC LIMIT 1")[0]
        self.assertEqual((row['username'],row['kind']),('testadmin','logout'))

    def test_successful_login_recorded_without_credentials(self):
        self.start()
        salt,digest=main.make_password('test-password-only')
        self.sql('UPDATE users SET pw_salt=?,pw_hash=? WHERE id=2',(salt,digest))
        self.client.cookies.clear()
        self.client.get('/select-store?store=spinza',follow_redirects=False)
        self.assertEqual(self.client.post('/login',data={'username':'teststaff','password':'test-password-only'},follow_redirects=False).status_code,303)
        row=self.sql("SELECT * FROM control_activity_events WHERE path='/login'")[0]
        self.assertEqual((row['user_id'],row['kind']),(2,'access'))
        self.assertNotIn('test-password-only',str(row))
        self.assertNotIn(digest,str(row))

    def test_failed_transaction_records_error_without_inventing_movement(self):
        self.start();self.login(2)
        with patch.object(main,'_log',side_effect=RuntimeError('simulated failure')):
            with self.assertRaises(RuntimeError):
                self.client.post(f'/items/{self.pid}/delta',data={'delta':2},follow_redirects=False)
        row=self.sql("SELECT * FROM control_activity_events ORDER BY id DESC LIMIT 1")[0]
        self.assertEqual((row['kind'],row['status']),('error',500))
        self.assertEqual(self.sql('SELECT qty FROM products WHERE id=?',(self.pid,))[0]['qty'],20)
        self.assertEqual(self.sql('SELECT * FROM logs'),[])

    def test_logout_clears_recent_state_but_preserves_history(self):
        self.start();self.event();self.event(when=self.t+1,kind='logout',status=303)
        self.sql('INSERT INTO inventory_activity_sessions(token,user_id,store,last_ping) VALUES(?,?,?,?)',('old-tab',2,'spinza',self.t))
        with patch('app.inventory_activity.activity_now',return_value=self.t+2):
            page=self.client.get('/admin/fasce-orarie?day=2026-10-29&store=spinza')
        self.assertNotIn('● Segnale recente',page.text)
        self.assertIn('data-user="2"',page.text)
        self.assertEqual(len(self.detail().json()['records']),2)

    def test_previous_day_never_shows_live_state_after_midnight(self):
        self.start()
        at=datetime(2026,10,29,23,59,55,tzinfo=ZoneInfo('Europe/Rome')).timestamp()
        self.event(when=at)
        with patch('app.inventory_activity.activity_now',return_value=at+10):
            page=self.client.get('/admin/fasce-orarie?day=2026-10-29&store=spinza')
        self.assertNotIn('● Segnale recente',page.text)
        self.assertIn('23:59:55',page.text)
