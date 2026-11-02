import asyncio, os, sys, tempfile, unittest
from datetime import timezone
from pathlib import Path
from unittest.mock import patch
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from fastapi import FastAPI, HTTPException
from starlette.datastructures import FormData
from jinja2 import Environment, FileSystemLoader, select_autoescape
from app import closure_reports as cr, db
def run(coro):
    try: coro.send(None)
    except StopIteration as result: return result.value
    raise AssertionError('Unexpected external await')
class Request:
    def __init__(self,session,data=()): self.session=session; self.data=FormData(data)
    async def form(self): return self.data
class ReportsTest(unittest.TestCase):
    def test_reports(self):
        with patch.object(cr,'ZoneInfo',lambda name:timezone.utc), tempfile.TemporaryDirectory(dir=Path(__file__).resolve().parent) as folder, patch.dict(os.environ,{'DATABASE_URL':'','RENDER':''}), patch.object(db,'SQLITE_PATH',Path(folder)/'test.db'):
            app=FastAPI(); user={'role':'admin','username':'test','store':'spinza'}; scope=['spinza']
            env=Environment(loader=FileSystemLoader(Path(__file__).resolve().parents[1]/'app/templates'),autoescape=select_autoescape(['html']))
            def render(name,**ctx):
                ctx.update(header_store=ctx['selected'],active_store=ctx['selected'])
                return env.get_template(name).render(**ctx)
            def insert(cur,store,day,method,amount,notes,user):
                cur.execute('INSERT INTO cash_entries(store,flow_date,payment_method,amount,notes,created_by) VALUES(?,?,?,?,?,?)',(store,day,method,amount,notes,user))
            cr.install(app,{'_insert_cash_entry_compat':insert,'_ensure_payment_method':lambda *args:None,'_log':lambda *args,**kw:None,'_ph':lambda:'?','STORES':{'spinza':'Spinza','other':'Altro'},'require_login':lambda r:user,'_effective_store':lambda r,u:scope[0],'render':render})
            cr.init_reports(); cr.init_reports()
            with db.connect() as c:
                c.cursor().execute('CREATE TABLE cash_entries (store TEXT,flow_date TEXT,payment_method TEXT,amount REAL,notes TEXT,created_by TEXT)')
            routes={r.path:r.endpoint for r in app.routes if r.path.startswith('/report-chiusura')}
            session={}; page=routes['/report-chiusura'](Request(session)); self.assertIn('Report chiusura',page)
            csrf=session['closure_reports_csrf']
            def send(path,data): return run(routes[path](Request(session,[('csrf',csrf),('store','spinza')]+data)))
            self.assertEqual(send('/report-chiusura/canali',[('name','Glovo')]).status_code,303)
            send('/report-chiusura/canali',[('name','gLoVo')])
            data=[('closure_date','2026-11-02'),('submission','a'*43),('notes','<script>test</script>'),('channel','POS'),('amount','12,30'),('channel','Cash'),('amount','0.20'),('channel','Glovo'),('amount','4')]
            self.assertEqual(send('/report-chiusura/salva',data).status_code,303)
            send('/report-chiusura/salva',data)
            with db.connect() as c:
                rows=c.cursor().execute('SELECT * FROM closure_reports').fetchall();self.assertEqual(len(rows),1);self.assertEqual(rows[0]['total_cents'],1650)
                income=c.cursor().execute('SELECT * FROM cash_entries').fetchall();self.assertEqual(len(income),3);self.assertAlmostEqual(sum(r['amount'] for r in income),16.5);self.assertEqual(income[1]['payment_method'],'Contanti');self.assertTrue(all(r['store']=='spinza' for r in income))
                self.assertEqual(len(c.cursor().execute('SELECT * FROM closure_report_channels').fetchall()),1)
            page=routes['/report-chiusura'](Request(session));self.assertIn('16,50',page);self.assertIn('&lt;script&gt;',page)
            cal=routes['/report-chiusura'](Request(session),month='2026-11',day='2026-11-02')
            self.assertIn('data-date="2026-11-02" data-count="1"',cal)
            self.assertIn('Chiusure del 02/11/2026',cal)
            self.assertNotIn('Incassi nel periodo',cal)
            self.assertIn('value="2026-11-02"',cal)
            missing=routes['/report-chiusura'](Request(session),month='2026-11',day='2026-11-03')
            self.assertIn('Nessuna chiusura inserita per questo giorno',missing)
            self.assertIn('data-date="2026-11-02" data-count="1"',missing)
            self.assertNotIn('Report #1',missing)
            leap=routes['/report-chiusura'](Request(session),month='2028-02')
            self.assertIn('data-date="2028-02-29"',leap)
            december=routes['/report-chiusura'](Request(session),month='2026-12')
            self.assertIn('month=2027-01',december)
            for month in ('bad','2026-13','2026-1'):
                with self.assertRaises(HTTPException):routes['/report-chiusura'](Request(session),month=month)
            scope[0]='other'
            other_cal=routes['/report-chiusura'](Request(session),month='2026-11')
            self.assertIn('data-date="2026-11-02" data-count="0"',other_cal)
            scope[0]='spinza'
            for bad in ['-1','NaN','Infinity','1.999','1.000,00','']:
                with self.assertRaises(HTTPException):cr.cents(bad)
            with self.assertRaises(HTTPException):send('/report-chiusura/salva',[(k,'bad' if k=='closure_date' else v) for k,v in data])
            app.router.on_startup[-1](); app.router.on_startup[-1]()
            with db.connect() as c: self.assertEqual(len(c.cursor().execute('SELECT * FROM cash_entries').fetchall()),3)
            scope[0]='other'
            with self.assertRaises(HTTPException):send('/report-chiusura/salva',data)
            self.assertNotIn('16,50',routes['/report-chiusura'](Request(session)))
            scope[0]='spinza'
            with self.assertRaises(HTTPException): run(routes['/report-chiusura/salva'](Request(session,[('csrf','wrong')])))
            # Deleting a custom channel preserves historical reports and finance.
            self.assertEqual(send('/report-chiusura/canali/elimina',[('name','Glovo')]).status_code,303)
            with db.connect() as c:
                self.assertEqual(c.cursor().execute('SELECT COUNT(*) AS n FROM closure_report_channels').fetchone()['n'],0)
                self.assertEqual(c.cursor().execute('SELECT COUNT(*) AS n FROM cash_entries').fetchone()['n'],3)
                self.assertIn('Glovo',c.cursor().execute('SELECT lines_json FROM closure_reports').fetchone()['lines_json'])
            for name in ('POS','Cash','Contanti'):
                with self.assertRaises(HTTPException):send('/report-chiusura/canali/elimina',[('name',name)])
            scope[0]='other'
            with self.assertRaises(HTTPException):send('/report-chiusura/canali/elimina',[('name','Glovo')])
            scope[0]='spinza'
            with self.assertRaises(HTTPException):run(routes['/report-chiusura/canali/elimina'](Request(session,[('csrf','bad'),('store','spinza'),('name','Glovo')])))
            send('/report-chiusura/canali',[('name','Glovo')])
            self.assertIn('Glovo',routes['/report-chiusura'](Request(session)))
            user.clear()
            with self.assertRaises(HTTPException):routes['/report-chiusura'](Request(session))
if __name__=='__main__': unittest.main()


