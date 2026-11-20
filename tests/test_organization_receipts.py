import sys,unittest,re
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parent))
import test_control as fixture
from app import cash_treasury,cash_documents
class OrganizationTests(unittest.TestCase):
    setUp=fixture.ControlTests.setUp
    login=fixture.ControlTests.login
    sql=fixture.ControlTests.sql
    pdf=fixture.ControlTests.pdf
    def test_receipts_and_optional_upload(self):
        with fixture.db.connect() as c: cash_treasury.ensure_schema(c.cursor());cash_documents.schema(c.cursor())
        self.login(store='ALL')
        page=self.client.get('/contanti-nei-negozi/spese?day=2026-01-01')
        self.assertEqual(page.status_code,200)
        csrf=re.search(r'name="csrf" value="([^"]+)"',page.text)[1]
        for token,files in [('a'*43,None),('b'*43,{'files':('receipt.pdf',self.pdf(),'application/pdf')})]:
            response=self.client.post('/contanti-nei-negozi/spese',data=dict(csrf=csrf,submission=token,day='2026-01-01',description='Test',amount='20',reburger_percent='50'),files=files,follow_redirects=False)
            self.assertEqual(response.status_code,303,response.text)
        self.assertEqual(len(self.sql('SELECT * FROM cash_company_spending')),2)
        self.assertEqual(len(self.sql('SELECT * FROM cash_expense_documents')),1)
        page=self.client.get('/contanti-nei-negozi/ricevute?spending_token='+'a'*43)
        self.assertIn('2026-01-01',page.text)
        csrf=re.search(r'name="csrf" value="([^"]+)"',page.text)[1]
        response=self.client.post('/contanti-nei-negozi/ricevute',data=dict(csrf=csrf,day='2025-01-02',store='spinza',description='Vecchia ricevuta',spending_token='a'*43),files={'files':('old.pdf',self.pdf(),'application/pdf')},follow_redirects=False)
        self.assertEqual(response.status_code,303,response.text)
        self.assertEqual(len(self.sql('SELECT * FROM cash_company_spending')),2)
        self.assertEqual(len(self.sql('SELECT * FROM cash_expense_documents')),2)
        self.assertEqual(self.client.get('/contanti-nei-negozi/ricevute/1').headers['content-type'],'application/pdf')
        response=self.client.post('/contanti-nei-negozi/ricevute/1/collega',data=dict(csrf=csrf,spending_token='a'*43),follow_redirects=False)
        self.assertEqual(response.status_code,303)
        self.assertEqual(self.sql('SELECT spending_token FROM cash_expense_documents WHERE id=1')[0]['spending_token'],'a'*43)
        expense_id=self.sql("INSERT INTO cash_expenses(store,flow_date,category,supplier,payment_method,amount,notes,created_by) VALUES('spinza','2024-02-01','Varie','Fornitore','Contanti',30,'','testadmin')")
        response=self.client.post('/contanti-nei-negozi/ricevute/1/collega',data=dict(csrf=csrf,spending_token='expense:'+str(expense_id)),follow_redirects=False)
        self.assertEqual(response.status_code,303)
        self.assertEqual(self.sql('SELECT spending_token FROM cash_expense_documents WHERE id=1')[0]['spending_token'],'expense:'+str(expense_id))
        self.assertIn('2024-02-01',self.client.get('/contanti-nei-negozi/ricevute').text)
        self.login(uid=2)
        self.assertEqual(self.client.get('/contanti-nei-negozi/ricevute').status_code,403)
        self.assertEqual(self.client.get('/contanti-nei-negozi/ricevute/1').status_code,403)
    def test_all_stores_and_staff_scope(self):
        self.login(store='ALL')
        page=self.client.get('/report-chiusura?month=2026-11')
        self.assertEqual(page.status_code,200,page.text[:100])
        self.assertIn('Controllo pubblicazioni',page.text)
        for store in ('spinza','reburger_maggio','reburger_camaldoli'):self.assertIn('store-'+store,page.text)
        self.login(uid=2)
        self.assertEqual(self.client.get('/report-chiusura?store=ALL').status_code,403)
        self.assertEqual(self.client.get('/report-chiusura').status_code,200)
if __name__=='__main__':unittest.main()
