import json,re,unittest
from datetime import date
from unittest.mock import patch
import test_control as fixture
from app.cash_collection import collection_windows,CollectionIndex
from app.cash_treasury import ensure_schema
from app.reconciliation import comparisons


def report(day,value,store='spinza',expenses=()):
    return dict(store=store,closure_date=day,created_by='dipendente',lines_json=json.dumps([dict(name='Cash',cents=value),dict(name='POS',cents=5000)]),expenses_json=json.dumps(list(expenses)))
def receipt(day,value,store='spinza'):
    return dict(store=store,day=day,amount_cents=value,counted_by='admin')

class CashCollectionTests(unittest.TestCase):
    def test_skipped_counts_accumulate_net_cash_and_keep_previous_day_rule(self):
        reports=[report('2026-11-19',10000),report('2026-11-20',15000),report('2026-11-21',20000,expenses=[dict(cents=1000,mode='included'),dict(cents=500,mode='cash_fund')])]
        counts=[receipt('2026-11-20',10000),receipt('2026-11-22',34000)]
        windows=collection_windows(reports,counts)
        row=windows['spinza','2026-11-22']
        self.assertEqual((row['report_start'],row['report_end']),('2026-11-20','2026-11-21'))
        self.assertEqual((row['declared'],row['difference'],row['report_days']),(34000,0,2))
        self.assertEqual(sum(r['declared'] for r in windows.values()),44000)
        self.assertEqual(CollectionIndex(windows).for_report('spinza','2026-11-20')['day'],'2026-11-22')
    def test_month_boundary_store_independence_and_real_zero(self):
        reports=[report('2026-10-30',1000),report('2026-10-31',2000),report('2026-11-01',3000),report('2026-10-31',4000,'reburger_maggio'),report('2026-11-01',5000,'reburger_maggio')]
        counts=[receipt('2026-10-31',1000),receipt('2026-11-01',0),receipt('2026-11-02',3000),receipt('2026-11-02',9000,'reburger_maggio')]
        w=collection_windows(reports,counts)
        self.assertEqual(w['spinza','2026-11-01']['difference'],-2000)
        self.assertEqual(w['spinza','2026-11-02']['declared'],3000)
        self.assertEqual(w['reburger_maggio','2026-11-02']['declared'],9000)
        self.assertEqual(w['reburger_maggio','2026-11-02']['report_start'],'2026-10-31')
    def test_missing_reports_are_not_invented_as_zero_and_multiple_shifts_sum(self):
        reports=[report('2026-11-20',1000),report('2026-11-20',2000),report('2026-11-22',4000)]
        w=collection_windows(reports,[receipt('2026-11-23',7000)])['spinza','2026-11-23']
        self.assertEqual(w['declared'],7000)
        self.assertEqual(w['missing_days'],1)
        self.assertIsNone(w['difference'])
        empty=collection_windows([], [receipt('2026-11-23',0)])['spinza','2026-11-23']
        self.assertIsNone(empty['declared'])
    def test_backdated_insertion_or_deletion_repartitions_expectation_not_actual_cash(self):
        reports=[report('2026-11-20',1000),report('2026-11-21',2000)]
        counts=[receipt('2026-11-22',3000)]
        self.assertEqual(collection_windows(reports,counts)['spinza','2026-11-22']['declared'],3000)
        split=collection_windows(reports,[receipt('2026-11-21',1000),*counts])
        self.assertEqual(split['spinza','2026-11-22']['declared'],2000)
        self.assertEqual(split['spinza','2026-11-22']['amount_cents'],3000)
        self.assertEqual(split['spinza','2026-11-22']['difference'],1000)
    def test_reconciliation_reuses_the_same_cumulative_cash_window(self):
        reports=[report('2026-11-20',1000),report('2026-11-21',2000)]
        rows=comparisons(reports,[],[receipt('2026-11-22',3000)])
        for r in rows:
            self.assertEqual(r['expected'],3000)
            self.assertEqual(r['cash_difference'],0)
            self.assertEqual(r['collection_day'],'2026-11-22')

class CashCollectionPageTests(unittest.TestCase):
    login=fixture.ControlTests.login
    sql=fixture.ControlTests.sql
    def setUp(self):
        fixture.ControlTests.setUp(self)
        from app import db
        with db.connect() as c:ensure_schema(c.cursor())
    def add_report(self,day,value,store='spinza'):
        r=report(day,value,store)
        return self.sql('INSERT INTO closure_reports(store,closure_date,lines_json,total_cents,notes,created_by,created_at,submission_token) VALUES(?,?,?,?,?,?,?,?)',(store,day,r['lines_json'],value+5000,'','testadmin',day,store+day))
    def add_count(self,day,value,store='spinza'):
        self.sql('INSERT INTO cash_store_receipts(store,day,amount_cents,counted_by) VALUES(?,?,?,?)',(store,day,value,'testadmin'))
    def test_preview_and_history_cross_month_no_virtual_receipts_saved(self):
        self.add_report('2026-10-30',1000);self.add_report('2026-10-31',2000);self.add_report('2026-11-01',3000)
        self.add_count('2026-10-31',1000)
        page=self.client.get('/contanti-nei-negozi/incassi?day=2026-11-02')
        self.assertEqual(page.status_code,200)
        self.assertIn('50,00 €',page.text)
        self.assertIn('Report dal 2026-10-31 al 2026-11-01',page.text)
        self.assertEqual(len(self.sql('SELECT * FROM cash_store_receipts')),1)
        csrf=re.search(r'name="csrf" value="([^"]+)"',page.text)[1]
        response=self.client.post('/contanti-nei-negozi/incassi',data={'csrf':csrf,'day':'2026-11-02','spinza':'50','reburger_maggio':''},follow_redirects=False)
        self.assertEqual(response.status_code,303)
        page=self.client.get(response.headers['location'])
        self.assertIn('Conforme',page.text)
        self.assertEqual(len(self.sql('SELECT * FROM cash_store_receipts')),2)
    def test_filtered_comparison_can_find_a_later_count_covering_the_report(self):
        self.add_report('2026-11-20',1000);self.add_report('2026-11-21',2000);self.add_count('2026-11-22',3000)
        page=self.client.get('/gestionale/controllo-dati?start=2026-11-20&end=2026-11-20')
        self.assertEqual(page.status_code,200)
        self.assertIn('Conteggio 2026-11-22',page.text)
        self.assertIn('report dal 2026-11-20 al 2026-11-21',page.text)
        self.assertIn('Apri il totale del conteggio',page.text)
