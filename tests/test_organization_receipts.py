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
    def test_receipt_archive_accepts_postgres_date_values(self):
        from contextlib import contextmanager
        from datetime import date
        from unittest.mock import patch
        from app import cash_documents,cash_treasury,db
        with db.connect() as conn:
            cur=conn.cursor();cash_treasury.ensure_schema(cur);cash_documents.schema(cur)
            cur.execute("INSERT INTO cash_company_spending VALUES('old-company','2025-02-01','Spesa aziendale',1000,0,'testadmin')")
            cur.execute("INSERT INTO cash_expenses(store,flow_date,category,supplier,payment_method,amount,notes,created_by) VALUES('spinza','2026-01-03','Varie','Fornitore','Contanti',30,'','testadmin')")
        class Cursor:
            def __init__(self,raw): self.raw=raw;self.date_values=False
            def execute(self,sql,params=()):
                self.date_values='FROM cash_expenses WHERE' in sql
                self.raw.execute(sql,params);return self
            def fetchall(self):
                rows=self.raw.fetchall()
                if self.date_values:
                    for row in rows: row['flow_date']=date.fromisoformat(row['flow_date'])
                return rows
        class Connection:
            def __init__(self,raw):self.raw=raw
            def cursor(self):return Cursor(self.raw.cursor())
        @contextmanager
        def postgres_dates():
            with db.connect() as conn: yield Connection(conn)
        with patch.object(cash_documents,'connect',postgres_dates):
            response=self.client.get('/contanti-nei-negozi/ricevute')
            self.assertEqual(response.status_code,200)
            self.assertIn('2026-01-03',response.text)
            self.assertIn('2025-02-01',response.text)
    def test_fill_from_spending_preserve_manual_values_and_delete(self):
        with fixture.db.connect() as c:
            cur=c.cursor();cash_treasury.ensure_schema(cur)
            cur.execute("INSERT INTO cash_company_spending VALUES('split','2025-03-02','Spesa ripartita',1500,500,'testadmin')")
        page=self.client.get('/contanti-nei-negozi/ricevute?spending_token=split')
        self.assertEqual(page.status_code,200)
        csrf=re.search(r'name="csrf" value="([^"]+)"',page.text)[1]
        self.assertIn('value="2025-03-02"',page.text)
        self.assertIn('value="Spesa ripartita"',page.text)
        self.assertIn('Reburger Slow Fast Food SRL · € 15,00 + S&amp;C SRLS · € 5,00',page.text)
        def upload(**fields):
            return self.client.post('/contanti-nei-negozi/ricevute',data=dict(csrf=csrf,spending_token='split',**fields),files={'files':('receipt.pdf',self.pdf(),'application/pdf')},follow_redirects=False)
        self.assertEqual(upload().status_code,303)
        row=self.sql('SELECT * FROM cash_expense_documents')[0]
        self.assertEqual((row['receipt_date'],row['description'],row['store']),('2025-03-02','Spesa ripartita','aziende'))
        self.assertIn('S&C SRLS',row['payer'])
        self.assertEqual(upload(day='2024-01-01',description='Descrizione manuale',store='spinza',payer='Pagante manuale').status_code,303)
        manual=self.sql('SELECT * FROM cash_expense_documents ORDER BY id DESC')[0]
        self.assertEqual((manual['receipt_date'],manual['description'],manual['store'],manual['payer']),('2024-01-01','Descrizione manuale','spinza','Pagante manuale'))
        path=f"/contanti-nei-negozi/ricevute/{row['id']}/elimina"
        self.assertEqual(self.client.post(path,data={'csrf':'wrong'}).status_code,403)
        self.login(uid=2)
        self.assertEqual(self.client.post(path,data={'csrf':csrf}).status_code,403)
        self.login()
        page=self.client.get('/contanti-nei-negozi/ricevute')
        csrf=re.search(r'name="csrf" value="([^"]+)"',page.text)[1]
        self.assertEqual(self.client.post(path,data={'csrf':csrf},follow_redirects=False).status_code,303)
        self.assertEqual(len(self.sql('SELECT * FROM cash_expense_documents')),1)
        self.assertEqual(len(self.sql('SELECT * FROM cash_company_spending')),1)
        self.assertEqual(self.client.get(f"/contanti-nei-negozi/ricevute/{row['id']}").status_code,404)
        self.assertEqual(self.client.post('/contanti-nei-negozi/ricevute',data=dict(csrf=csrf,day='2023-01-01',store='spinza',description='Indipendente',payer='S&C SRLS'),files={'files':('free.pdf',self.pdf(),'application/pdf')},follow_redirects=False).status_code,303)
        self.assertEqual(self.sql('SELECT spending_token FROM cash_expense_documents ORDER BY id DESC')[0]['spending_token'],'')
if __name__=='__main__':unittest.main()
