"""Document privacy, employee decisions and November-only historical processing."""
import re
import json
from unittest.mock import patch
import test_control as fixture
from app import document_jobs as jobs
from app.document_workflow import submitted

class DocumentWorkflowTests(fixture.unittest.TestCase):
    setUp=fixture.ControlTests.setUp
    login=fixture.ControlTests.login
    sql=fixture.ControlTests.sql
    pdf=fixture.ControlTests.pdf
    doc=fixture.ControlTests.doc

    def result(self):
        return dict(readable=True,date='2026-11-19',printed_date='2026-11-19',printed_time='05:59',
            supplier='Spinza',amount=355.15,currency='EUR',text='Totale 355,15 contanti 57,50',
            description='Chiusura',lines=[],warnings=[],cash=57.5,card=284.7,tickets=12.95)

    def prepare(self,kind):
        did=self.doc(kind)
        table=jobs.KINDS[kind][0]
        self.sql(f"UPDATE {table} SET uploaded_by='teststaff' WHERE id=?",(did,))
        with patch.object(jobs,'start_worker'),patch('app.document_workflow.start_worker'):
            submitted(kind,did,'spinza','teststaff')
        with patch.object(jobs,'extract_document',return_value=self.result()):
            jobs.run_document(kind,did)
        return did

    def test_confirmation_revokes_own_preview_all_three_types(self):
        for kind in jobs.KINDS:
            did=self.prepare(kind);self.login(2)
            self.assertEqual(self.client.get(f'/{kind}/{did}').status_code,403)
            self.assertEqual(self.client.get(f'/schede/{kind}/{did}/anteprima').status_code,200)
            page=self.client.get(f'/schede/{kind}/{did}')
            self.assertEqual(page.status_code,200,page.text)
            token=re.search(r'name="csrf" value="([^"]+)"',page.text)[1]
            revision=self.sql('SELECT revision FROM digital_documents WHERE kind=? AND doc_id=?',(kind,did))[0]['revision']
            data=dict(csrf=token,revision=str(revision),decision='confirm')
            self.assertEqual(self.client.post(f'/schede/{kind}/{did}/decisione',data={**data,'csrf':'bad'}).status_code,403)
            response=self.client.post(f'/schede/{kind}/{did}/decisione',data=data,follow_redirects=False)
            self.assertEqual(response.status_code,303,response.text)
            self.assertEqual(self.client.get(f'/schede/{kind}/{did}/anteprima').status_code,403)
            self.assertEqual(self.client.get(f'/schede/{kind}/{did}').status_code,200)
            self.assertEqual(self.client.post(f'/schede/{kind}/{did}/decisione',data=data).status_code,409)
            self.login()
            self.assertEqual(self.client.get(f'/{kind}/{did}').status_code,200)
            self.assertEqual(self.client.get(f'/schede/{kind}/{did}/anteprima').status_code,200)

    def test_blurry_document_never_produces_confirmable_data(self):
        did=self.doc()
        with patch.object(jobs,'extract_document',return_value={**self.result(),'readable':False,'quality_reason':'Sfocata'}):
            with self.assertRaisesRegex(ValueError,'Sfocata'):jobs.run_document('fatture',did)
        self.assertEqual(self.sql('SELECT * FROM digital_documents'),[])

    def test_previous_shift_and_nulls_preserved(self):
        did=self.prepare('chiusure')
        row=self.sql("SELECT * FROM digital_documents WHERE kind='chiusure' AND doc_id=?",(did,))[0]
        self.assertEqual(row['doc_date'],'2026-11-18')
        details=json.loads(self.sql("SELECT details_json FROM document_details WHERE kind='chiusure' AND doc_id=?",(did,))[0]['details_json'])
        self.assertEqual(details['printed_date'],'2026-11-19')
        self.assertIsNone(details.get('uncollected'))

    def test_historical_queue_includes_only_november_and_is_idempotent(self):
        expected=[]
        for kind,(table,col,_) in jobs.KINDS.items():
            for day in ['2026-10-31','2026-11-01','2026-11-30','2026-12-01','2025-11-10']:
                did=self.doc(kind);self.sql(f'UPDATE {table} SET {col}=? WHERE id=?',(day,did))
                if day.startswith('2026-11'):expected.append((kind,did))
        self.assertEqual(jobs.enqueue_history(),6)
        self.assertEqual(jobs.enqueue_history(),0)
        self.assertEqual(sorted((r['kind'],r['doc_id']) for r in self.sql('SELECT * FROM document_ai_jobs')),sorted(expected))

    def test_cross_store_and_other_uploader_cannot_preview(self):
        did=self.prepare('fatture');self.login(3)
        self.assertEqual(self.client.get(f'/schede/fatture/{did}').status_code,404)
        self.login()
        self.sql("UPDATE document_submissions SET username='someoneelse' WHERE doc_id=?",(did,))
        self.login(2)
        self.assertEqual(self.client.get(f'/schede/fatture/{did}/anteprima').status_code,403)
        self.assertEqual(self.client.get(f'/inventario/fatture-periodo/{did}?store=spinza').status_code,403)

    def test_nonconforming_preserves_original_and_requires_new_upload(self):
        did=self.prepare('spese-secondarie');self.login(2)
        page=self.client.get(f'/schede/spese-secondarie/{did}')
        token=re.search(r'name="csrf" value="([^"]+)"',page.text)[1]
        response=self.client.post(f'/schede/spese-secondarie/{did}/decisione',data=dict(csrf=token,revision=1,decision='reject'),follow_redirects=False)
        self.assertEqual(response.status_code,303)
        self.assertIn('rifai=1',response.headers['location'])
        self.assertEqual(self.sql('SELECT state FROM document_submissions')[0]['state'],'rejected')
        self.assertTrue(self.sql('SELECT data FROM secondary_expenses')[0]['data'])
        self.assertEqual(self.client.get(f'/schede/spese-secondarie/{did}/anteprima').status_code,403)
