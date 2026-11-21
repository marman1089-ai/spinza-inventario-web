import io,json,os,unittest
from unittest.mock import patch
import test_control  # isolated app setup / import path
from app.document_ai import extract_document

class CompactExtractionTests(unittest.TestCase):
    def read(self,kind,lines=None):
        data=dict(date='2026-11-21',supplier='Test',amount=12,currency='EUR',readable=True,
            essential_fields_readable=dict(issuer=True,date=True,total=True,items_or_payments=True),warnings=[],quality_reason='')
        if lines is not None:data['lines']=lines
        response={'status':'completed','output':[{'content':[{'type':'output_text','text':json.dumps(data)}]}]}
        with patch.dict(os.environ,{'OPENAI_API_KEY':'test-only','DOCUMENT_AI_ENABLED':'new'}),patch('app.document_ai.urllib.request.urlopen',return_value=io.BytesIO(json.dumps(response).encode())) as call:
            result=extract_document(b'%PDF test',kind,new_upload=True)
            self.assertEqual(call.call_count,1)
            payload=json.loads(call.call_args.args[0].data)
        return result,payload
    def test_closure_does_not_request_transcript_or_duplicate_payment_lines(self):
        result,payload=self.read('Chiusure')
        properties=payload['text']['format']['schema']['properties']
        for key in ('text','description','lines','due_date','readable','essential_fields_readable','quality_reason'):self.assertNotIn(key,properties)
        self.assertIn('cash',properties);self.assertIn('card',properties)
        self.assertEqual(result['lines'],[]);self.assertTrue(result['text'])
    def test_invoice_keeps_every_product_without_requesting_narrative(self):
        rows=[dict(description='Prodotto A',qty=1,unit='kg',amount=5),dict(description='Prodotto B',qty=2,unit='pz',amount=7)]
        result,payload=self.read('Fatture',rows)
        self.assertEqual(result['lines'],rows)
        fields=payload['text']['format']['schema']['properties']
        self.assertIn('due_date',fields);self.assertNotIn('text',fields);self.assertNotIn('cash_tendered',fields)
    def test_expense_keeps_change_and_omits_unneeded_invoice_fields(self):
        _,payload=self.read('Spese secondarie',[])
        fields=payload['text']['format']['schema']['properties']
        self.assertIn('cash_tendered',fields);self.assertIn('change',fields);self.assertNotIn('due_date',fields)
