"""Store switching preserves navigation without retaining old store identifiers."""
import ast
import unittest
from pathlib import Path
from urllib.parse import urlsplit, parse_qsl, urlencode, quote
from jinja2 import Environment

ROOT=Path(__file__).resolve().parents[1]
source=(ROOT/'app/main.py').read_text(encoding='utf-8')
module=ast.parse(source)
helper=next(n for n in module.body if isinstance(n,ast.FunctionDef) and n.name=='_store_switch_url')
namespace=dict(urlsplit=urlsplit,parse_qsl=parse_qsl,urlencode=urlencode,quote=quote)
exec(compile(ast.Module(body=[helper],type_ignores=[]),'store_switch','exec'),namespace)
switch=namespace['_store_switch_url']

class StoreSwitchTests(unittest.TestCase):
    def test_finance_pages_preserve_period(self):
        for path in ['/gestionale/incassi','/gestionale/uscite','/gestionale/storico-mensile','/gestionale/report-vendite','/report-chiusura','/admin/cassa/report']:
            target=urlsplit(switch(path+'?month=2026-11&month_key=2026-11&period_type=month&anchor_date=2026-11-01&start=2026-11-01&end=2026-11-30&store=spinza&saved=true&id=20#closing-history','reburger_maggio'))
            self.assertEqual(target.path,path)
            params=dict(parse_qsl(target.query))
            for key in ['month','month_key','period_type','anchor_date','start','end']: self.assertIn(key,params)
            self.assertNotIn('id',params);self.assertNotIn('saved',params)
            self.assertNotIn('spinza',target.query)
            self.assertEqual(target.fragment,'closing-history')

    def test_sections_and_filters(self):
        for path in ['/ordini','/ordinati','/storico-ordini','/fatture','/logs/documenti','/admin/controllo','/file-archiviati','/scambi','/inventario-controllo']:
            self.assertEqual(urlsplit(switch(path,'reburger_maggio')).path,path)
        target=urlsplit(switch('/inventario?area=bibite&cat=Birre&loc=SALA&only_low=1&store=spinza','reburger_maggio'))
        self.assertEqual(dict(parse_qsl(target.query)),dict(area='bibite',cat='Birre',loc='SALA',only_low='1',store='reburger_maggio'))
        self.assertEqual(dict(parse_qsl(urlsplit(switch('/inventario/report?store=spinza&id=8&start=2026-11-01','reburger_maggio')).query)),dict(start='2026-11-01',store='reburger_maggio'))

    def test_redirect_safety(self):
        for url in ['https://evil.test/gestionale','//evil.test/gestionale','/logout','/set-active-store','/unknown','/inventario/report/csv','/fatture/123','http://[bad']:
            self.assertEqual(switch(url,'spinza'),'/inventario')

    def test_endpoint_and_header(self):
        endpoint=next(n for n in module.body if isinstance(n,ast.FunctionDef) and n.name=='set_active_store')
        self.assertIn('_store_switch_url',ast.unparse(endpoint))
        base=(ROOT/'app/templates/base.html').read_text(encoding='utf-8')
        Environment().parse(base)
        self.assertIn('location.pathname+location.search+location.hash',base)

if __name__=='__main__': unittest.main()
