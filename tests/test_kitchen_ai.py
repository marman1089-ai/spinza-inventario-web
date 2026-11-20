import json,os,re,unittest
from unittest.mock import patch
import test_control as fixture
from app import main,db,kitchen_ai as ai,document_ai
class KitchenAITests(unittest.TestCase):
    login=fixture.ControlTests.login
    sql=fixture.ControlTests.sql
    pdf=fixture.ControlTests.pdf
    qty=fixture.ControlTests.qty
    def setUp(self):
        fixture.ControlTests.setUp(self)
        main._ensure_kitchen_invoice_tables()
        with db.connect() as c: ai.schema(c.cursor())
        self.doc=self.sql("INSERT INTO invoices_docs(store,supplier,doc_date,uploaded_by,filename,content_type,data) VALUES('spinza','Fornitore','2026-11-04','testadmin','test.pdf','application/pdf',?)",(self.pdf(),))
        self.env=patch.dict(os.environ,{'OPENAI_API_KEY':'test-not-a-real-key','OPENAI_DOCUMENT_MODEL':'gpt-6.1-terra'});self.env.start();self.addCleanup(self.env.stop)
        self.worker=patch.object(ai,'start_worker');self.worker.start();self.addCleanup(self.worker.stop)
        self.week='2026-11-02'
        page=self.client.get('/inventario/fatture-settimana?store=spinza&week='+self.week)
        self.assertEqual(page.status_code,200,page.text[:300])
        self.csrf=re.search(r'name="csrf" value="([^\"]+)"',page.text)[1]
    def result(self):
        return dict(date='2026-11-04',supplier='Fornitore',invoice_number='F-123',supplier_vat='IT123',taxable_amount=20,vat_amount=2,amount=22,currency='EUR',description='Farina',text='Fattura F-123 Farina 5 kg 22 euro',warnings=[],lines=[dict(description='Farina',qty=5,unit='kg',amount=20,product_id=self.food,stock_qty=5,matching_note='5 kg = 5 kg',needs_review=False)])
    def queue(self,**extra):
        return self.client.post('/inventario/fatture-settimana/ai/accoda',data=dict(csrf=self.csrf,store='spinza',week=self.week,doc_id=self.doc,**extra),follow_redirects=False)
    def ready(self):
        self.assertEqual(self.queue().status_code,303)
        with patch.object(ai,'extract_document',return_value=self.result()) as provider:
            self.assertTrue(ai.process_one());self.assertEqual(provider.call_count,1)
            self.assertEqual(provider.call_args.kwargs['inventory'][0]['id'],self.food)
        return self.sql('SELECT * FROM kitchen_invoice_ai_jobs')[0]
    def confirm(self,**extra):
        data=dict(csrf=self.csrf,store='spinza',doc_id=self.doc,revision=1,invoice_date='2026-11-04',supplier='Fornitore',reviewed='1',product_0=self.food,qty_0='5');data.update(extra)
        return self.client.post('/inventario/fatture-settimana/ai/conferma',data=data,follow_redirects=False)
    def test_complete_workflow_consumption_no_stock_double_count(self):
        first=self.client.post('/inventario/conteggio',data={'area':'prodotti','store':'spinza','count_date':'2026-11-02','qty_'+str(self.food):'10'},follow_redirects=False)
        self.assertEqual(first.status_code,303,first.text)
        self.ready();self.assertEqual(self.qty(self.food),10)
        self.assertEqual(self.confirm().status_code,303)
        self.assertEqual(self.qty(self.food),10)
        self.assertEqual(self.confirm().status_code,303)
        self.assertEqual(len(self.sql('SELECT * FROM kitchen_invoice_review_lines')),1)
        second=self.client.post('/inventario/conteggio',data={'area':'prodotti','store':'spinza','count_date':'2026-11-09','qty_'+str(self.food):'6'},follow_redirects=False)
        self.assertEqual(second.status_code,303,second.text)
        page=self.client.get(second.headers['location'])
        self.assertEqual(page.status_code,200)
        self.assertIn('9.00 kg',page.text)
        self.assertIn('Consumo stimato della cucina',page.text)
    def test_queue_deduplicates_and_requires_key_admin_csrf(self):
        with patch.dict(os.environ,{'OPENAI_API_KEY':''}):self.assertEqual(self.queue().status_code,422)
        self.assertEqual(self.queue().status_code,303)
        self.assertEqual(self.queue().status_code,303)
        self.assertEqual(self.sql('SELECT revision FROM kitchen_invoice_ai_jobs')[0]['revision'],1)
        self.assertEqual(self.client.post('/inventario/fatture-settimana/ai/accoda',data=dict(csrf='wrong',store='spinza')).status_code,403)
        self.login(uid=2)
        self.assertEqual(self.queue().status_code,403)
        self.assertEqual(self.client.get('/inventario/fatture-settimana/ai/stato?store=spinza&week='+self.week).status_code,403)
    def test_proposals_digital_visible_but_do_not_confirm_automatically(self):
        self.ready()
        page=self.client.get('/inventario/fatture-settimana?store=spinza&week='+self.week)
        self.assertIn('F-123',page.text);self.assertIn('5 kg = 5 kg',page.text)
        self.assertEqual(len(self.sql('SELECT * FROM kitchen_invoice_reviews')),0)
        self.assertEqual(self.confirm(reviewed='').status_code,422)
        self.assertEqual(self.confirm(product_0=self.pid).status_code,422)
        self.assertEqual(self.confirm(qty_0='nan').status_code,422)
        self.assertEqual(len(self.sql('SELECT * FROM kitchen_invoice_reviews')),0)
    def test_preserve_manual_work_and_changed_invoice_or_catalog(self):
        self.ready()
        self.sql("UPDATE products SET unit='g' WHERE id=?",(self.food,))
        self.assertEqual(self.confirm().status_code,409)
        self.sql("UPDATE products SET unit='kg' WHERE id=?",(self.food,))
        self.sql("INSERT INTO kitchen_invoice_reviews(store,week_start,doc_id,status,updated_by,updated_at) VALUES('spinza',?,?,'draft','testadmin','manual')",(self.week,self.doc))
        self.assertEqual(self.confirm().status_code,409)
        self.assertEqual(self.sql('SELECT status FROM kitchen_invoice_reviews')[0]['status'],'draft')
    def test_date_correction_and_non_kitchen_invoice(self):
        self.ready()
        response=self.confirm(invoice_date='2026-11-10',product_0='ignore',qty_0='')
        self.assertEqual(response.status_code,303,response.text)
        self.assertEqual(self.sql('SELECT week_start FROM kitchen_invoice_reviews')[0]['week_start'],'2026-11-09')
        self.assertEqual(len(self.sql('SELECT * FROM kitchen_invoice_review_lines')),0)
        self.assertEqual(str(self.sql('SELECT doc_date FROM invoices_docs WHERE id=?',(self.doc,))[0]['doc_date']),'2026-11-10')
    def test_provider_failure_and_scope_validation(self):
        self.queue()
        with patch.object(ai,'extract_document',side_effect=ValueError('Chiave AI non valida.')):
            self.assertTrue(ai.process_one());self.assertFalse(ai.process_one())
        self.assertEqual(self.sql('SELECT state FROM kitchen_invoice_ai_jobs')[0]['state'],'error')
        self.assertEqual(self.qty(self.food),10)
        result=self.result();result['lines'][0]['product_id']=self.pid
        clean=ai.normalize_result(result,[{'id':self.food,'unit':'kg'}])
        self.assertIsNone(clean['lines'][0]['product_id']);self.assertIsNone(clean['lines'][0]['stock_qty'])
    def test_unit_conversions(self):
        self.assertEqual(ai.convert_quantity(50,'kg','25kg'),2)
        self.assertEqual(ai.convert_quantity(2,'0.5 kg','g'),1000)
        self.assertEqual(ai.convert_quantity(240,'Grammi','kg'),0.24)
        self.assertEqual(ai.convert_quantity(2,'pz.','Pz'),2)
        self.assertIsNone(ai.convert_quantity(2,'cartoni','kg'))
    def test_api_payload_structured_and_secret_not_in_prompt(self):
        class Reply:
            def __enter__(self):return self
            def __exit__(self,*args):pass
            def read(self):return json.dumps(dict(status='completed',output=[dict(content=[dict(type='output_text',text=json.dumps(self_result))])])).encode()
        self_result=self.result();captured={}
        def send(request,timeout):captured['payload']=json.loads(request.data);return Reply()
        with patch.object(document_ai.urllib.request,'urlopen',side_effect=send):
            result=document_ai.extract_document(self.pdf(),'Fattura',inventory=[dict(id=self.food,name='Farina',unit='kg')])
        payload=captured['payload'];self.assertFalse(payload['store'])
        self.assertEqual(payload['model'],'gpt-6.1-terra')
        self.assertIn('stock_qty',payload['text']['format']['schema']['properties']['lines']['items']['properties'])
        self.assertNotIn('test-not-a-real-key',json.dumps(payload))
        self.assertEqual(result['invoice_number'],'F-123')
if __name__=='__main__':unittest.main()
