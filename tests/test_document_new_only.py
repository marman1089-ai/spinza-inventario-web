import unittest
from unittest.mock import patch
import test_control as fixture
from app import document_jobs as jobs
from app.document_workflow import submitted
from app.document_ai import extract_document

class NewOnlyTests(unittest.TestCase):
    setUp=fixture.ControlTests.setUp
    login=fixture.ControlTests.login
    sql=fixture.ControlTests.sql
    pdf=fixture.ControlTests.pdf
    doc=fixture.ControlTests.doc
    def test_history_does_not_enqueue_or_read(self):
        did=self.doc()
        self.assertEqual(jobs.enqueue_history(),0)
        jobs.enqueue('fatture',did,'spinza','archivio')
        with patch.dict(jobs.os.environ,{'OPENAI_API_KEY':'test-only'}),patch.object(jobs,'extract_document') as ai:
            self.assertFalse(jobs.process_one());ai.assert_not_called()
            with self.assertRaisesRegex(ValueError,'Storico sospeso'):jobs.run_document('fatture',did)
            ai.assert_not_called()
    def test_new_upload_can_be_read(self):
        did=self.doc()
        with patch('app.document_workflow.start_worker'):
            submitted('fatture',did,'spinza','teststaff')
        result=dict(readable=True,date=self.dt,supplier='Test',amount=12,currency='EUR',text='Documento leggibile',description='Test',lines=[],warnings=[])
        with patch.dict(jobs.os.environ,{'OPENAI_API_KEY':'test-only'}),patch.object(jobs,'extract_document',return_value=result) as ai:
            self.assertTrue(jobs.process_one());self.assertTrue(ai.call_args.kwargs['new_upload'])
    def test_direct_legacy_extraction_cannot_spend_credit(self):
        with patch('app.document_ai.urllib.request.urlopen') as request:
            with self.assertRaisesRegex(ValueError,'sospese'):extract_document(self.pdf(),'Fatture')
            request.assert_not_called()
    def test_old_archive_shows_only_original(self):
        did=self.doc()
        page=self.client.get('/fatture')
        self.assertEqual(page.status_code,200)
        self.assertIn(f'/fatture/{did}',page.text)
        self.assertNotIn(f'/schede/fatture/{did}',page.text)
