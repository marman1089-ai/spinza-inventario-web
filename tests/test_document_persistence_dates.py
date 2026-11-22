import json,re,unittest
from datetime import date
from unittest.mock import patch
import test_control as fixture
from app import control,main,document_jobs as jobs

class PersistenceDateTests(unittest.TestCase):
    setUp=fixture.ControlTests.setUp
    login=fixture.ControlTests.login
    sql=fixture.ControlTests.sql
    pdf=fixture.ControlTests.pdf
    def upload(self,kind,**fields):
        with patch('app.document_workflow.start_worker'):
            response=self.client.post('/'+kind+'/upload',data=fields,files={'files':('foto.pdf',self.pdf(),'application/pdf')},follow_redirects=False)
        self.assertEqual(response.status_code,303)
        return self.sql(f'SELECT MAX(id) AS id FROM {jobs.KINDS[kind][0]}')[0]['id']
    def result(self,kind):
        return dict(date='2026-11-20',printed_date='2026-11-21' if kind=='chiusure' else None,printed_time='05:59',supplier='Fornitore letto',amount=123,currency='EUR',text='Dati estratti',lines=[],warnings=[],description='')
    def test_all_kinds_keep_document_date_and_saved_content_after_restart_and_next_day(self):
        for kind,(table,col,_) in jobs.KINDS.items():
            did=self.upload(kind,**{col:'2026-11-22'})
            if kind=='chiusure':
                # Even metadata created by the previous manual-date UI cannot override the photo.
                self.sql('INSERT INTO document_details(kind,doc_id,details_json) VALUES(?,?,?)',(kind,did,json.dumps({'selected_date':'2026-11-22'})))
            expected='2026-11-21' if kind=='chiusure' else '2026-11-20'
            with patch.dict(jobs.os.environ,{'OPENAI_API_KEY':'test-only'}),patch.object(jobs,'extract_document',return_value=self.result(kind)) as ai:
                self.assertTrue(jobs.process_one());self.assertEqual(ai.call_count,1)
            saved=self.sql('SELECT * FROM digital_documents WHERE kind=? AND doc_id=?',(kind,did))[0]
            original=self.sql(f'SELECT data FROM {table} WHERE id=?',(did,))[0]['data']
            control.init_control_db()  # schema startup is non-destructive
            with patch.dict(jobs.os.environ,{'OPENAI_API_KEY':'test-only'}),patch.object(jobs,'_thread',None),patch.object(jobs.threading,'Thread'),patch.object(jobs,'extract_document') as ai:
                jobs.finalize_ready_uploads();jobs.start_worker();ai.assert_not_called()
            self.login()  # fresh browser session
            with patch.object(main,'_rome_today',return_value=date(2026,11,23)):
                archive=self.client.get('/'+kind)
                self.assertIn(f'href="/schede/{kind}/{did}"',archive.text)
                self.assertIn('data-document-date>'+expected,archive.text)
                card=self.client.get(f'/schede/{kind}/{did}')
                self.assertEqual(card.status_code,200)
                status=self.client.get(f'/schede/{kind}/{did}/stato').json()
                self.assertEqual(status['state'],'done');self.assertEqual(status['doc_date'],expected)
            self.assertEqual(self.sql('SELECT * FROM digital_documents WHERE kind=? AND doc_id=?',(kind,did))[0],saved)
            self.assertEqual(self.sql(f'SELECT data FROM {table} WHERE id=?',(did,))[0]['data'],original)
            self.assertEqual(str(self.sql(f'SELECT {col} FROM {table} WHERE id=?',(did,))[0][col]),expected)
    def test_unreadable_date_never_becomes_the_upload_date(self):
        did=self.upload('spese-secondarie')
        result=self.result('spese-secondarie');result['date']=None
        with patch.dict(jobs.os.environ,{'OPENAI_API_KEY':'test-only'}),patch.object(jobs,'extract_document',return_value=result):self.assertTrue(jobs.process_one())
        self.assertEqual(self.sql('SELECT state FROM document_ai_jobs')[0]['state'],'error')
        self.assertEqual(self.sql('SELECT * FROM digital_documents'),[])
        self.assertTrue(self.sql('SELECT data FROM secondary_expenses WHERE id=?',(did,))[0]['data'])
    def test_upload_forms_do_not_ask_for_dates(self):
        for kind in jobs.KINDS:
            page=self.client.get('/'+kind)
            form=re.search(r'<form method="post" action="/'+kind+r'/upload"(.*?)</form>',page.text,re.S)
            self.assertIsNotNone(form)
            self.assertNotIn('type="date"',form[1])
