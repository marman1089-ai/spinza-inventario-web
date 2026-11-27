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
