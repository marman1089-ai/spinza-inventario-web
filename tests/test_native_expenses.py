import json,re,unittest
from unittest.mock import patch
from datetime import date
from app import native_expenses as native,expense_program as program
import test_expense_program as fixtures

class NativeExpenseTests(unittest.TestCase):
    setUp=fixtures.ExpenseProgramTests.setUp
    login=fixtures.ExpenseProgramTests.login
    sql=fixtures.ExpenseProgramTests.sql
    seed=fixtures.ExpenseProgramTests.seed
    import_data=fixtures.ExpenseProgramTests.import_data
    def fields(self,html,action):
        for form in re.findall(r'<form.*?</form>',html,re.S):
            if f'name="action" value="{action}"' in form:
                return dict(re.findall(r'<input type="hidden" name="([^"]+)" value="([^"]*)"',form))
        self.fail('Form assente: '+action)
    def test_all_sections_are_native_one_read_and_financial_rules_match(self):
        self.import_data();self.login(store='ALL')
        with program.local_copy(self.sql('SELECT data FROM expense_program_state')[0]['data']):
            with program.legacy_connect() as db:
                expected=program.legacy.global_dashboard(db,date(2026,11,23))
        with native.memory_data() as db:
            actual=native.view_data(db,'ALL',date(2026,11,23),'home','pending','month',date(2026,11,23),'',1,0)['dashboard']
        for key,value in expected.items():
            if isinstance(value,(int,float)):
                self.assertEqual(actual[key],value,key)
        with patch.object(native,'connect',wraps=native.connect) as connections:
            response=self.client.get('/gestionale/spese-pagamenti')
            self.assertEqual(connections.call_count,1)
        self.assertNotIn('<iframe',response.text)
        self.assertNotIn('/spese-programma/assets/app_pro.js',response.text)
        for tab in native.TABS:
            response=self.client.get('/gestionale/spese-pagamenti?tab='+tab)
            self.assertEqual(response.status_code,200,(tab,response.text[:300]))
        self.login(2)
        self.assertEqual(self.client.get('/gestionale/spese-pagamenti').status_code,403)
        self.assertEqual(self.client.get('/gestionale/spese-pagamenti/backup').status_code,403)
    def test_native_fixed_actual_payment_is_atomic_idempotent_and_starts_new_cycle(self):
        self.import_data();self.login(store='reburger_camaldoli')
        page=self.client.get('/gestionale/spese-pagamenti?tab=fixed')
        data=self.fields(page.text,'fixed_pay')
        data.update(actual_amount='42',paid_date='2026-11-23',payment_method='Contanti')
        response=self.client.post('/gestionale/spese-pagamenti/azione',data=data,follow_redirects=False)
        self.assertEqual(response.status_code,303,response.text)
        self.assertNotIn('error=',response.headers['location'])
        self.assertEqual(self.sql('SELECT amount FROM cash_expenses')[0]['amount'],42)
        self.assertEqual(self.sql('SELECT reburger_cents FROM cash_company_spending')[0]['reburger_cents'],4200)
        response=self.client.post('/gestionale/spese-pagamenti/azione',data=data,follow_redirects=False)
        self.assertEqual(response.status_code,303)
        self.assertEqual(len(self.sql('SELECT * FROM cash_expenses')),1)
        self.assertEqual(self.sql('SELECT COUNT(*) AS n FROM expense_native_actions')[0]['n'],1)
        # Another stale page has a different action nonce but cannot pay the next cycle accidentally.
        import secrets
        data['operation']=secrets.token_urlsafe(32)
        response=self.client.post('/gestionale/spese-pagamenti/azione',data=data,follow_redirects=False)
        self.assertIn('error=',response.headers['location'])
        self.assertEqual(len(self.sql('SELECT * FROM cash_expenses')),1)
        with native.memory_data() as db:
            row=db.execute("SELECT * FROM payments WHERE status='paid'").fetchone()
            self.assertEqual(row['amount_cents'],4200)
            self.assertEqual(row['estimated_amount_cents'],4000)
            self.assertEqual(db.execute('SELECT next_due_date FROM fixed_expenses WHERE id=2').fetchone()[0],'2026-12-25')
    def test_native_selection_and_store_navigation_keep_tab(self):
        self.import_data()
        page=self.client.get('/gestionale/spese-pagamenti?tab=payments')
        data=self.fields(page.text,'selection')
        response=self.client.post('/gestionale/spese-pagamenti/azione',data=data,follow_redirects=False)
        self.assertEqual(response.status_code,303)
        selected=self.client.get('/gestionale/spese-pagamenti?tab=selected')
        self.assertIn('Fornitore aperto',selected.text)
        response=self.client.post('/set-active-store',data={'store':'reburger_camaldoli','next_url':'/gestionale/spese-pagamenti?tab=fixed#rate'},follow_redirects=False)
        self.assertEqual(response.headers['location'],'/gestionale/spese-pagamenti?tab=fixed#rate')
    def test_variable_order_recharge_budget_and_history_mechanisms(self):
        self.import_data();self.login(store='spinza')
        page=self.client.get('/gestionale/spese-pagamenti?tab=variables')
        data=self.fields(page.text,'variable_add');data.update(description='Fornitore',monthly_budget='100',category='Materie prime',notes='')
        self.client.post('/gestionale/spese-pagamenti/azione',data=data)
        page=self.client.get('/gestionale/spese-pagamenti?tab=variables')
        data=self.fields(page.text,'variable_order');data.update(amount='27',purchase_date='2026-11-23',payment_status='paid',payment_method='Carta',note='Acquisto reale')
        response=self.client.post('/gestionale/spese-pagamenti/azione',data=data,follow_redirects=False)
        self.assertNotIn('error=',response.headers['location'])
        self.assertEqual(self.sql('SELECT amount FROM cash_expenses')[0]['amount'],27)
        page=self.client.get('/gestionale/spese-pagamenti?tab=recharges')
        data=self.fields(page.text,'recharge_add');data.update(amount='200',note='Versamento')
        self.client.post('/gestionale/spese-pagamenti/azione',data=data)
        with native.memory_data() as db:
            dash=program.legacy.store_dashboard(db,1,date(2026,11,23))
            self.assertEqual(dash['balance_cents'],17300)
            self.assertEqual(dash['variable_paid_orders_cents'],2700)
        page=self.client.get('/gestionale/spese-pagamenti?tab=budgets')
        data=self.fields(page.text,'budget_limit');data.update(monthly_limit='2000')
        self.client.post('/gestionale/spese-pagamenti/azione',data=data)
        self.assertIn('2.000', self.client.get('/gestionale/spese-pagamenti?tab=budgets').text.replace('2000','2.000'))
        response=self.client.get('/gestionale/spese-pagamenti/esporta?period=all')
        self.assertEqual(response.status_code,200)
        self.assertIn('27,00',response.text)
        self.assertIn('Acquisto reale',response.text)
    def test_live_selection_returns_authoritative_total_and_persists_without_accounting(self):
        self.import_data();self.login(store='spinza')
        page=self.client.get('/gestionale/spese-pagamenti?tab=payments')
        fields=self.fields(page.text,'selection');fields['scope_store']='spinza'
        response=self.client.post('/gestionale/spese-pagamenti/selezione',data=fields)
        self.assertEqual(response.status_code,200,response.text)
        self.assertEqual(response.json()['total_cents'],12345)
        self.assertEqual(response.json()['count'],1)
        self.assertEqual(self.sql('SELECT * FROM cash_expenses'),[])
        self.assertEqual(self.sql('SELECT * FROM cash_company_spending'),[])
        repeat=self.client.post('/gestionale/spese-pagamenti/selezione',data=fields)
        self.assertEqual(repeat.json()['total_cents'],12345)
        fields['selected']='0';fields['operation']=response.json()['next_operation']
        self.assertEqual(self.client.post('/gestionale/spese-pagamenti/selezione',data=fields).json()['count'],0)
        fields['scope_store']='reburger_maggio'
        self.assertEqual(self.client.post('/gestionale/spese-pagamenti/selezione',data=fields).status_code,409)
        self.login(2)
        self.assertEqual(self.client.get('/gestionale/spese-pagamenti/selezione').status_code,403)
