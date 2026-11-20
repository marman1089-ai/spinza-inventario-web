import os,re,unittest
from unittest.mock import patch
import test_control as fixture
from app import main,db,kitchen_periods
class InitialPeriodTests(unittest.TestCase):
    login=fixture.ControlTests.login
    sql=fixture.ControlTests.sql
    pdf=fixture.ControlTests.pdf
    def setUp(self):
        fixture.ControlTests.setUp(self)
        result=self.client.post('/inventario/conteggio',data=dict(area='prodotti',store='spinza',count_date='2026-11-09',**{'qty_'+str(self.food):'6'}),follow_redirects=False)
        self.assertEqual(result.status_code,303)
        self.report=self.sql('SELECT id FROM inventory_period_count_headers')[0]['id']
        self.url=f'/inventario/report?area=prodotti&store=spinza&id={self.report}'
        page=self.client.get(self.url);self.assertEqual(page.status_code,200)
        self.token=re.search(r'name="csrf" value="([^\"]+)"',page.text)[1]
    def baseline(self,day):return self.client.post('/inventario/report/data-iniziale',data=dict(csrf=self.token,store='spinza',report_id=self.report,start_date=day),follow_redirects=False)
    def invoice(self,day):return self.sql("INSERT INTO invoices_docs(store,supplier,doc_date,uploaded_by,filename,content_type,data) VALUES('spinza','Test',?,'testadmin','test.pdf','application/pdf',?)",(day,self.pdf()))
    def test_first_report_uses_retained_initial_values_and_persisted_dates(self):
        page=self.client.get(self.url);self.assertIn('Non serve un secondo report salvato',page.text)
        self.assertEqual(self.sql('SELECT previous FROM inventory_period_count_lines')[0]['previous'],10)
        self.assertEqual(self.baseline('2026-10-26').status_code,303)
        page=self.client.get(self.url);self.assertIn('2026-10-26 → 2026-11-09',page.text);self.assertIn('Durata: 15 giorni',page.text)
        doc=self.invoice('2026-10-28')
        review=self.sql("INSERT INTO kitchen_invoice_reviews(store,week_start,doc_id,status,updated_by,updated_at) VALUES('spinza','2026-10-26',?,'done','testadmin','test')",(doc,))
        self.sql("INSERT INTO kitchen_invoice_review_lines(review_id,product_id,qty,unit) VALUES(?,?,5,'kg')",(review,self.food))
        page=self.client.get(self.url);self.assertIn('9.00 kg',page.text)
        self.assertEqual(self.sql('SELECT qty FROM products WHERE id=?',(self.food,))[0]['qty'],6)
    def test_ai_queues_only_invoices_inside_initial_period(self):
        self.baseline('2026-10-26')
        before=self.invoice('2026-10-25');first=self.invoice('2026-10-26');middle=self.invoice('2026-11-04');last=self.invoice('2026-11-09');after=self.invoice('2026-11-10')
        with patch.dict(os.environ,{'OPENAI_API_KEY':'test-key'}),patch('app.kitchen_ai.enqueue_docs') as queue:
            result=self.client.post('/inventario/report/ai-periodo',data=dict(csrf=self.token,store='spinza',report_id=self.report),follow_redirects=False)
        self.assertEqual(result.status_code,303)
        self.assertEqual([call.args[3] for call in queue.call_args_list],[first,middle,last])
        self.assertIn('report_id='+str(self.report),result.headers['location'])
    def test_dates_and_permissions(self):
        self.assertEqual(self.baseline('2026-11-10').status_code,422)
        self.assertEqual(self.client.post('/inventario/report/data-iniziale',data=dict(csrf='wrong',store='spinza',report_id=self.report,start_date='2026-10-26')).status_code,403)
        self.login(uid=2);self.assertEqual(self.baseline('2026-10-26').status_code,403)
    def test_later_period_uses_actual_previous_count(self):
        self.baseline('2026-10-26')
        self.client.post('/inventario/conteggio',data=dict(area='prodotti',store='spinza',count_date='2026-11-16',**{'qty_'+str(self.food):'3'}),follow_redirects=False)
        second=self.sql('SELECT MAX(id) AS id FROM inventory_period_count_headers')[0]['id']
        with db.connect() as conn:self.assertEqual(kitchen_periods.period_bounds(conn.cursor(),second,'spinza'),('2026-11-09','2026-11-16',False))
if __name__=='__main__':unittest.main()
