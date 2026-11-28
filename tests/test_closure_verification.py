import json
import unittest
from datetime import date
from unittest.mock import patch
import test_control as fixture
from app.closure_verification import daily_comparisons
from app.document_jobs import init_jobs


def report(day='2026-11-20',store='spinza',cash=10000,pos=5000,glovo=2000,deliveroo=3000):
    return dict(id=1,store=store,closure_date=day,lines_json=json.dumps([dict(name=k,cents=v) for k,v in [('Cash',cash),('POS',pos),('Glovo',glovo),('Deliveroo',deliveroo),('Voucher',99900)]]))
def doc(day='2026-11-21',store='spinza',cash=100,card=50,uncollected=50):
    return dict(id=1,store=store,closure_date=day,digital_id=1,details_json=json.dumps(dict(cash=cash,card=card,uncollected=uncollected,tickets=9999)),job_state='done')

class VerificationMathTests(unittest.TestCase):
    def compare(self,reports,documents,stores=None,first='2026-11-20',last='2026-11-20'):
        return daily_comparisons(reports,documents,date.fromisoformat(first),date.fromisoformat(last),stores or {'spinza':'Spinza'})
    def test_daily_shifts_delivery_sum_voucher_exclusion_and_next_day(self):
        sections=self.compare([report(cash=4000,pos=2000,glovo=500,deliveroo=1000),report(cash=6000,pos=3000,glovo=1500,deliveroo=2000)],[doc()])
        row=sections[0]['rows'][0]
        self.assertEqual(row['status'],'Coincide')
        self.assertEqual(row['till_day'],'2026-11-21')
        self.assertEqual([m['reported'] for m in row['metrics']],[10000,5000,5000])
        self.assertEqual([m['key'] for m in row['metrics']],['cash','card','uncollected'])
    def test_shortages_and_excesses_do_not_cancel_and_partial_is_never_conform(self):
        section=self.compare([report()],[doc(cash=110,card=40)])[0]
        self.assertEqual((section['missing'],section['excess']),(1000,1000))
        row=section['rows'][0]
        self.assertEqual(row['status'],'Differenze')
        self.assertEqual([m['difference'] for m in row['metrics']],[-1000,1000,0])
        self.assertEqual(self.compare([report()],[doc(card=None)])[0]['rows'][0]['status'],'Da completare')
        zero=report(cash=0,pos=0,glovo=0,deliveroo=0)
        self.assertEqual(self.compare([zero],[doc(cash=0,card=0,uncollected=0)])[0]['rows'][0]['status'],'Coincide')
    def test_missing_both_sides_duplicates_and_store_separation(self):
        sections=self.compare([report()],[doc(store='reburger_maggio')],{'spinza':'Spinza','reburger_maggio':'San Felice','reburger_camaldoli':'Camaldoli'})
        self.assertIn('Manca la chiusura',sections[0]['rows'][0]['issues'][0])
        self.assertIn('Manca il report',sections[1]['rows'][0]['issues'][0])
        self.assertEqual(sections[2]['rows'][0]['status'],'Nessun dato')
        row=self.compare([report()],[doc(),doc()])[0]['rows'][0]
        self.assertEqual(row['status'],'Da completare')
        self.assertTrue(all(m['actual'] is None for m in row['metrics']))
    def test_month_boundary_invalid_fields_and_conflicting_delivery_values(self):
        row=self.compare([report(day='2026-10-31')],[doc(day='2026-11-01')],first='2026-10-31',last='2026-10-31')[0]['rows'][0]
        self.assertEqual(row['status'],'Coincide')
        broken=doc();broken['details_json']='oops'
        self.assertEqual(self.compare([report()],[broken])[0]['rows'][0]['status'],'Da completare')
        r=report();lines=json.loads(r['lines_json']);lines.append(dict(name='Non riscosso',cents=6000));r['lines_json']=json.dumps(lines)
        row=self.compare([r],[doc()])[0]['rows'][0]
        self.assertEqual(row['status'],'Da completare')
        self.assertIsNone(row['metrics'][2]['reported'])

class VerificationPageTests(unittest.TestCase):
    login=fixture.ControlTests.login
    sql=fixture.ControlTests.sql
    def setUp(self):
        fixture.ControlTests.setUp(self);init_jobs()
    def test_admin_global_page_readonly_filters_missing_and_access(self):
        data=report()
        self.sql('INSERT INTO closure_reports(store,closure_date,lines_json,total_cents,notes,created_by,created_at,submission_token) VALUES(?,?,?,?,?,?,?,?)',('spinza','2026-11-20',data['lines_json'],20000,'','teststaff','2026-11-20','verify-test'))
        cid=self.sql('INSERT INTO closures(store,closure_date,uploaded_by,filename,content_type,data) VALUES(?,?,?,?,?,?)',('spinza','2026-11-21','testadmin','test.pdf','application/pdf',b'%PDF'))
        self.sql('INSERT INTO digital_documents(kind,doc_id,store,doc_date,amount,updated_by,updated_at) VALUES(?,?,?,?,?,?,?)',('chiusure',cid,'spinza','2026-11-21',200,'testadmin','2026-11-21'))
        self.sql('INSERT INTO document_details(kind,doc_id,details_json) VALUES(?,?,?)',('chiusure',cid,doc()['details_json']))
        before=self.sql('SELECT COUNT(*) AS n FROM cash_entries')[0]['n']
        page=self.client.get('/gestionale/verifica-chiusure?start=2026-11-20&end=2026-11-20')
        self.assertEqual(page.status_code,200,page.text[:200])
        for text in ['Verifica chiusure','Piazza San Felice','Via Camaldoli','Spinza','Coincide','Glovo + Deliveroo','voucher sono esclusi']:
            self.assertIn(text,page.text)
        self.assertEqual(self.sql('SELECT COUNT(*) AS n FROM cash_entries')[0]['n'],before)
        self.assertEqual(self.sql('SELECT COUNT(*) AS n FROM document_ai_jobs')[0]['n'],0)
        self.assertEqual(self.client.get('/gestionale/verifica-chiusure?start=bad').status_code,422)
        self.assertEqual(self.client.get('/gestionale/verifica-chiusure?store=bad').status_code,422)
        self.assertEqual(self.client.get('/gestionale/verifica-chiusure?status=bad').status_code,422)
        self.login(2)
        self.assertEqual(self.client.get('/gestionale/verifica-chiusure').status_code,403)

class ClosedDayTests(unittest.TestCase):
    compare=VerificationMathTests.compare
    def test_explicit_zero_closure_without_report_is_closed_not_missing(self):
        d=doc(cash=None,card=None,uncollected=None);d['digital_total']=0
        section=self.compare([], [d])[0];row=section['rows'][0]
        self.assertEqual(row['status'],'Chiuso')
        self.assertEqual(section['counts']['Chiuso'],1)
        self.assertEqual(row['issues'],[])
        self.assertTrue(row['closed'])
        self.assertEqual([m['actual'] for m in row['metrics']],[0,0,0])
    def test_zero_total_conflicts_and_unknown_total_are_not_silently_closed(self):
        d=doc(cash=None,card=None,uncollected=None);d['digital_total']=0
        row=self.compare([report()], [d])[0]['rows'][0]
        self.assertEqual(row['status'],'Differenze')
        self.assertEqual(row['excess'],20000)
        d['digital_total']=None
        self.assertEqual(self.compare([], [d])[0]['rows'][0]['status'],'Da completare')
        d=doc();d['digital_total']=0
        self.assertFalse(self.compare([report()], [d])[0]['rows'][0]['closed'])

class ClosedDayPageTests(unittest.TestCase):
    setUp=VerificationPageTests.setUp
    login=fixture.ControlTests.login
    sql=fixture.ControlTests.sql
    def test_closed_badge_in_verification_and_digital_card_without_creating_reports(self):
        cid=self.sql('INSERT INTO closures(store,closure_date,uploaded_by,filename,content_type,data) VALUES(?,?,?,?,?,?)',('spinza','2026-11-25','testadmin','zero.pdf','application/pdf',b'%PDF'))
        self.sql('INSERT INTO digital_documents(kind,doc_id,store,doc_date,amount,updated_by,updated_at) VALUES(?,?,?,?,?,?,?)',('chiusure',cid,'spinza','2026-11-25',0,'testadmin','2026-11-25'))
        url='/gestionale/verifica-chiusure?start=2026-11-24&end=2026-11-24&status=chiusi'
        page=self.client.get(url)
        self.assertEqual(page.status_code,200)
        self.assertIn('Negozio chiuso',page.text)
        self.assertNotIn('Manca il report della chiusura',page.text)
        self.assertEqual(self.sql('SELECT * FROM closure_reports'),[])
        card=self.client.get('/schede/chiusure/'+str(cid))
        self.assertEqual(card.status_code,200)
        self.assertIn('Negozio chiuso',card.text)
        self.sql('UPDATE digital_documents SET amount=NULL WHERE doc_id=?',(cid,))
        self.assertNotIn('Negozio chiuso',self.client.get('/schede/chiusure/'+str(cid)).text)
