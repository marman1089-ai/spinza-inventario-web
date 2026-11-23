import unittest
from app.cash_treasury import company_cash_balances

def receipt(day,store,value):return dict(day=day,store=store,amount_cents=value)
def expense(day,sc=0,reburger=0):return dict(day=day,sc_cents=sc,reburger_cents=reburger)
def loan(day,lender,borrower,value):return dict(day=day,lender=lender,borrower=borrower,amount_cents=value,notes='')

class CompanyCashBalancesTests(unittest.TestCase):
    def calculate(self,receipts,expenses,loans=()):
        rows,_,moves=company_cash_balances(receipts,expenses,loans)
        self.assertEqual(rows[2]['balance'],sum(r['amount_cents'] for r in receipts)-sum(r['sc_cents']+r['reburger_cents'] for r in expenses))
        for row in rows[:2]:
            self.assertFalse(row['debt']>0 and row['balance']>0)
        return rows,moves
    def test_cash_accumulates_only_after_full_repayment_and_new_spending_borrows_again(self):
        receipts=[receipt('01','reburger_maggio',20000),receipt('02','spinza',4000)]
        expenses=[expense('01',sc=10000)]
        rows,_=self.calculate(receipts,expenses)
        self.assertEqual((rows[0]['balance'],rows[0]['debt']),(0,6000))
        receipts.append(receipt('03','spinza',8000))
        rows,_=self.calculate(receipts,expenses)
        self.assertEqual((rows[0]['balance'],rows[0]['debt']),(2000,0))
        expenses.append(expense('04',sc=5000))
        rows,_=self.calculate(receipts,expenses)
        self.assertEqual((rows[0]['balance'],rows[0]['debt']),(0,3000))
    def test_returned_cash_and_opposing_debt_do_not_leave_cash_with_unpaid_debt(self):
        rows,moves=self.calculate([receipt('01','reburger_maggio',20000)],[],[
            loan('01','reburger','sc',10000)])
        self.assertEqual((rows[0]['balance'],rows[0]['debt']),(0,0))
        self.assertTrue(any(m['kind']=='Rimborso automatico' for m in moves))
    def test_expenses_before_repayment_and_unfunded_shortfall_remains_visible(self):
        rows,_=self.calculate([receipt('01','reburger_maggio',10000),receipt('02','spinza',8000)],
                              [expense('01',sc=15000),expense('02',sc=3000)])
        self.assertEqual((rows[0]['balance'],rows[0]['debt']),(0,10000))
        rows,_=self.calculate([], [expense('01',sc=5000)])
        self.assertEqual(rows[0]['uncovered'],5000)
    def test_later_partner_income_covers_existing_shortfall(self):
        rows,_=self.calculate([receipt('02','reburger_maggio',5000)],[expense('01',sc=3000)])
        self.assertEqual((rows[0]['balance'],rows[0]['debt'],rows[0]['uncovered']),(0,3000,0))

    def test_opposing_loans_are_offset_without_changing_total_cash(self):
        rows,moves=self.calculate([receipt('01','reburger_maggio',20000)],[],[
            loan('01','reburger','sc',10000),loan('01','sc','reburger',3000)])
        self.assertEqual((rows[0]['balance'],rows[0]['debt'],rows[1]['credit']),(0,0,0))
        self.assertTrue(any(m['kind']=='Compensazione prestiti' and m['amount_cents']==3000 for m in moves))
