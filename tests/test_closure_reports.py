import asyncio, os, sys, tempfile, unittest
from datetime import timezone, datetime
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
            def insert_expense(cur,store,day,category,supplier,method,amount,notes,user):
                cur.execute('INSERT INTO cash_expenses(store,flow_date,amount,notes) VALUES(?,?,?,?)',(store,day,amount,notes))
            cr.install(app,{'_insert_cash_expense_compat':insert_expense,'_insert_cash_entry_compat':insert,'_ensure_payment_method':lambda *args:None,'_log':lambda *args,**kw:None,'_ph':lambda:'?','STORES':{'spinza':'Spinza','other':'Altro'},'require_login':lambda r:user,'_effective_store':lambda r,u:scope[0],'render':render})
            cr.init_reports(); cr.init_reports()
            with db.connect() as c:
                c.cursor().execute('CREATE TABLE cash_expenses (store TEXT,flow_date TEXT,amount REAL,notes TEXT)')
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
            spending=[(k,'b'*43 if k=='submission' else v) for k,v in data]+[('expense_name','Ghiaccio'),('expense_amount','2,30'),('expense_name','Pane'),('expense_amount','1.20')]
            self.assertEqual(send('/report-chiusura/salva',spending).status_code,303)
            send('/report-chiusura/salva',spending)
            app.router.on_startup[-1]()
            with db.connect() as c:
                expense_rows=c.cursor().execute('SELECT * FROM cash_expenses').fetchall()
                self.assertEqual(len(expense_rows),2)
                self.assertAlmostEqual(sum(e['amount'] for e in expense_rows),3.5)
                self.assertTrue(all(e['store']=='spinza' for e in expense_rows))
                self.assertIn('Ghiaccio',expense_rows[0]['notes'])
                report=c.cursor().execute('SELECT * FROM closure_reports WHERE submission_token=?',('b'*43,)).fetchone()
                self.assertEqual(report['total_cents'],2000)
                income=c.cursor().execute('SELECT * FROM cash_entries').fetchall()
                self.assertEqual(len(income),6)
                self.assertAlmostEqual(sum(r['amount'] for r in income),36.5)
                cash=[r['amount'] for r in income if r['payment_method']=='Contanti']
                self.assertEqual(cash,[0.2,3.7])
            page=routes['/report-chiusura'](Request(session))
            self.assertIn('Ghiaccio',page);self.assertIn('20,00',page);self.assertIn('16,50',page)
            for name,amount in [('', '2'),('Pane',''),('Pane','-2'),('Pane','0'),('Pane','1.234')]:
                invalid=[(k,'c'*43 if k=='submission' else v) for k,v in data]+[('expense_name',name),('expense_amount',amount)]
                with self.assertRaises(HTTPException):send('/report-chiusura/salva',invalid)
            with db.connect() as c:
                self.assertEqual(c.cursor().execute('SELECT COUNT(*) AS n FROM closure_reports').fetchone()['n'],2)
                self.assertEqual(c.cursor().execute('SELECT COUNT(*) AS n FROM cash_expenses').fetchone()['n'],2)
            # Deletion permissions are enforced by the server as well as the UI.
            spending_id=report['id']
            delete_path='/report-chiusura/elimina'
            def delete(report_id): return send(delete_path,[('report_id',str(report_id))])
            with self.assertRaises(HTTPException) as error:
                run(routes[delete_path](Request(session,[('csrf','wrong'),('store','spinza'),('report_id',str(spending_id))])))
            self.assertEqual(error.exception.status_code,403)
            scope[0]='other'
            with self.assertRaises(HTTPException): delete(spending_id)
            scope[0]='spinza'; user['role']='staff'
            with patch.object(cr,'datetime') as clock:
                clock.now.return_value=datetime.fromisoformat('2026-11-03T00:00:00+01:00')
                with self.assertRaises(HTTPException) as error: delete(spending_id)
                self.assertEqual(error.exception.status_code,403)
                page=routes['/report-chiusura'](Request(session))
                self.assertNotIn('class="closing-report-delete"',page)
                self.assertIn('solo un admin',page)
                with db.connect() as c:
                    self.assertEqual(c.cursor().execute('SELECT COUNT(*) AS n FROM cash_entries').fetchone()['n'],6)
                # The rule follows the closure date even for reports entered later.
                clock.now.return_value=datetime.fromisoformat('2026-11-02T23:59:59+01:00')
                page=routes['/report-chiusura'](Request(session))
                self.assertIn(f'Elimina report #{spending_id}',page)
                # Other references and stores must survive the deletion.
                with db.connect() as c:
                    c.cursor().execute('INSERT INTO cash_entries VALUES(?,?,?,?,?,?)',('spinza','2026-11-02','POS',99,f'Report chiusura #{spending_id}0','test'))
                    c.cursor().execute('INSERT INTO cash_expenses VALUES(?,?,?,?)',('other','2026-11-02',99,f'Report chiusura #{spending_id} - Ghiaccio'))
                self.assertEqual(delete(spending_id).status_code,303)
                with db.connect() as c:
                    self.assertEqual(c.cursor().execute('SELECT COUNT(*) AS n FROM closure_reports').fetchone()['n'],1)
                    self.assertEqual(c.cursor().execute('SELECT COUNT(*) AS n FROM cash_entries').fetchone()['n'],4)
                    self.assertEqual(c.cursor().execute('SELECT COUNT(*) AS n FROM cash_expenses').fetchone()['n'],1)
                    self.assertEqual(c.cursor().execute('SELECT COUNT(*) AS n FROM closure_report_expenses WHERE report_id=?',(spending_id,)).fetchone()['n'],0)
                    self.assertEqual(c.cursor().execute('SELECT COUNT(*) AS n FROM closure_report_exports WHERE report_id=?',(spending_id,)).fetchone()['n'],0)
                with self.assertRaises(HTTPException) as error: delete(spending_id)
                self.assertEqual(error.exception.status_code,404)
                with self.assertRaises(HTTPException) as error: send('/report-chiusura/salva',spending)
                self.assertEqual(error.exception.status_code,409)
                clock.now.return_value=datetime.fromisoformat('2026-11-01T23:59:59+01:00')
                with self.assertRaises(HTTPException): delete(1)
                clock.now.return_value=datetime.fromisoformat('2026-12-02T12:00:00+01:00')
                user['role']='admin'
                self.assertIn('Elimina report #1',routes['/report-chiusura'](Request(session)))
                self.assertEqual(delete(1).status_code,303)
            app.router.on_startup[-1]()
            with db.connect() as c:
                self.assertEqual(c.cursor().execute('SELECT COUNT(*) AS n FROM closure_reports').fetchone()['n'],0)
                self.assertEqual(c.cursor().execute('SELECT COUNT(*) AS n FROM cash_entries').fetchone()['n'],1)
            page=routes['/report-chiusura'](Request(session),month='2026-11',deleted=True)
            self.assertIn('data-date="2026-11-02" data-count="0"',page)
            self.assertIn('Report eliminato',page)
            # Each expense can use a different source, without counting the float as revenue.
            base=[('closure_date','2026-11-02'),('channel','POS'),('amount','50'),('channel','Cash'),('amount','100')]
            cases=[
                ('l', [('cash_fund_initial','50')],0,0,5000),
                ('i', [('cash_fund_initial','100,50')],15000,15000,10050),
                ('j', [('cash_fund_initial','0')],15000,15000,0),
                ('k', [('cash_fund_initial','150'),('expense_name',''),('expense_amount',''),('expense_mode','add_to_cash')],15000,15000,15000),
                ('d', [('expense_name','Ghiaccio'),('expense_amount','20'),('expense_mode','add_to_cash')],17000,15000,None),
                ('e', [('expense_name','Ghiaccio'),('expense_amount','20'),('expense_mode','included')],15000,13000,None),
                ('f', [('expense_name','Ghiaccio'),('expense_amount','20'),('expense_mode','cash_fund'),('cash_fund_initial','100')],15000,15000,8000),
                ('g', [('expense_name','A'),('expense_amount','20'),('expense_mode','add_to_cash'),('expense_name','B'),('expense_amount','10'),('expense_mode','included'),('expense_name','C'),('expense_amount','30'),('expense_mode','cash_fund'),('cash_fund_initial','100')],17000,14000,7000),
                ('h', [('expense_name','Ghiaccio'),('expense_amount','20'),('expense_mode','cash_fund'),('cash_fund_initial','20')],15000,15000,0),
            ]
            for letter,extra,total,net,remaining in cases:
                selected_base=[(k,'0' if k=='amount' else v) for k,v in base] if letter=='l' else base
                payload=selected_base+[('submission',letter*43)]+extra
                self.assertEqual(send('/report-chiusura/salva',payload).status_code,303)
                send('/report-chiusura/salva',payload)
                # Inspect persisted values and the rendered totals.
                with db.connect() as c:
                    row=c.cursor().execute('SELECT * FROM closure_reports WHERE submission_token=?',(letter*43,)).fetchone()
                    self.assertEqual(row['total_cents'],total)
                    import json
                    expense=c.cursor().execute('SELECT lines_json FROM closure_report_expenses WHERE report_id=?',(row['id'],)).fetchone()
                    expenses=json.loads(expense['lines_json'])
                    self.assertEqual(total-sum(e['cents'] for e in expenses if e['mode']!='cash_fund'),net)
                    fund=c.cursor().execute('SELECT initial_cents FROM closure_report_cash_funds WHERE report_id=?',(row['id'],)).fetchone()
                    self.assertEqual(fund['initial_cents']-sum(e['cents'] for e in expenses if e['mode']=='cash_fund') if fund else None,remaining)
                    income=c.cursor().execute('SELECT SUM(amount) AS total FROM cash_entries WHERE notes=?',(f"Report chiusura #{row['id']}",)).fetchone()
                    self.assertAlmostEqual(income['total'] or 0,total/100)
                    self.assertEqual(c.cursor().execute('SELECT COUNT(*) AS n FROM cash_expenses WHERE store=? AND notes LIKE ?',('spinza',f"Report chiusura #{row['id']} - %")).fetchone()['n'],len(expenses))
                page=routes['/report-chiusura'](Request(session))
                self.assertIn('Incassi al netto delle spese sull’incasso',page)
                if remaining is not None:
                    self.assertIn('Fondo cassa iniziale:',page)
                    self.assertIn(('%.2f'%(remaining/100)).replace('.',','),page)
                if letter=='g':
                    verify_path='/report-chiusura/verifica-cash'
                    check_data=[('report_id',str(row['id'])),('counted_cash','150'),('cash_notes','PRIVATE-AMMANCO <script>nota</script>')]
                    with patch.object(cr,'datetime') as next_day:
                        next_day.now.return_value=datetime.fromisoformat('2026-11-03T12:00:00+01:00')
                        self.assertEqual(send(verify_path,check_data).status_code,303)
                    page=routes['/report-chiusura'](Request(session))
                    self.assertIn('Contanti attesi: <strong>160,00',page)
                    self.assertIn('Mancano: <strong>10,00',page)
                    self.assertIn('PRIVATE-AMMANCO &lt;script&gt;',page)
                    user['role']='staff'
                    staff_page=routes['/report-chiusura'](Request(session))
                    self.assertNotIn('PRIVATE-AMMANCO',staff_page)
                    self.assertNotIn('Bilancio contanti',staff_page)
                    self.assertNotIn('counted_cash',staff_page)
                    with self.assertRaises(HTTPException) as error: send(verify_path,check_data)
                    self.assertEqual(error.exception.status_code,403)
                    user['role']='admin'
                    scope[0]='other'
                    with self.assertRaises(HTTPException): send(verify_path,check_data)
                    scope[0]='spinza'
                    for actual,message in [('165','Contanti in più: <strong>5,00'),('160','Cassa in pari'),('','Contanti ancora da verificare.')]:
                        send(verify_path,[('report_id',str(row['id'])),('counted_cash',actual),('cash_notes','Controllo aggiornato il giorno dopo')])
                        self.assertIn(message,routes['/report-chiusura'](Request(session)))
                    with db.connect() as c:
                        self.assertEqual(c.cursor().execute('SELECT COUNT(*) AS n FROM closure_report_cash_checks WHERE report_id=?',(row['id'],)).fetchone()['n'],1)
                        income=c.cursor().execute('SELECT SUM(amount) AS total FROM cash_entries WHERE notes=?',(f"Report chiusura #{row['id']}",)).fetchone()
                        self.assertAlmostEqual(income['total'],170)
                    for value in ['-1','NaN','1.234']:
                        with self.assertRaises(HTTPException):send(verify_path,[('report_id',str(row['id'])),('counted_cash',value)])
                    with self.assertRaises(HTTPException):send(verify_path,[('report_id',str(row['id'])),('cash_notes','x'*2001)])
                    with self.assertRaises(HTTPException):send(verify_path,[('report_id',str(row['id']))])
                    with self.assertRaises(HTTPException) as error:
                        run(routes[verify_path](Request(session,[('store','spinza'),('csrf','wrong'),('report_id',str(row['id'])),('counted_cash','150')])))
                    self.assertEqual(error.exception.status_code,403)
                self.assertEqual(delete(row['id']).status_code,303)
                with db.connect() as c:
                    self.assertEqual(c.cursor().execute('SELECT COUNT(*) AS n FROM closure_report_cash_funds WHERE report_id=?',(row['id'],)).fetchone()['n'],0)
                    self.assertEqual(c.cursor().execute('SELECT COUNT(*) AS n FROM closure_report_cash_checks WHERE report_id=?',(row['id'],)).fetchone()['n'],0)
            for extra in [
                [('expense_mode','bad')],
                [('expense_mode','included'),('expense_mode','cash_fund')],
                [('expense_mode','cash_fund')],
                [('expense_mode','cash_fund'),('cash_fund_initial','19,99')],
                [('expense_mode','cash_fund'),('cash_fund_initial','-100')],
            ]:
                payload=base+[('submission','z'*43),('expense_name','Test'),('expense_amount','20')]+extra
                with self.assertRaises(HTTPException) as error: send('/report-chiusura/salva',payload)
                self.assertEqual(error.exception.status_code,422)
            with db.connect() as c:
                self.assertEqual(c.cursor().execute('SELECT COUNT(*) AS n FROM closure_reports').fetchone()['n'],0)
            for value in ['-1','NaN','1.234']:
                with self.assertRaises(HTTPException):
                    send('/report-chiusura/salva',base+[('submission','n'*43),('cash_fund_initial',value)])
            user.clear()
            with self.assertRaises(HTTPException):routes['/report-chiusura'](Request(session))
if __name__=='__main__': unittest.main()


