"""Regression tests for permanent counts and inventory activity (isolated databases)."""
import unittest
from unittest.mock import patch
from datetime import date, datetime, timezone
from pathlib import Path
import uuid
import test_control as fixture
from app import main, db
from app.inventory_activity import init_activity_db, day_bounds, merge_intervals

class InventoryTests(unittest.TestCase):
    setUp = fixture.ControlTests.setUp
    login = fixture.ControlTests.login
    sql = fixture.ControlTests.sql
    qty = fixture.ControlTests.qty

    def submit(self, day='2026-10-12', qty='8'):
        return self.client.post('/inventario/conteggio',data={'area':'prodotti','store':'spinza','count_date':day,'qty_'+str(self.food):qty},follow_redirects=False)

    def test_archive_first_and_second_count_and_restart(self):
        r=self.submit();self.assertEqual(r.status_code,303,r.text)
        url=r.headers['location']
        self.assertEqual(self.qty(self.food),8)
        page=self.client.get(url)
        self.assertEqual(page.status_code,200)
        self.assertIn('Conteggio salvato',page.text)
        self.assertIn('Farina',page.text)
        self.assertIn('8.0',page.text)
        self.assertEqual(self.submit('2026-10-19','5').status_code,303)
        rows=self.sql('SELECT observed,previous,outgoing FROM inventory_period_count_lines ORDER BY id')
        self.assertEqual(rows[0]['observed'],8)
        self.assertEqual(rows[1]['outgoing'],3)
        # New database connection / client, same persistent file.
        self.client.close()
        self.client=fixture.TestClient(main.app)
        self.login()
        self.assertEqual(self.client.get(url).status_code,200)
        self.assertEqual(self.sql('SELECT COUNT(*) AS n FROM inventory_period_count_headers')[0]['n'],2)
        csv=self.client.get('/inventario/report/csv?store=spinza&area=prodotti&id=1')
        self.assertIn('Farina',csv.text)

    def test_concurrent_confirmation_is_serialized(self):
        from concurrent.futures import ThreadPoolExecutor
        main._ensure_inventory_report_tables()
        with ThreadPoolExecutor(max_workers=2) as pool:
            responses=list(pool.map(lambda _:self.submit(),range(2)))
        self.assertEqual(sorted(r.status_code for r in responses),[303,409])
        self.assertEqual(self.sql('SELECT COUNT(*) AS n FROM inventory_period_count_headers')[0]['n'],1)

    def test_atomic_rollback(self):
        with patch.object(main,'_log',side_effect=RuntimeError('simulated failure')):
            with self.assertRaises(RuntimeError): self.submit()
        self.assertEqual(self.qty(self.food),10)
        self.assertEqual(self.sql('SELECT COUNT(*) AS n FROM inventory_period_count_headers')[0]['n'],0)
        self.assertEqual(self.sql('SELECT COUNT(*) AS n FROM inventory_period_count_lines')[0]['n'],0)
        self.assertEqual(self.submit().status_code,303)

    def test_validation_duplicate_and_scoping(self):
        self.assertEqual(self.submit(qty='nan').status_code,400)
        self.assertEqual(self.submit(qty='-1').status_code,400)
        self.assertEqual(self.submit().status_code,303)
        self.assertEqual(self.submit().status_code,409)
        self.assertEqual(self.sql('SELECT COUNT(*) AS n FROM inventory_period_count_headers')[0]['n'],1)
        self.login(3)
        self.assertEqual(self.client.get('/inventario/report?store=spinza&area=prodotti&id=1').status_code,403)
        self.assertEqual(self.client.get('/inventario/report/csv?store=spinza&area=prodotti&id=1').status_code,403)
        self.login()
        self.assertEqual(self.client.get('/inventario/report?store=spinza&area=prodotti&id=999').status_code,404)

    def test_draft_removed_only_after_success(self):
        r=self.client.post('/inventario/bozza',data={'store':'spinza','area':'prodotti','count_date':'2026-10-12','qty_'+str(self.food):'8'},follow_redirects=False)
        self.assertEqual(r.status_code,303)
        self.assertEqual(self.submit(qty='bad').status_code,400)
        self.assertEqual(len(self.sql('SELECT * FROM inventory_week_drafts')),1)
        self.assertEqual(self.submit().status_code,303)
        self.assertEqual(len(self.sql('SELECT * FROM inventory_week_drafts')),0)

    def test_arrivals_suffix_and_rome_boundary(self):
        self.submit()
        # 22:30 UTC is the following local day in October, and included.
        self.sql("INSERT INTO logs(ts,store,username,action,category,name,delta) VALUES(?,?,?,?,?,?,?)",('2026-10-12 22:30:00','spinza','testadmin','ORDER_CARICATO_INVENTARIO','Cucina','Farina | MAGAZZINO | ordine #42',4))
        self.assertEqual(self.submit('2026-10-19','6').status_code,303)
        row=self.sql('SELECT * FROM inventory_period_count_lines ORDER BY id DESC')[0]
        self.assertEqual(row['received'],4)
        self.assertEqual(row['outgoing'],6)

    def ping(self,token,t,store='spinza',headers=True):
        with patch('app.inventory_activity.activity_now',return_value=t):
            return self.client.post('/inventario/attivita',data={'token':token,'store':store,'area':'prodotti'},headers={'X-Inventory-Activity':'1'} if headers else {})

    def test_activity_idle_resume_and_permissions(self):
        init_activity_db();init_activity_db()
        token=str(uuid.uuid4()); t=datetime(2026,10,20,10,tzinfo=timezone.utc).timestamp()
        self.assertEqual(self.ping(token,t,headers=False).status_code,403)
        self.assertEqual(self.ping(token,t).status_code,204)
        self.assertEqual(self.ping(token,t+15).status_code,204)
        self.ping(token,t+120);self.ping(token,t+135)
        rows=self.sql('SELECT * FROM inventory_activity_intervals')
        self.assertEqual(len([r for r in rows if r['ended']>r['started']]),2)
        self.assertEqual(sum(r['ended']-r['started'] for r in rows),30)
        page=self.client.get('/admin/fasce-orarie?day=2026-10-20&store=spinza')
        self.assertEqual(page.status_code,200)
        for text in ('00:00','24:00','testadmin','teststaff','12:00:00 CEST'):
            self.assertIn(text,page.text)
        self.login(2)
        self.assertEqual(self.client.get('/admin/fasce-orarie').status_code,403)
        self.assertEqual(self.ping(token,t+150,store='reburger_maggio').status_code,403)
        self.client.cookies.clear()
        self.assertEqual(self.ping(token,t+165).status_code,401)

    def test_dst_midnight_overlap(self):
        self.assertEqual(day_bounds(date(2026,3,29))[1]-day_bounds(date(2026,3,29))[0],23*3600)
        self.assertEqual(day_bounds(date(2026,10,25))[1]-day_bounds(date(2026,10,25))[0],25*3600)
        self.assertEqual(merge_intervals([(10,30),(20,40),(50,60)]),[[10,40],[50,60]])
        init_activity_db()
        lo,hi=day_bounds(date(2026,10,25))
        self.sql('INSERT INTO inventory_activity_intervals(user_id,username,store,area,started,ended) VALUES(?,?,?,?,?,?)',(1,'testadmin','spinza','prodotti',lo-30,lo+30))
        page=self.client.get('/admin/fasce-orarie?day=2026-10-25&store=spinza')
        self.assertIn('&lt;1 min',page.text)
        self.assertIn('Giorno di cambio ora',page.text)

    def test_invoice_links_scoped(self):
        did=self.sql('INSERT INTO invoices_docs(store,supplier,doc_date,uploaded_by,filename,content_type,data) VALUES(?,?,?,?,?,?,?)',('spinza','Test fattura','2026-10-15','testadmin','test.pdf','application/pdf',b'%PDF-test'))
        self.submit();self.submit('2026-10-19','5')
        page=self.client.get('/inventario/report?store=spinza&area=prodotti&id=2')
        self.assertIn('Test fattura',page.text)
        self.assertEqual(self.client.get(f'/inventario/fatture-periodo/{did}?store=spinza').content,b'%PDF-test')
        self.login(3)
        self.assertEqual(self.client.get(f'/inventario/fatture-periodo/{did}?store=spinza').status_code,403)

    def test_migration_on_copy_of_attached_legacy_database(self):
        import shutil, sqlite3, hashlib
        source=Path(__file__).resolve().parents[1]/'spinza.db'
        digest=hashlib.sha256(source.read_bytes()).digest()
        target=Path(db.SQLITE_PATH).with_name('legacy-'+str(uuid.uuid4())+'.sqlite')
        shutil.copyfile(source,target)
        with sqlite3.connect(target) as conn:
            before=conn.execute('SELECT COUNT(*) FROM products').fetchone()[0]
        db.SQLITE_PATH=target
        db.init_db()
        main._ensure_inventory_report_tables();main._ensure_inventory_report_tables()
        init_activity_db()
        self.assertEqual(self.sql('SELECT COUNT(*) AS n FROM products')[0]['n'],before)
        self.assertEqual(hashlib.sha256(source.read_bytes()).digest(),digest)
        self.assertEqual(self.sql('SELECT COUNT(*) AS n FROM inventory_period_count_headers')[0]['n'],0)

    def test_postgres_pool_restores_transaction_mode(self):
        from unittest.mock import MagicMock
        conn=MagicMock();conn.autocommit=True
        pool=MagicMock();pool.connection.return_value.__enter__.return_value=conn
        with patch.dict('os.environ',{'DATABASE_URL':'postgresql://test.invalid/database'}), patch.object(db,'_get_postgres_pool',return_value=pool):
            with db.connect() as acquired:
                self.assertFalse(acquired.autocommit)
                acquired.autocommit=True  # Simulate init_db DDL mode.
            self.assertFalse(conn.autocommit)
            conn.commit.assert_called_once()
            conn.reset_mock()
            with self.assertRaises(RuntimeError):
                with db.connect(): raise RuntimeError('failure')
            conn.rollback.assert_called_once()

    def test_store_switch_updates_header_logo_and_scoped_page(self):
        self.submit()
        r=self.client.post('/set-active-store',data={'store':'reburger_maggio','next_url':'/inventario/report?store=spinza&area=prodotti&id=1'},follow_redirects=False)
        self.assertEqual(r.status_code,303)
        self.assertIn('store=reburger_maggio',r.headers['location'])
        self.assertNotIn('id=',r.headers['location'])
        page=self.client.get(r.headers['location'])
        self.assertEqual(page.status_code,200)
        self.assertIn('brand-reburger_maggio',page.text)
        self.assertIn('/static/logo_reburger_header.svg',page.text)
        self.assertIn('Reburger San Felice',page.text)
        self.assertNotIn('Scheda #1 archiviata',page.text)
        # Header must also follow the session on pages without active_store context.
        page=self.client.get('/admin')
        self.assertIn('brand-reburger_maggio',page.text)
        self.assertEqual(page.text.count('action="/set-active-store"'),1)
        admin_content=page.text.split('<div class="wrap">',1)[1]
        self.assertIn('/admin/fasce-orarie',admin_content)
        header=page.text.split('<div class="wrap">',1)[0]
        self.assertNotIn('/admin/fasce-orarie',header)
        for key in ('reburger_camaldoli','spinza','ALL','reburger_maggio','spinza'):
            r=self.client.post('/set-active-store',data={'store':key,'next_url':'/admin/fasce-orarie?day=2026-10-20&store=spinza'},follow_redirects=False)
            page=self.client.get(r.headers['location'])
            self.assertEqual(page.status_code,200)
            self.assertIn('brand-'+key,page.text)
        self.assertIn('/static/logo_spinza_header.svg',page.text)
        r=self.client.post('/set-active-store',data={'store':'spinza','next_url':'//example.com'},follow_redirects=False)
        self.assertEqual(r.headers['location'],'/inventario')

    def test_daily_weekly_percentages_and_csv(self):
        from app.inventory_analytics import build_reports
        init_activity_db()
        selected=date(2026,10,21) # Wednesday, compare Mon-Wed with Mon-Wed.
        intervals=[]
        for day,minutes in [(date(2026,10,21),60),(date(2026,10,20),30),(date(2026,10,19),30),(date(2026,10,14),30),(date(2026,10,13),15),(date(2026,10,12),15)]:
            start=day_bounds(day)[0]+9*3600
            intervals.append((start,start+minutes*60))
            self.sql('INSERT INTO inventory_activity_intervals(user_id,username,store,area,started,ended) VALUES(?,?,?,?,?,?)',(1,'testadmin','spinza','prodotti',start,start+minutes*60))
        report=build_reports(intervals,selected)
        self.assertEqual(report['week']['seconds'],120*60)
        self.assertEqual(report['week']['base'],60*60)
        self.assertEqual(report['week']['delta'],100)
        self.assertAlmostEqual(report['daily']['base'],90*60/7)
        self.assertEqual(report['daily']['delta'],366.7)
        self.assertIsNone(build_reports([],selected)['daily']['delta'])
        page=self.client.get('/admin/fasce-orarie?store=spinza&day=2026-10-21')
        self.assertEqual(page.status_code,200)
        self.assertIn('+100.0%',page.text)
        self.assertIn('1 h 00 min',page.text)
        result=self.client.get('/admin/fasce-orarie?store=spinza&day=2026-10-21&download=csv')
        self.assertEqual(result.status_code,200)
        self.assertIn('Variazione %',result.text)
        self.assertIn('Settimana;testadmin',result.text)
        self.login(2)
        self.assertEqual(self.client.get('/admin/fasce-orarie?download=csv').status_code,403)

    def test_live_first_interaction_and_pause(self):
        token=str(uuid.uuid4())
        t=day_bounds(main._rome_today())[0]+10*3600
        self.ping(token,t)
        with patch('app.inventory_activity.activity_now',return_value=t+1):
            page=self.client.get('/admin/fasce-orarie?store=spinza')
        self.assertIn('● Segnale recente',page.text)
        self.assertIn('10:00',page.text)
        r=self.client.post('/inventario/attivita',data={'token':token,'store':'spinza','area':'prodotti','state':'paused'},headers={'X-Inventory-Activity':'1'})
        self.assertEqual(r.status_code,204)
        self.assertEqual(len(self.sql('SELECT * FROM inventory_activity_sessions')),0)
        with patch('app.inventory_activity.activity_now',return_value=t+2):
            page=self.client.get('/admin/fasce-orarie?store=spinza')
        self.assertNotIn('● Segnale recente',page.text)

    def deletion_form(self,count_id=1):
        import re
        page=self.client.get(f'/inventario/report?area=prodotti&store=spinza&id={count_id}')
        token=re.search(r'name="delete_token" value="([^"]+)"',page.text).group(1)
        return {'store':'spinza','area':'prodotti','delete_token':token}

    def test_delete_latest_and_recount_same_date(self):
        self.submit();form=self.deletion_form()
        r=self.client.post('/inventario/report/1/elimina',data=form,follow_redirects=False)
        self.assertEqual(r.status_code,303)
        self.assertEqual(self.qty(self.food),8)
        self.assertEqual(len(self.sql('SELECT * FROM inventory_period_count_headers')),0)
        self.assertEqual(len(self.sql('SELECT * FROM inventory_period_count_lines')),0)
        self.assertEqual(self.client.get('/inventario/report?store=spinza&area=prodotti&id=1').status_code,404)
        self.assertEqual(self.submit(qty='7').status_code,303)
        self.assertEqual(self.qty(self.food),7)
        self.assertEqual(len(self.sql("SELECT * FROM logs WHERE action='REPORT_INVENTARIO_ELIMINATO'")),1)

    def test_delete_middle_recomputes_following_consumption(self):
        self.submit(qty='10')
        self.sql('INSERT INTO logs(ts,store,username,action,category,name,delta) VALUES(?,?,?,?,?,?,?)',('2026-10-15 12:00:00','spinza','testadmin','ORDER_CARICATO_INVENTARIO','Cucina','Farina | MAGAZZINO | ordine #1',4))
        self.submit('2026-10-19','100')
        self.sql('INSERT INTO logs(ts,store,username,action,category,name,delta) VALUES(?,?,?,?,?,?,?)',('2026-10-22 12:00:00','spinza','testadmin','ORDER_CARICATO_INVENTARIO','Cucina','Farina | MAGAZZINO | ordine #2',6))
        self.submit('2026-10-26','5')
        form=self.deletion_form(2)
        r=self.client.post('/inventario/report/2/elimina',data=form,follow_redirects=False)
        self.assertEqual(r.status_code,303)
        row=self.sql('SELECT * FROM inventory_period_count_lines WHERE count_id=3')[0]
        self.assertEqual(row['previous'],10)
        self.assertEqual(row['observed'],5)
        self.assertEqual(row['outgoing'],15)
        self.assertEqual(row['received'],10)
        self.assertEqual(self.qty(self.food),5)
        form=self.deletion_form(1)
        self.assertEqual(self.client.post('/inventario/report/1/elimina',data=form,follow_redirects=False).status_code,303)
        row=self.sql('SELECT * FROM inventory_period_count_lines WHERE count_id=3')[0]
        self.assertIsNone(row['previous']);self.assertIsNone(row['outgoing'])

    def test_delete_authorization_and_atomicity(self):
        self.submit();form=self.deletion_form()
        self.assertEqual(self.client.post('/inventario/report/1/elimina',data={**form,'delete_token':'wrong'}).status_code,403)
        self.assertEqual(self.client.post('/inventario/report/1/elimina',data={**form,'store':'reburger_maggio'}).status_code,404)
        with patch.object(main,'_log',side_effect=RuntimeError('simulated failure')):
            with self.assertRaises(RuntimeError):self.client.post('/inventario/report/1/elimina',data=form)
        self.assertEqual(len(self.sql('SELECT * FROM inventory_period_count_headers')),1)
        self.assertEqual(len(self.sql('SELECT * FROM inventory_period_count_lines')),1)
        self.login(2)
        page=self.client.get('/inventario/report?store=spinza&area=prodotti&id=1')
        self.assertNotIn('Elimina report',page.text)
        self.assertEqual(self.client.post('/inventario/report/1/elimina',data=form).status_code,403)

    def test_render_refuses_ephemeral_sqlite(self):
        with patch.dict('os.environ',{'RENDER':'true'}):
            with self.assertRaisesRegex(RuntimeError,'obbligatoria'):
                with db.connect(): pass

if __name__=='__main__': unittest.main()
