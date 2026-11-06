import re
import unittest
from datetime import date
from unittest.mock import patch
import test_control as fixture
from app import main,db,cash_treasury as ct

class TreasuryTests(unittest.TestCase):
    login=fixture.ControlTests.login
    sql=fixture.ControlTests.sql
    def setUp(self):
        fixture.ControlTests.setUp(self)
        self.clock=patch.object(main,'_rome_today',return_value=date(2026,11,6));self.clock.start();self.addCleanup(self.clock.stop)
        self.login(store='ALL');self.counter=0
        page=self.client.get('/contanti-nei-negozi');self.assertEqual(page.status_code,200,page.text[:300])
        self.csrf=re.search(r'name="csrf" value="([^"]+)"',page.text).group(1)
    def send(self,path,data,token=None):
        self.counter+=1
        return self.client.post('/contanti-nei-negozi/'+path,data=dict(csrf=self.csrf,submission=token or str(self.counter).zfill(43),**data),follow_redirects=False)
    def open(self,store='spinza',value='1000',day='2026-11-01'):
        result=self.send('saldo-iniziale',dict(store=store,amount=value,day=day));self.assertEqual(result.status_code,303,result.text)
    def open_all(self,value='1000'):
        for store in ct.ORDER:self.open(store,value)
    def pay(self,total='100',day='2026-11-03',target='reburger',beneficiary='reburger_maggio',sources=None,token=None,expense_id='0'):
        sources=sources or dict(reburger_maggio='50',reburger_camaldoli='30',spinza='20')
        return self.send('paga',dict(name='Fornitura test',day=day,company=target,beneficiary_store=beneficiary,amount=total,expense_id=expense_id,**{'from_'+st:value for st,value in sources.items()}),token)
    def balances(self,day='2026-11-06'):
        with db.connect() as conn:
            op,stream,_,_,_=ct.events(conn.cursor());return ct.balances(op,stream,day)
    def loans(self):
        with db.connect() as conn:
            _,_,p,r,_=ct.events(conn.cursor());return ct.loan_stream(p,r)
    def test_split_payment_single_expense_and_automatic_loan(self):
        self.open_all()
        r=self.pay(token='a'*43);self.assertEqual(r.status_code,303,r.text)
        self.assertEqual(self.balances(),dict(reburger_maggio=95000,reburger_camaldoli=97000,spinza=98000))
        expense=self.sql('SELECT * FROM cash_expenses');self.assertEqual(len(expense),1)
        self.assertEqual(expense[0]['store'],'reburger_maggio');self.assertEqual(expense[0]['amount'],100)
        self.assertEqual([(r['lender'],r['borrower'],r['delta']) for r in self.loans()],[('sc','reburger',2000)])
        self.assertEqual(self.pay(token='a'*43).status_code,303)
        self.assertEqual(len(self.sql('SELECT * FROM cash_expenses')),1)
        self.assertEqual(self.client.post('/gestionale/uscite/'+str(expense[0]['id'])+'/delete').status_code,409)
    def test_company_reverse_and_same_company_has_no_loan(self):
        self.open_all()
        r=self.pay(target='sc',beneficiary='spinza',sources=dict(reburger_maggio='40',reburger_camaldoli='60'))
        self.assertEqual(r.status_code,303,r.text)
        self.assertEqual(sum(row['delta'] for row in self.loans()),10000)
        self.assertTrue(all(row['lender']=='reburger' and row['borrower']=='sc' for row in self.loans()))
        self.assertEqual(self.pay(sources=dict(reburger_camaldoli='100')).status_code,303)
        self.assertEqual(sum(row['delta'] for row in self.loans()),10000)
    def test_overdraft_backdate_and_validation_rollback(self):
        self.open_all('100')
        self.assertEqual(self.pay(total='90',day='2026-11-05',target='sc',beneficiary='spinza',sources=dict(spinza='90')).status_code,303)
        failed=self.pay(total='20',day='2026-11-03',target='sc',beneficiary='spinza',sources=dict(spinza='20'))
        self.assertEqual(failed.status_code,422,failed.text)
        self.assertEqual(len(self.sql('SELECT * FROM cash_treasury_payments')),1)
        self.assertEqual(len(self.sql('SELECT * FROM cash_expenses')),1)
        self.assertEqual(self.balances()['spinza'],1000)
        self.assertEqual(self.pay(day='2026-10-31').status_code,422)
        self.assertEqual(self.pay(day='2026-11-07').status_code,422)
        self.assertEqual(self.pay(total='99').status_code,422)
        self.assertEqual(self.pay(target='sc',beneficiary='reburger_maggio').status_code,422)
    def test_existing_expense_no_duplicate_cancel_restores_original(self):
        self.open_all()
        eid=self.sql("INSERT INTO cash_expenses(store,flow_date,category,amount,payment_method,notes,created_by) VALUES('reburger_maggio','2026-11-01','Forniture',100,'','Gia presente','testadmin')")
        result=self.pay(expense_id=str(eid));self.assertEqual(result.status_code,303,result.text)
        self.assertEqual(len(self.sql('SELECT * FROM cash_expenses')),1)
        self.assertEqual(str(self.sql('SELECT flow_date FROM cash_expenses')[0]['flow_date']),'2026-11-01')
        pid=self.sql('SELECT id FROM cash_treasury_payments')[0]['id']
        self.assertEqual(self.send('annulla-pagamento',dict(payment_id=pid,reason='Collegamento errato')).status_code,303)
        original=self.sql('SELECT * FROM cash_expenses')[0]
        self.assertEqual(original['payment_method'],'');self.assertEqual(original['notes'],'Gia presente')
        self.assertEqual(self.balances(),{st:100000 for st in ct.ORDER})
    def test_repayment_moves_cash_reduces_debt_not_business_expense(self):
        self.open_all();self.assertEqual(self.pay().status_code,303)
        self.assertEqual(self.send('rimborso',dict(day='2026-11-04',source_store='reburger_camaldoli',target_store='spinza',amount='21')).status_code,422)
        self.assertEqual(self.send('rimborso',dict(day='2026-11-04',source_store='reburger_camaldoli',target_store='spinza',amount='10')).status_code,303)
        self.assertEqual(self.balances(),dict(reburger_maggio=95000,reburger_camaldoli=96000,spinza=99000))
        self.assertEqual(sum(row['delta'] for row in self.loans()),1000)
        self.assertEqual(len(self.sql('SELECT * FROM cash_expenses')),1)
        pid=self.sql('SELECT id FROM cash_treasury_payments')[0]['id']
        self.assertEqual(self.send('annulla-pagamento',dict(payment_id=pid,reason='Errore')).status_code,422)
        rid=self.sql('SELECT id FROM cash_treasury_repayments')[0]['id']
        self.assertEqual(self.send('annulla-rimborso',dict(repayment_id=rid,reason='Rimborso mai avvenuto')).status_code,303)
        self.assertEqual(self.send('annulla-pagamento',dict(payment_id=pid,reason='Pagamento mai avvenuto')).status_code,303)
        self.assertEqual(self.balances(),{st:100000 for st in ct.ORDER})
        self.assertEqual(len(self.sql('SELECT * FROM cash_expenses')),0)
    def test_only_cash_and_known_expenses_and_no_daily_float_duplication(self):
        self.open()
        for method,day,value in [('Contanti','2026-11-02',100),('POS','2026-11-02',400),('Cash','2026-10-31',999)]:
            self.sql('INSERT INTO cash_entries(store,flow_date,payment_method,amount,created_by) VALUES(?,?,?,?,?)',('spinza',day,method,value,'testadmin'))
        for category,method,notes,value in [('Spese','Contanti','Contanti spesi',30),('Spese chiusura','','Report chiusura #123 - Spesa',20),('Forniture','','Pagamento incerto',50)]:
            self.sql('INSERT INTO cash_expenses(store,flow_date,category,payment_method,notes,amount,created_by) VALUES(?,?,?,?,?,?,?)',('spinza','2026-11-02',category,method,notes,value,'testadmin'))
        self.assertEqual(self.balances()['spinza'],105000)
        page=self.client.get('/contanti-nei-negozi').text;self.assertIn('Pagamento incerto',page)
        self.assertIn('metodo di pagamento non indicato',page)
    def test_count_adjustment_keeps_audit_not_new_expense(self):
        self.open()
        r=self.send('rettifica',dict(store='spinza',day='2026-11-02',amount='990',reason='Ammanco verificato'))
        self.assertEqual(r.status_code,303,r.text);self.assertEqual(self.balances()['spinza'],99000)
        self.assertEqual(len(self.sql('SELECT * FROM cash_expenses')),0)
        self.assertEqual(self.sql('SELECT delta_cents FROM cash_treasury_adjustments')[0]['delta_cents'],-1000)
        r=self.send('saldo-iniziale',dict(store='spinza',day='2026-11-01',amount='1200'))
        self.assertEqual(r.status_code,409)
    def test_concurrent_payments_cannot_spend_same_cash_twice(self):
        from concurrent.futures import ThreadPoolExecutor
        self.open_all('100')
        with ThreadPoolExecutor(max_workers=2) as pool:
            futures=[pool.submit(self.pay,total='80',target='sc',beneficiary='spinza',sources=dict(spinza='80'),token=letter*43) for letter in ['p','q']]
            results=[future.result() for future in futures]
        self.assertEqual(sorted(response.status_code for response in results),[303,422])
        self.assertEqual(self.balances()['spinza'],2000)
        self.assertEqual(len(self.sql('SELECT * FROM cash_expenses')),1)

    def test_historical_projection_and_loan_date_and_cancelled_replay(self):
        self.open_all()
        self.assertEqual(self.pay(token='z'*43).status_code,303)
        self.assertEqual(self.balances('2026-11-02')['spinza'],100000)
        self.assertEqual(self.balances('2026-11-03')['spinza'],98000)
        early=self.send('rimborso',dict(day='2026-11-02',source_store='reburger_camaldoli',target_store='spinza',amount='10'))
        self.assertEqual(early.status_code,422)
        pid=self.sql('SELECT id FROM cash_treasury_payments')[0]['id']
        self.assertEqual(self.send('annulla-pagamento',dict(payment_id=pid,reason='Registrazione di prova')).status_code,303)
        self.assertEqual(self.pay(token='z'*43).status_code,409)
        self.assertEqual(self.balances()['spinza'],100000)

    def test_permissions_csrf_unconfigured_and_layout(self):
        self.assertEqual(self.pay().status_code,422)
        self.assertEqual(self.client.post('/contanti-nei-negozi/paga',data={'csrf':'wrong'}).status_code,403)
        html=self.client.get('/contanti-nei-negozi').text
        self.assertLess(html.index('<h2>Piazza San Felice'),html.index('<h2>Via Camaldoli'))
        self.assertLess(html.index('<h2>Via Camaldoli'),html.index('<h2>Spinza'))
        self.login(store='spinza');html=self.client.get('/contanti-nei-negozi').text
        self.assertEqual(html.count('class="card ct-balance"'),1)
        self.login(2)
        for path in ['','/paga','/saldo-iniziale','/rimborso','/rettifica','/annulla-pagamento','/annulla-rimborso']:
            response=self.client.get('/contanti-nei-negozi') if not path else self.client.post('/contanti-nei-negozi'+path,data={})
            self.assertEqual(response.status_code,403)

if __name__=='__main__':unittest.main()
