import unittest
from app.document_ai import assess_readability

class ReadabilityTests(unittest.TestCase):
    def result(self):
        return dict(readable=False,quality_reason='Bordo e testo legale coperti',date='2026-11-05',supplier='Fornitore',amount=12.5,
                    warnings=[],essential_fields_readable=dict(issuer=True,date=True,total=True,items_or_payments=True))
    def test_clipped_secondary_text_does_not_require_another_photo(self):
        result=assess_readability(self.result())
        self.assertTrue(result['readable']);self.assertEqual(result['quality_reason'],'');self.assertTrue(result['warnings'])
    def test_unreadable_product_details_still_require_a_new_photo(self):
        result=self.result();result['readable']=True;result['essential_fields_readable']['items_or_payments']=False
        self.assertFalse(assess_readability(result)['readable'])
    def test_claimed_readability_cannot_replace_a_missing_total(self):
        result=self.result();result['amount']=None
        self.assertFalse(assess_readability(result)['readable'])
    def test_missing_optional_date_does_not_block(self):
        result=self.result();result.update(due_date=None,received_date=None)
        self.assertTrue(assess_readability(result)['readable'])
    def test_legacy_result_retains_its_assessment(self):
        self.assertFalse(assess_readability({'readable':False})['readable'])
