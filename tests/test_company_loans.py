import re
import secrets
import unittest
import test_control as fixture
from app.cash_treasury import ensure_schema

class CompanyLoanPageTests(unittest.TestCase):
    login=fixture.ControlTests.login
    sql=fixture.ControlTests.sql
    def setUp(self):
        fixture.ControlTests.setUp(self)
        from app import db
        with db.connect() as c:
            ensure_schema(c.cursor())
            ensure_schema(c.cursor())

    def test_external_loan_saves_once_without_changing_cash_sources(self):
        self.sql("INSERT INTO cash_store_receipts VALUES('reburger_maggio','2026-11-01',20000,'admin')")
        page=self.client.get('/contanti-nei-negozi/bilanci')
        self.assertEqual(page.status_code,200)
        self.assertIn('Pagato con altri fondi',page.text)
        payload=dict(csrf=re.search(r'name="csrf" value="([^"]+)"',page.text)[1],submission=secrets.token_urlsafe(32),day='2026-11-02',lender='reburger',borrower='sc',amount='1.000,50',notes='Test',funding='external')
        for _ in range(2):
            r=self.client.post('/contanti-nei-negozi/bilanci/prestiti',data=payload,follow_redirects=False)
            self.assertEqual(r.status_code,303,r.text)
        rows=self.sql('SELECT * FROM cash_company_loans')
        self.assertEqual(len(rows),1)
        self.assertEqual((rows[0]['amount_cents'],rows[0]['funding']),(100050,'external'))
        self.assertEqual(len(self.sql('SELECT * FROM cash_store_receipts')),1)
        self.assertEqual(len(self.sql('SELECT * FROM cash_company_spending')),0)
        self.assertEqual(self.client.get('/contanti-nei-negozi/bilanci').status_code,200)
        payload.update(submission=secrets.token_urlsafe(32),funding='invalid')
        self.assertEqual(self.client.post('/contanti-nei-negozi/bilanci/prestiti',data=payload).status_code,422)
        self.assertEqual(len(self.sql('SELECT * FROM cash_company_loans')),1)
