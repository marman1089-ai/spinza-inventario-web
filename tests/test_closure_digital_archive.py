import unittest
from unittest.mock import patch
import test_control as fixture
from app import document_jobs as jobs

class ClosureArchiveTests(unittest.TestCase):
    setUp=fixture.ControlTests.setUp
    login=fixture.ControlTests.login
    sql=fixture.ControlTests.sql
    pdf=fixture.ControlTests.pdf
    def test_upload_returns_to_archive_then_becomes_digital_when_ready(self):
        self.login(2)
        with patch('app.document_workflow.start_worker'):
            response=self.client.post('/chiusure/upload',files={'files':('foto.pdf',self.pdf(),'application/pdf')},follow_redirects=False)
        self.assertEqual(response.status_code,303)
        self.assertEqual(response.headers['location'],'/chiusure')
        did=self.sql('SELECT id FROM closures')[0]['id']
        page=self.client.get('/chiusure')
        self.assertEqual(page.status_code,200)
        self.assertIn(f'data-document-status="/schede/chiusure/{did}/stato"',page.text)
        self.assertIn('closure-digital-spinner',page.text)
        self.assertNotIn(f'href="/schede/chiusure/{did}"',page.text)
        self.assertNotIn('Invia e controlla',page.text)
        self.assertNotIn('Controlla e conferma',page.text)
        result=dict(date=self.dt,supplier='Esercente',amount=123,currency='EUR',text='Incassi 123',description='',warnings=[],lines=[])
        with patch.dict(jobs.os.environ,{'OPENAI_API_KEY':'test-only'}),patch.object(jobs,'extract_document',return_value=result):
            self.assertTrue(jobs.process_one())
        page=self.client.get('/chiusure')
        self.assertIn(f'href="/schede/chiusure/{did}"',page.text)
        self.assertNotIn(f'data-document-status="/schede/chiusure/{did}/stato"',page.text)
    def test_failed_read_does_not_leave_a_permanent_spinner(self):
        with patch('app.document_workflow.start_worker'):
            self.client.post('/chiusure/upload',files={'files':('foto.pdf',self.pdf(),'application/pdf')},follow_redirects=False)
        did=self.sql('SELECT id FROM closures')[0]['id']
        self.sql("UPDATE document_ai_jobs SET state='error',error='Lettura non riuscita' WHERE kind='chiusure' AND doc_id=?",(did,))
        page=self.client.get('/chiusure')
        self.assertIn('Lettura non riuscita',page.text)
        self.assertNotIn(f'data-document-status="/schede/chiusure/{did}/stato"',page.text)
