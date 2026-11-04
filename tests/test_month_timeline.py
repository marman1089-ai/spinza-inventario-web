import ast
import re
import sqlite3
import unittest
from pathlib import Path
from datetime import date, datetime
from urllib.parse import urlencode
from jinja2 import Environment, FileSystemLoader, select_autoescape

ROOT=Path(__file__).resolve().parents[1]
NAMES={'_cash_month_summaries','_valid_month_key','_month_keys_between','_cash_month_timeline','_month_label','_query_url','_month_options_for_scope'}
tree=ast.parse((ROOT/'app/main.py').read_text(encoding='utf-8'))
functions=[n for n in tree.body if isinstance(n,ast.FunctionDef) and n.name in NAMES]

class TimelineTests(unittest.TestCase):
    def setUp(self):
        self.db=sqlite3.connect(':memory:');self.db.row_factory=sqlite3.Row
        self.addCleanup(self.db.close)
        self.db.execute('CREATE TABLE sales_report_periods(store TEXT,month_key TEXT)')
        self.db.execute('CREATE TABLE cash_entries(store TEXT,flow_date TEXT,amount REAL)')
        self.db.execute('CREATE TABLE cash_expenses(store TEXT,flow_date TEXT,amount REAL,category TEXT,supplier TEXT,notes TEXT)')
        self.ns=dict(date=date,datetime=datetime,re=re,urlencode=urlencode,
            _rome_today=lambda:date(2026,11,4),_store_label=lambda s:s,_ph=lambda:'?',
            _parse_date_safe=lambda value,fallback:date.fromisoformat(value),
            _expense_family=lambda row:row['category'],
            _cash_scope_where=lambda store:('1=1',()) if store=='ALL' else ('store=?',(store,)),
            _dict_rows=lambda cur,sql,params:[dict(r) for r in cur.execute(sql,params).fetchall()])
        exec(compile(ast.Module(body=functions,type_ignores=[]),'timeline','exec'),self.ns)
    def income(self,month,amount=10,store='spinza'):
        self.db.execute('INSERT INTO cash_entries VALUES(?,?,?)',(store,month+'-01',amount))
    def timeline(self,start='',end='',store='spinza'):
        return self.ns['_cash_month_timeline'](self.db.cursor(),store,start,end)
    def test_nearest_current_and_future_ties(self):
        for month in ['2026-09','2026-10','2026-11','2026-12','2027-01']:self.income(month)
        result=self.timeline()
        self.assertEqual([r['key'] for r in result['rows']],['2026-11','2026-10','2026-12','2026-09','2027-01'])
        self.assertEqual([r['key'] for r in result['options']],[r['key'] for r in result['rows']])
        self.assertTrue(result['rows'][0]['is_current'])
        self.assertEqual(result['totals']['income_total'],50)
    def test_missing_current_and_empty_store(self):
        self.income('2026-09')
        result=self.timeline()
        self.assertEqual([r['key'] for r in result['rows']],['2026-11','2026-10','2026-09'])
        self.assertEqual(result['rows'][0]['income_total'],0)
        empty=self.timeline(store='other')
        self.assertEqual([r['key'] for r in empty['rows']],['2026-11'])
        self.assertEqual(empty['totals']['income_total'],0)
    def test_filters_invalid_months_and_scoping(self):
        self.income('2026-09',100);self.income('2026-10',200)
        self.income('2026-11',999,'other')
        self.db.execute("INSERT INTO cash_expenses VALUES('spinza','2026-10-20',30,'Spese','','')")
        filtered=self.timeline('2026-10','2026-09')
        self.assertTrue(filtered['range_swapped'])
        self.assertEqual([r['key'] for r in filtered['rows']],['2026-10','2026-09'])
        self.assertEqual(filtered['totals']['net_total'],270)
        self.assertEqual(filtered['totals']['months_count'],2)
        for key in ['2026-00','2026-13','0000-01','2026-1','bad']:
            result=self.timeline(key,key)
            self.assertEqual((result['selected_from'],result['selected_to']),('2026-09','2026-11'))
        self.assertEqual(self.timeline(store='ALL')['totals']['income_total'],1299)
        self.income('2026-13',800)
        self.assertEqual(self.timeline()['totals']['income_total'],300)
    def test_no_truncation_and_calendar_boundaries(self):
        keys=self.ns['_month_keys_between']('1970-01','2026-11')
        self.assertGreater(len(keys),600);self.assertEqual(keys[-1],'2026-11')
        self.assertEqual(self.ns['_month_keys_between']('9999-12','9999-12'),['9999-12'])
        self.income('1970-01')
        result=self.timeline()
        self.assertEqual(result['rows'][0]['key'],'2026-11')
        self.assertEqual(result['rows'][-1]['key'],'1970-01')
    def test_month_dropdown_keeps_recent_months_in_long_history(self):
        self.income('1970-01');self.income('2026-13')
        options=self.ns['_month_options_for_scope'](self.db.cursor(),'spinza')
        self.assertEqual(options[0]['key'],'2026-11')
        self.assertEqual(options[-1]['key'],'1970-01')
        self.assertGreater(len(options),600)
        self.assertNotIn('2026-13',[item['key'] for item in options])

    def test_render_order_and_current_badge(self):
        self.income('2026-09')
        env=Environment(loader=FileSystemLoader(ROOT/'app/templates'),autoescape=select_autoescape(['html']))
        html=env.get_template('gestionale_month_timeline.html').render(timeline=self.timeline(),user=None,brand='spinza',scope_label='Spinza')
        self.assertLess(html.index('data-month="2026-11"'),html.index('data-month="2026-09"'))
        self.assertIn('Mese corrente',html)
        self.assertIn('/gestionale?period_type=month',html)

if __name__=='__main__': unittest.main()
