import io,json,re,sqlite3,unittest
from pathlib import Path
from unittest.mock import patch
from datetime import date
import test_control as fixture
from app import expense_program as program,expense_archive as archive,db

class ExpenseProgramTests(unittest.TestCase):
    setUp=fixture.ControlTests.setUp
    login=fixture.ControlTests.login
    sql=fixture.ControlTests.sql
    def seed(self):
        # Tiny synthetic fixture. Real uploaded financial records never enter the repository.
        connection=sqlite3.connect(':memory:')
        source=Path(program.legacy.__file__).read_text()
        schema=source.split('conn.executescript(',1)[1].split('"""',2)[1]
        connection.executescript(schema)
        connection.execute('CREATE TABLE IF NOT EXISTS expense_selections(selection_key TEXT PRIMARY KEY,created_at TEXT NOT NULL)')
        # Migration columns in the supplied database.
        for statement in ('ALTER TABLE payments ADD COLUMN fixed_expense_id INTEGER','ALTER TABLE payments ADD COLUMN estimated_amount_cents INTEGER',"ALTER TABLE variable_purchases ADD COLUMN status TEXT NOT NULL DEFAULT 'paid'",'ALTER TABLE variable_purchases ADD COLUMN payment_id INTEGER','ALTER TABLE variable_purchases ADD COLUMN due_date TEXT','ALTER TABLE variable_purchases ADD COLUMN paid_at TEXT'):
            connection.execute(statement)
        for sid,name in ((1,'Spinza'),(2,'Camaldoli'),(3,'San Felice')):connection.execute('INSERT INTO stores VALUES(?,?,?)',(sid,name,'2026-01-01'))
        connection.execute("INSERT INTO payments(store_id,description,amount_cents,due_date,status,created_at) VALUES(1,'Fornitore aperto',12345,'2026-11-25','pending','2026-11-01')")
        connection.execute("INSERT INTO fixed_expenses(store_id,description,amount_cents,frequency_months,next_due_date,created_at) VALUES(1,'Affitto',60000,1,'2026-11-10','2026-10-01')")
        connection.execute("INSERT INTO fixed_expenses(store_id,description,amount_cents,frequency_months,next_due_date,created_at) VALUES(2,'Wi-Fi',4000,1,'2026-11-25','2026-10-01')")
        connection.commit();data=connection.serialize();connection.close();return data
    def import_data(self):
        data=self.seed();page=self.client.get('/gestionale/spese-pagamenti')
        csrf=re.search(r'id="expense-program-csrf" value="([^"]+)"',page.text)[1]
        with patch('app.main._rome_today',return_value=date(2026,11,23)):
            response=self.client.post('/spese-programma/importa',headers={'x-expense-csrf':csrf},files={'file':('test.db',data,'application/octet-stream')})
        self.assertEqual(response.status_code,200,response.text)
        return csrf,data
    def test_preserve_original_normalize_fixed_only_and_permissions(self):
        csrf,data=self.import_data()
        self.assertEqual(self.client.get('/spese-programma/originale').content,data)
        state=self.sql('SELECT data FROM expense_program_state')[0]
        with program.local_copy(state['data']):
            with program.legacy_connect() as c:
                self.assertEqual(c.execute('SELECT status FROM payments WHERE id=1').fetchone()[0],'pending')
                self.assertEqual(c.execute('SELECT next_due_date FROM fixed_expenses WHERE id=1').fetchone()[0],'2026-12-10')
                self.assertEqual(c.execute('SELECT next_due_date FROM fixed_expenses WHERE id=2').fetchone()[0],'2026-11-25')
                self.assertIsNone(c.execute('SELECT actual_cents FROM imported_fixed_settlements').fetchone()[0])
        self.assertEqual(self.client.get('/gestionale/tutte-le-spese').status_code,200)
        self.assertEqual(self.client.post('/spese-programma/importa',headers={'x-expense-csrf':csrf},files={'file':('test.db',data,'application/octet-stream')}).status_code,409)
        self.login(2)
        for path in ('/gestionale/spese-pagamenti','/gestionale/tutte-le-spese','/spese-programma/app','/spese-programma/assets/app_pro.js','/spese-programma/api/stores','/spese-programma/originale'):
            self.assertEqual(self.client.get(path).status_code,403,path)
    def test_payment_actual_amount_posts_once_and_delete_reverses(self):
        csrf,_=self.import_data()
        url='/spese-programma/api/payments/1/pay'
        payload=dict(actual_amount='120.00',paid_date='2026-11-23',payment_method='Contanti')
        self.assertEqual(self.client.post(url,json=payload).status_code,403)
        response=self.client.post(url,json=payload,headers={'x-expense-csrf':csrf})
        self.assertEqual(response.status_code,200,response.text)
        self.assertEqual(self.sql('SELECT amount FROM cash_expenses')[0]['amount'],120)
        self.assertEqual(self.sql('SELECT sc_cents FROM cash_company_spending')[0]['sc_cents'],12000)
        self.assertEqual(self.client.post(url,json=payload,headers={'x-expense-csrf':csrf}).status_code,409)
        self.assertEqual(len(self.sql('SELECT * FROM cash_expenses')),1)
        page=self.client.get('/gestionale/tutte-le-spese');self.assertEqual(page.status_code,200,page.text[:300])
        with db.connect() as c:
            archive.archive_schema(c.cursor());rows=archive.collect(c.cursor())
        self.assertEqual(sum(r['amount'] or 0 for r in rows if r['status']=='paid'),12000)
        self.assertEqual(self.client.delete('/spese-programma/api/payments/1',headers={'x-expense-csrf':csrf}).status_code,200)
        self.assertEqual(self.sql('SELECT * FROM cash_expenses'),[])
        self.assertEqual(self.sql('SELECT * FROM cash_company_spending'),[])
    def test_missing_method_rolls_back_and_fixed_actual_payment_starts_next_cycle(self):
        csrf,_=self.import_data()
        url='/spese-programma/api/fixed-expenses/2/pay'
        response=self.client.post(url,json=dict(actual_amount='42',paid_date='2026-11-25'),headers={'x-expense-csrf':csrf})
        self.assertEqual(response.status_code,422)
        self.assertEqual(self.sql('SELECT * FROM cash_expenses'),[])
        response=self.client.post(url,json=dict(actual_amount='42',paid_date='2026-11-25',payment_method='Bonifico'),headers={'x-expense-csrf':csrf})
        self.assertEqual(response.status_code,200,response.text)
        self.assertEqual(self.sql('SELECT amount FROM cash_expenses')[0]['amount'],42)
        self.assertEqual(self.sql('SELECT * FROM cash_company_spending'),[])
        response=self.client.get('/spese-programma/api/fixed-expenses?store_id=2')
        self.assertEqual(response.status_code,200,response.text)
        self.assertIn('2026-12-25',response.text)
    def test_archive_groups_all_sources_links_evidence_and_persists_selection(self):
        self.import_data()
        eid=self.sql("INSERT INTO cash_expenses(store,flow_date,category,payment_method,amount,notes,created_by) VALUES('spinza','2026-11-23','Materie prime','Bonifico',25,'Acquisto','testadmin')")
        self.sql("INSERT INTO secondary_expenses(store,expense_date,uploaded_by,filename,content_type,data) VALUES('spinza','2026-11-23','testadmin','prova.pdf','application/pdf',?)",(b'%PDF',))
        page=self.client.get('/gestionale/tutte-le-spese')
        csrf=re.search(r'name="csrf" value="([^"]+)"',page.text)[1]
        headers=dict(csrf=csrf)
        response=self.client.post('/gestionale/tutte-le-spese/collega',data={**headers,'source':'doc:spese-secondarie:1','target':'uscita:'+str(eid)},follow_redirects=False)
        self.assertEqual(response.status_code,303)
        with db.connect() as c:
            rows=archive.collect(c.cursor())
        self.assertFalse(any(r['key']=='doc:spese-secondarie:1' for r in rows))
        linked=next(r for r in rows if r['key']=='uscita:'+str(eid))
        self.assertEqual(linked['amount'],2500)
        self.assertTrue(linked['provenance'])
        self.assertEqual(self.client.post('/gestionale/tutte-le-spese/seleziona',data={**headers,'key':'payment:1','selected':'1'},follow_redirects=False).status_code,303)
        self.assertEqual(self.client.get('/gestionale/tutte-le-spese?selected_only=true').status_code,200)
        self.assertEqual(len(self.sql('SELECT * FROM expense_archive_selected')),1)
        self.assertEqual(self.client.post('/gestionale/tutte-le-spese/scollega',data={**headers,'source':'doc:spese-secondarie:1'},follow_redirects=False).status_code,303)
        self.assertEqual(self.client.get('/gestionale/tutte-le-spese').status_code,200)
    def test_complete_previously_settled_fixed_uses_real_amount_and_never_repeats(self):
        csrf,_=self.import_data()
        payload=dict(csrf=csrf,amount='630',paid_date='2026-11-10',payment_method='Carta')
        response=self.client.post('/spese-programma/rate-saldate/1',data=payload,follow_redirects=False)
        self.assertEqual(response.status_code,303,response.text)
        self.assertEqual(self.sql('SELECT amount FROM cash_expenses')[0]['amount'],630)
        self.assertEqual(self.client.post('/spese-programma/rate-saldate/1',data=payload,follow_redirects=False).status_code,409)
        self.assertEqual(len(self.sql('SELECT * FROM cash_expenses')),1)
        self.assertEqual(self.client.get('/gestionale/tutte-le-spese').status_code,200)
