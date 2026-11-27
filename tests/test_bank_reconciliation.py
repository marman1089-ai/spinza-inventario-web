import json
import secrets
import unittest
import test_expense_program as fixtures
from app import expense_archive as archive,expense_program as program,db,main

class BankReconciliationTests(unittest.TestCase):
    setUp=fixtures.ExpenseProgramTests.setUp
    login=fixtures.ExpenseProgramTests.login
    sql=fixtures.ExpenseProgramTests.sql
    seed=fixtures.ExpenseProgramTests.seed
    import_data=fixtures.ExpenseProgramTests.import_data

    def test_reconcile_existing_paid_preserves_id_updates_actual_and_is_idempotent(self):
        csrf,_=self.import_data();headers={'x-expense-csrf':csrf}
        r=self.client.post('/spese-programma/api/payments/1/pay',headers=headers,json=dict(actual_amount='120',paid_date='2026-11-20',payment_method='Bonifico'))
        self.assertEqual(r.status_code,200)
        eid=self.sql('SELECT id FROM cash_expenses')[0]['id']
        data=dict(actual_amount='123.45',paid_date='2026-11-21',payment_method='Bonifico',bank_reference='Test PDF',expected_amount_cents=12000,expected_status='paid',__native_action_token=secrets.token_urlsafe(32))
        for _ in range(2):
            r=self.client.post('/spese-programma/api/payments/1/reconcile',headers=headers,json=data)
            self.assertEqual(r.status_code,200,r.text)
        rows=self.sql('SELECT * FROM cash_expenses');self.assertEqual(len(rows),1)
        self.assertEqual((rows[0]['id'],rows[0]['amount'],rows[0]['flow_date']),(eid,123.45,'2026-11-21'))
        self.assertEqual(self.sql('SELECT * FROM cash_company_spending'),[])
        data['__native_action_token']=secrets.token_urlsafe(32)
        self.assertEqual(self.client.post('/spese-programma/api/payments/1/reconcile',headers=headers,json=data).status_code,409)

    def test_dashboard_all_paid_sources_without_double_counting_or_booking_estimates(self):
        csrf,_=self.import_data()
        self.client.post('/spese-programma/api/payments/1/pay',headers={'x-expense-csrf':csrf},json=dict(actual_amount='120',paid_date='2026-11-20',payment_method='Contanti'))
        self.sql("INSERT INTO cash_company_spending VALUES('separate','2026-11-21','Altro',0,500,'admin')")
        with db.connect() as c:
            expenses=archive.financial_expenses(c.cursor(),'spinza')
            self.assertEqual(sum(r['amount'] for r in expenses),125)
            _,totals,_,_,_,_=main._build_cash_dashboard(c.cursor(),'spinza','month','2026-11-20',expense_rows=expenses)
            self.assertEqual(totals['expense_period'],125)
            chart=main._build_scope_period_chart(c.cursor(),'spinza','month','2026-11-20',expense_rows=expenses)
            overview=main._build_expense_overview(c.cursor(),'spinza','month','2026-11-20',expense_rows=expenses)
            self.assertEqual(overview['period_total'],125)
            monthly=main._cash_month_summaries(c.cursor(),'spinza',expense_rows=expenses)
            self.assertEqual(monthly[0]['expense_total'],125)
        response=self.client.get('/gestionale?period_type=month&anchor_date=2026-11-20')
        self.assertEqual(response.status_code,200)
        self.assertIn('Programma spese',response.text)

    def test_batch_is_atomic_on_stale_match_and_posts_every_transfer_once(self):
        csrf,_=self.import_data();headers={'x-expense-csrf':csrf}
        new=dict(store_id=1,actual_amount='10',paid_date='2026-11-25',bank_reference='test-bank-1',description='Fornitore nuovo',category='Fornitura')
        existing=dict(new,id=1,expected_amount_cents=1,expected_status='pending',bank_reference='test-bank-2',actual_amount='123.45')
        data=dict(items=[new,existing],payment_method='Bonifico',__native_action_token=secrets.token_urlsafe(32))
        url='/spese-programma/api/payments/reconcile-batch'
        self.assertEqual(self.client.post(url,headers=headers,json=data).status_code,400)
        self.assertEqual(self.sql('SELECT * FROM cash_expenses'),[])
        existing['expected_amount_cents']=12345
        for _ in range(2):
            r=self.client.post(url,headers=headers,json=data)
            self.assertEqual(r.status_code,200,r.text)
        self.assertEqual(len(self.sql('SELECT * FROM cash_expenses')),2)
        self.assertEqual(sum(r['amount'] for r in self.sql('SELECT * FROM cash_expenses')),133.45)
        self.assertEqual(self.sql('SELECT * FROM cash_company_spending'),[])

    def test_san_felice_old_and_new_planner_payments_match_expense_page_and_charts(self):
        import sqlite3
        original_seed=self.seed
        def seed_with_paid():
            c=sqlite3.connect(':memory:');c.deserialize(original_seed())
            c.execute("INSERT INTO payments(store_id,description,amount_cents,status,created_at,paid_at) VALUES(3,'Luce San Felice',48036,'paid','2026-11-01','2026-11-10')")
            c.execute("INSERT INTO payments(store_id,description,amount_cents,status,created_at) VALUES(3,'Fornitore nuovo SF',9000,'pending','2026-11-01')")
            c.execute("INSERT INTO payments(store_id,description,amount_cents,status,created_at,paid_at) VALUES(2,'Altro negozio',5000,'paid','2026-11-01','2026-11-10')")
            c.commit();data=c.serialize();c.close();return data
        self.seed=seed_with_paid
        csrf,_=self.import_data();self.login(store='reburger_maggio')
        self.sql("INSERT INTO cash_expenses(store,flow_date,category,payment_method,amount,notes,created_by) VALUES('reburger_maggio','2026-11-12','Varie','Contanti',40,'Spesa reale','testadmin')")
        for _ in range(2):
            page=self.client.get('/gestionale/uscite?month_key=2026-11')
            self.assertEqual(page.status_code,200)
            self.assertIn('520.36',page.text)
            self.assertIn('Luce San Felice',page.text)
            self.assertNotIn('Altro negozio',page.text)
            self.assertNotIn('/uscite/0/delete',page.text)
        self.assertEqual(len(self.sql('SELECT * FROM cash_expenses')),1)
        with db.connect() as conn:
            rows=archive.financial_expenses(conn.cursor(),'reburger_maggio')
            chart=main._build_store_period_chart(conn.cursor(),'reburger_maggio','month','2026-11-01',expense_rows=rows)
            self.assertAlmostEqual(sum(r['expense'] for r in chart),520.36)
        page=self.client.get('/gestionale/spese-pagamenti')
        import re
        csrf=re.search(r'id="expense-program-csrf" value="([^"]+)"',page.text)[1]
        r=self.client.post('/spese-programma/api/payments/3/pay',headers={'x-expense-csrf':csrf},json=dict(actual_amount='90',paid_date='2026-11-20',payment_method='Bonifico'))
        self.assertEqual(r.status_code,200,r.text)
        page=self.client.get('/gestionale/uscite?month_key=2026-11')
        self.assertIn('610.36',page.text)
        self.assertIn('Fornitore nuovo SF',page.text)
        self.assertEqual(len(self.sql('SELECT * FROM cash_expenses')),2)
        self.assertIn('Apri spesa',page.text)

    def test_move_paid_expense_keeps_same_payment_and_native_posting_without_cash_movement(self):
        csrf,_=self.import_data();headers={'x-expense-csrf':csrf}
        self.assertEqual(self.client.post('/spese-programma/api/payments/1/pay',headers=headers,json=dict(actual_amount='120',paid_date='2026-11-12',payment_method='Bonifico')).status_code,200)
        before=self.sql('SELECT * FROM cash_expenses')[0]
        data=dict(store_id=3,expected_store_id=1,payment_method='Bonifico',__native_action_token=secrets.token_urlsafe(32))
        url='/spese-programma/api/payments/1/move'
        for _ in range(2):self.assertEqual(self.client.post(url,headers=headers,json=data).status_code,200)
        after=self.sql('SELECT * FROM cash_expenses');self.assertEqual(len(after),1)
        self.assertEqual((after[0]['id'],after[0]['store'],after[0]['amount'],after[0]['flow_date']),(before['id'],'reburger_maggio',120,'2026-11-12'))
        self.assertEqual(self.sql('SELECT * FROM cash_company_spending'),[])
        with program.local_copy(self.sql('SELECT data FROM expense_program_state')[0]['data']):
            with program.legacy_connect() as c:
                self.assertEqual(c.execute('SELECT store_id FROM payments WHERE id=1').fetchone()[0],3)
                self.assertEqual(c.execute("SELECT store_id FROM movements WHERE movement_type='payment' AND ref_id=1").fetchone()[0],3)
        data['__native_action_token']=secrets.token_urlsafe(32)
        self.assertEqual(self.client.post(url,headers=headers,json=data).status_code,409)
