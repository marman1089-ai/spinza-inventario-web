import json
import unittest
import test_control as fixture
from app.reconciliation import comparisons

class ReconciliationTests(unittest.TestCase):
    setUp=fixture.ControlTests.setUp
    login=fixture.ControlTests.login
    sql=fixture.ControlTests.sql
    def test_comparison_next_day_expenses_zero_missing_and_duplicates(self):
        report=dict(store='spinza',closure_date='2026-11-20',lines_json=json.dumps([dict(name='Cash',cents=10000),dict(name='POS',cents=5000)]),expenses_json=json.dumps([dict(cents=1000,mode='included'),dict(cents=500,mode='cash_fund')]))
        doc=dict(id=1,store='spinza',closure_date='2026-11-21',digital_id=1,details_json=json.dumps(dict(cash=100,card=50)))
        receipt=dict(store='spinza',day='2026-11-21',amount_cents=9000)
        row=comparisons([report],[doc],[receipt])[0]
        self.assertEqual(row['status'],'Coincide');self.assertEqual(row['expected'],9000)
        self.assertEqual(row['day'],'2026-11-20');self.assertEqual(row['cash_difference'],0)
        receipt['amount_cents']=8900
        self.assertEqual(comparisons([report],[doc],[receipt])[0]['cash_difference'],-100)
        self.assertEqual(comparisons([report],[],[])[0]['status'],'Da completare')
        self.assertIsNone(comparisons([report],[doc,doc],[receipt])[0]['metrics'][0]['actual'])
        doc['details_json']=json.dumps(dict(cash=0,card=0))
        self.assertEqual(comparisons([], [doc],[])[0]['metrics'][0]['actual'],0)
    def test_admin_page_filters_scope_and_validation(self):
        response=self.client.get('/gestionale/controllo-dati')
        self.assertEqual(response.status_code,200,response.text[:300])
        self.assertIn('Controllo dati',response.text)
        self.sql('INSERT INTO closure_reports(store,closure_date,lines_json,total_cents,notes,created_by,created_at,submission_token) VALUES(?,?,?,?,?,?,?,?)',('spinza','2026-11-20',json.dumps([dict(name='Cash',cents=10000),dict(name='POS',cents=5000)]),15000,'','testadmin','2026-11-20','reconciliation-test'))
        self.sql('INSERT INTO cash_store_receipts(store,day,amount_cents,counted_by) VALUES(?,?,?,?)',('spinza','2026-11-21',10000,'testadmin'))
        page=self.client.get('/gestionale/controllo-dati?start=2026-11-20&end=2026-11-20')
        self.assertEqual(page.status_code,200)
        self.assertIn('100,00 €',page.text)
        self.assertIn('2026-11-21',page.text)
        self.assertIn('Da completare',page.text)
        self.assertEqual(self.client.get('/gestionale/controllo-dati?store=reburger_maggio').status_code,403)
        self.assertEqual(self.client.get('/gestionale/controllo-dati?start=oops').status_code,422)
        self.login(2)
        self.assertEqual(self.client.get('/gestionale/controllo-dati').status_code,403)
