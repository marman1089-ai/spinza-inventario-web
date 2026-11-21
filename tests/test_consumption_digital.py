import json,re,unittest
import test_control as fixture
from app import main

class DigitalConsumptionTests(unittest.TestCase):
    login=fixture.ControlTests.login
    sql=fixture.ControlTests.sql
    pdf=fixture.ControlTests.pdf
    def setUp(self):
        fixture.ControlTests.setUp(self)
        response=self.client.post('/inventario/conteggio',data={'area':'prodotti','store':'spinza','count_date':'2026-11-09','qty_'+str(self.food):'6'},follow_redirects=False)
        self.url=response.headers['location'];self.report=self.sql('SELECT id FROM inventory_period_count_headers')[0]['id']
        page=self.client.get(self.url)
        self.token=re.search(r'name="csrf" value="([^"]+)"',page.text)[1]
        self.client.post('/inventario/report/data-iniziale',data={'csrf':self.token,'store':'spinza','report_id':self.report,'start_date':'2026-11-01'})
        self.did=self.sql("INSERT INTO invoices_docs(store,supplier,doc_date,uploaded_by,filename,content_type,data) VALUES('spinza','Test','2026-11-04','testadmin','test.pdf','application/pdf',?)",(self.pdf(),))
        self.lines=[dict(description='Farina',qty=5,unit='kg',amount=12,product_id=self.food,stock_qty=5)]
        self.sql("INSERT INTO digital_documents(kind,doc_id,store,doc_date,supplier,amount,state,lines_json,updated_by,updated_at) VALUES('fatture',?,'spinza','2026-11-04','Test',12,'verificato',?,'teststaff','now')",(self.did,json.dumps(self.lines)))
    def link(self):
        return self.client.post('/inventario/report/collega-digitali',data={'csrf':self.token,'store':'spinza','report_id':self.report},follow_redirects=False)
    def test_reuses_digital_quantities_idempotently_without_stock_writes(self):
        self.assertEqual(self.link().status_code,303)
        self.assertEqual(self.link().status_code,303)
        self.assertEqual(len(self.sql('SELECT * FROM kitchen_invoice_review_lines')),1)
        self.assertEqual(self.sql('SELECT qty FROM products WHERE id=?',(self.food,))[0]['qty'],6)
        page=self.client.get(self.url)
        self.assertIn('9.00 kg',page.text)
        self.assertIn('Prima attività cucina registrata',page.text)
        self.assertIn('Primo report',page.text)
        self.assertIn('/schede/fatture/'+str(self.did),page.text)
    def test_unknown_product_prevents_fabricated_consumption(self):
        self.sql('UPDATE digital_documents SET lines_json=?',(json.dumps([dict(description='Sconosciuto',qty=5,unit='kg')]),))
        self.assertEqual(self.link().status_code,422)
        self.assertEqual(self.sql('SELECT * FROM kitchen_invoice_review_lines'),[])
    def test_hub_menu_and_write_restricted_to_admin(self):
        self.login(1,'ALL')
        page=self.client.get('/gestionale/consumi')
        self.assertEqual(page.status_code,200)
        self.assertIn('Apri report e fatture',page.text)
        self.login(2)
        self.assertEqual(self.client.get('/gestionale/consumi').status_code,403)
        self.assertNotIn('Consumi · tutti i report',self.client.get('/fatture').text)
        self.assertEqual(self.link().status_code,403)
