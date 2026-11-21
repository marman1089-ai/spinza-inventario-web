import unittest
from unittest.mock import patch
import test_control as fixture
from app import document_jobs as jobs

class DocumentArchiveProgressTests(unittest.TestCase):
    setUp=fixture.ControlTests.setUp
    login=fixture.ControlTests.login
    sql=fixture.ControlTests.sql
    pdf=fixture.ControlTests.pdf
    def test_each_upload_returns_to_its_archive_and_shows_progress_until_ready(self):
        self.login(2)
        for kind,(table,_,_) in jobs.KINDS.items():
            with self.subTest(kind=kind),patch('app.document_workflow.start_worker'):
                response=self.client.post('/'+kind+'/upload',files={'files':('foto.pdf',self.pdf(),'application/pdf')},follow_redirects=False)
            self.assertEqual(response.status_code,303,response.text)
            self.assertEqual(response.headers['location'],'/'+kind)
            did=self.sql(f'SELECT MAX(id) AS id FROM {table}')[0]['id']
            page=self.client.get('/'+kind)
            self.assertEqual(page.status_code,200,page.text[:100])
            self.assertIn(f'data-document-status="/schede/{kind}/{did}/stato"',page.text)
            self.assertNotIn(f'href="/schede/{kind}/{did}"',page.text)
            self.assertNotIn('Invia e controlla',page.text)
            result=dict(date=self.dt,supplier='Esercente',amount=10,currency='EUR',text='Dati estratti',description='',lines=[],warnings=[])
            with patch.dict(jobs.os.environ,{'OPENAI_API_KEY':'test-only'}),patch.object(jobs,'extract_document',return_value=result):self.assertTrue(jobs.process_one())
            page=self.client.get('/'+kind)
            self.assertIn(f'href="/schede/{kind}/{did}"',page.text)
            self.assertNotIn(f'data-document-status="/schede/{kind}/{did}/stato"',page.text)
