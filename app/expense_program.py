"""Admin expense planner. Atomic durable snapshots preserve the supplied program's data and rules.

The original SQLite database is a private payload in the site's PostgreSQL database,
never a public asset or a deployment-local persistent file. A row lock serializes all
planner operations; the snapshot and native accounting links commit together.
"""
import contextvars
import hashlib
import io
import json
import secrets
import sqlite3
import tempfile
from contextlib import contextmanager
from datetime import date, datetime
from pathlib import Path
from zoneinfo import ZoneInfo
from urllib.parse import urlencode
from fastapi import Request, HTTPException
from fastapi.responses import Response, RedirectResponse, JSONResponse
from starlette.concurrency import run_in_threadpool
from .db import connect, using_postgres
from . import expense_program_legacy as legacy
from .cash_treasury import ensure_schema as cash_schema

ROOT='/spese-programma'
STORE_MAP={1:'spinza',2:'reburger_camaldoli',3:'reburger_maggio'}
ASSETS=Path(__file__).with_name('expense_program_assets')
_local_db=contextvars.ContextVar('expense_program_local_db')

def p():return '%s' if using_postgres() else '?'
def stamp():return datetime.now(ZoneInfo('Europe/Rome')).isoformat(timespec='seconds')

def legacy_connect():
    conn=sqlite3.connect(_local_db.get());conn.row_factory=sqlite3.Row
    conn.execute('PRAGMA foreign_keys=ON')
    return conn
legacy.get_db=legacy_connect
legacy.now_iso=lambda:datetime.now(ZoneInfo('Europe/Rome')).isoformat(sep=' ',timespec='seconds')
legacy.today_iso=lambda:datetime.now(ZoneInfo('Europe/Rome')).date().isoformat()
class RomeDate(date):
    @classmethod
    def today(cls):return datetime.now(ZoneInfo('Europe/Rome')).date()
legacy.date=RomeDate


def schema(cur):
    blob='BYTEA' if using_postgres() else 'BLOB'
    ident='SERIAL PRIMARY KEY' if using_postgres() else 'INTEGER PRIMARY KEY AUTOINCREMENT'
    cur.execute(f'''CREATE TABLE IF NOT EXISTS expense_program_state (
        id INTEGER PRIMARY KEY, data {blob} NOT NULL, revision INTEGER NOT NULL,
        imported_sha TEXT NOT NULL, updated_at TEXT NOT NULL)''')
    cur.execute(f'''CREATE TABLE IF NOT EXISTS expense_program_history (
        id {ident}, revision INTEGER NOT NULL, data {blob} NOT NULL,
        username TEXT NOT NULL, operation TEXT NOT NULL, saved_at TEXT NOT NULL)''')
    cur.execute('''CREATE TABLE IF NOT EXISTS expense_program_postings (
        source_key TEXT PRIMARY KEY, expense_id INTEGER, cash_token TEXT NOT NULL DEFAULT '',
        method TEXT NOT NULL, original_json TEXT NOT NULL)''')
    cur.execute('''CREATE TABLE IF NOT EXISTS expense_archive_links (
        source_key TEXT PRIMARY KEY, target_key TEXT NOT NULL, username TEXT NOT NULL, linked_at TEXT NOT NULL)''')


@contextmanager
def local_copy(data):
    with tempfile.TemporaryDirectory(prefix='expense-planner-') as folder:
        path=Path(folder)/'planner.sqlite';path.write_bytes(bytes(data))
        token=_local_db.set(str(path))
        try:yield path
        finally:_local_db.reset(token)


def paid_rows(conn):
    rows={f'payment:{r["id"]}':dict(r) for r in conn.execute("SELECT * FROM payments WHERE status='paid'")}
    for r in conn.execute("SELECT v.*,b.description,b.category FROM variable_purchases v JOIN variable_budgets b ON b.id=v.budget_id WHERE v.status='paid' AND v.payment_id IS NULL"):
        rows[f'purchase:{r["id"]}']=dict(r)
    return rows


def normalize_import(conn,today):
    """Only overdue fixed cycles are settled; actual amounts are intentionally unknown."""
    conn.execute('''CREATE TABLE IF NOT EXISTS imported_fixed_settlements (
        id INTEGER PRIMARY KEY AUTOINCREMENT, fixed_expense_id INTEGER NOT NULL,
        due_date TEXT NOT NULL, original_json TEXT NOT NULL, actual_cents INTEGER,
        UNIQUE(fixed_expense_id,due_date))''')
    for fixed in conn.execute('SELECT * FROM fixed_expenses WHERE active=1').fetchall():
        row=dict(fixed);due=date.fromisoformat(row['next_due_date'])
        while due<today:
            paid=conn.execute("SELECT 1 FROM payments WHERE fixed_expense_id=? AND due_date=? AND status='paid'",(row['id'],due.isoformat())).fetchone()
            if not paid:
                conn.execute('INSERT OR IGNORE INTO imported_fixed_settlements(fixed_expense_id,due_date,original_json) VALUES(?,?,?)',
                             (row['id'],due.isoformat(),json.dumps(row)))
            due=legacy.add_months(due,int(row['frequency_months']))
        conn.execute('UPDATE fixed_expenses SET next_due_date=? WHERE id=?',(due.isoformat(),row['id']))
    # Preserve linked pending fixed installments in the settlement archive, not in open payments.
    for r in conn.execute("SELECT * FROM payments WHERE status='pending' AND fixed_expense_id IS NOT NULL AND due_date<?",(today.isoformat(),)).fetchall():
        conn.execute('INSERT OR IGNORE INTO imported_fixed_settlements(fixed_expense_id,due_date,original_json) VALUES(?,?,?)',
                     (r['fixed_expense_id'],r['due_date'],json.dumps(dict(r))))
        conn.execute('DELETE FROM expense_selections WHERE selection_key=?',('payment:'+str(r['id']),))
        conn.execute('DELETE FROM payments WHERE id=?',(r['id'],))
    conn.commit()


class Handler(legacy.AppHandler):
    def __init__(self,path,payload):self.path=path;self.payload=payload;self.result=None
    def _read_json(self):return self.payload
    def _send_json(self,payload,status=200):self.result=JSONResponse(payload,status_code=int(status),headers={'Cache-Control':'no-store'})
    def _send_bytes(self,data,content_type,filename=None,status=200):
        headers={'Cache-Control':'no-store'}
        if filename:headers['Content-Disposition']=f'attachment; filename="{filename}"'
        self.result=Response(data,status_code=int(status),media_type=content_type,headers=headers)
    def send_error(self,status,*args):self._send_json({'error':'Percorso non disponibile'},status)


def sync_payments(cur,before,after,method,user):
    removed=set(before)-set(after)
    for key in removed:
        link=cur.execute(f'SELECT * FROM expense_program_postings WHERE source_key={p()}',(key,)).fetchone()
        if link:
            if link['expense_id'] is not None:
                cur.execute(f'DELETE FROM cash_expenses WHERE id={p()}',(link['expense_id'],))
            if link['cash_token']:cur.execute(f'DELETE FROM cash_company_spending WHERE token={p()}',(link['cash_token'],))
            cur.execute(f'DELETE FROM expense_program_postings WHERE source_key={p()}',(key,))
    for key in set(after)-set(before):
        if method not in ('Bonifico','Carta','Contanti'):raise HTTPException(422,'Scegli come è stata pagata la spesa.')
        row=after[key];store=STORE_MAP.get(row['store_id'])
        if not store:raise HTTPException(422,'Negozio non collegato al gestionale.')
        day=str(row.get('paid_at') or row.get('purchase_date') or '')[:10]
        date.fromisoformat(day)
        cents=int(row['amount_cents']);category=row.get('category') or 'Da classificare'
        description=row.get('description') or 'Pagamento fornitore'
        cash_token=''
        cur.execute(f'''INSERT INTO cash_expenses(store,flow_date,category,supplier,payment_method,amount,notes,created_by)
            VALUES({','.join([p()]*8)}) RETURNING id''',(store,day,category,'',method,cents/100,description,user['username']))
        expense_id=cur.fetchone()['id']
        if method=='Contanti':
            cash_token=secrets.token_urlsafe(32)
            cur.execute(f'INSERT INTO cash_company_spending(token,day,description,reburger_cents,sc_cents,recorded_by) VALUES({",".join([p()]*6)})',
                        (cash_token,day,description,0 if store=='spinza' else cents,cents if store=='spinza' else 0,user['username']))
        cur.execute(f'INSERT INTO expense_program_postings(source_key,expense_id,cash_token,method,original_json) VALUES({",".join([p()]*5)})',
                    (key,expense_id,cash_token,method,json.dumps(row)))


def execute(method,path,payload,user):
    with connect() as native:
        cur=native.cursor()
        if using_postgres():cur.execute('SELECT pg_advisory_xact_lock(hashtext(%s))',('expense-program',))
        else:native.execute('BEGIN IMMEDIATE')
        state=cur.execute('SELECT * FROM expense_program_state WHERE id=1'+(' FOR UPDATE' if using_postgres() else '')).fetchone()
        if not state:raise HTTPException(409,'Il programma delle spese deve essere importato.')
        if (method=='POST' and path=='/api/stores') or (method=='DELETE' and path.startswith('/api/stores/')):
            raise HTTPException(422,'I negozi sono collegati a quelli del gestionale e non si eliminano da qui.')
        with local_copy(state['data']) as file:
            if method!='GET':
                with legacy_connect() as db:before=paid_rows(db)
            handler=Handler(path,payload)
            getattr(handler,'do_'+method)()
            result=handler.result
            if result is None:raise HTTPException(500,'Risposta del programma non disponibile.')
            if result.status_code>=400:return result
            if method!='GET':
                with legacy_connect() as db:after=paid_rows(db)
                sync_payments(cur,before,after,payload.get('payment_method'),user)
            updated=file.read_bytes()
            if updated!=bytes(state['data']):
                cur.execute(f'INSERT INTO expense_program_history(revision,data,username,operation,saved_at) VALUES({",".join([p()]*5)})',
                            (state['revision'],state['data'],user['username'],method+' '+path,stamp()))
                cur.execute(f'UPDATE expense_program_state SET data={p()},revision=revision+1,updated_at={p()} WHERE id=1',(updated,stamp()))
            return result


def install(app,api):
    def admin(request):
        user=api['require_login'](request)
        if not user or not api['is_admin'](request):raise HTTPException(403,'Sezione riservata agli admin.')
        return user
    def csrf(request):return request.session.setdefault('expense_program_csrf',secrets.token_urlsafe(32))
    def check(request):
        if not secrets.compare_digest(request.headers.get('x-expense-csrf',''),csrf(request)):raise HTTPException(403,'Ricarica la pagina prima di salvare.')
    @app.on_event('startup')
    def init():
        with connect() as conn:schema(conn.cursor())
    @app.get('/gestionale/spese-pagamenti')
    def index(request:Request):
        user=admin(request)
        with connect() as conn:
            cur=conn.cursor();state=cur.execute('SELECT revision,updated_at FROM expense_program_state WHERE id=1').fetchone()
        return api['render']('expense_program.html',request=request,user=user,state=state,csrf=csrf(request))
    @app.post(ROOT+'/importa')
    async def import_data(request:Request):
        user=admin(request);check(request)
        form=await request.form();upload=form.get('file')
        if not upload:raise HTTPException(422,'Seleziona il database del programma.')
        data=await upload.read(8*1024*1024+1)
        if len(data)>8*1024*1024 or not data.startswith(b'SQLite format 3\x00'):raise HTTPException(422,'Database non valido.')
        with connect() as conn:
            cur=conn.cursor();schema(cur)
            if using_postgres():cur.execute('SELECT pg_advisory_xact_lock(hashtext(%s))',('expense-program',))
            else:conn.execute('BEGIN IMMEDIATE')
            if cur.execute('SELECT id FROM expense_program_state WHERE id=1').fetchone():raise HTTPException(409,'Dati già importati: nessuna sovrascrittura effettuata.')
            with local_copy(data) as file:
                with legacy_connect() as db:
                    if db.execute('PRAGMA integrity_check').fetchone()[0]!='ok':raise HTTPException(422,'Database danneggiato.')
                    if set(r[0] for r in db.execute('SELECT id FROM stores'))!=set(STORE_MAP):raise HTTPException(422,'Verifica la corrispondenza dei negozi.')
                    normalize_import(db,api['_rome_today']())
                normalized=file.read_bytes()
            cur.execute(f'INSERT INTO expense_program_history(revision,data,username,operation,saved_at) VALUES({",".join([p()]*5)})',(0,data,user['username'],'Importazione originale',stamp()))
            cur.execute(f'INSERT INTO expense_program_state VALUES(1,{p()},1,{p()},{p()})',(normalized,hashlib.sha256(data).hexdigest(),stamp()))
        return JSONResponse({'ok':True})
    @app.post(ROOT+'/rate-saldate/{sid}')
    async def complete_settlement(request:Request,sid:int):
        user=admin(request)
        form=await request.form()
        if not secrets.compare_digest(str(form.get('csrf','')),csrf(request)):raise HTTPException(403,'Ricarica la pagina.')
        actual=legacy.money_to_cents(form.get('amount'))
        day=str(form.get('paid_date',''));method=str(form.get('payment_method',''))
        try:date.fromisoformat(day)
        except ValueError:raise HTTPException(422,'Indica la data reale del pagamento.')
        with connect() as conn:
            cur=conn.cursor();schema(cur);cash_schema(cur)
            if using_postgres():cur.execute('SELECT pg_advisory_xact_lock(hashtext(%s))',('expense-program',))
            else:conn.execute('BEGIN IMMEDIATE')
            state=cur.execute('SELECT * FROM expense_program_state WHERE id=1'+(' FOR UPDATE' if using_postgres() else '')).fetchone()
            if not state:raise HTTPException(404)
            with local_copy(state['data']) as file:
                with legacy_connect() as db:
                    row=db.execute('SELECT * FROM imported_fixed_settlements WHERE id=?',(sid,)).fetchone()
                    if not row:raise HTTPException(404,'Rata non trovata.')
                    if row['actual_cents'] is not None:raise HTTPException(409,'Importo già registrato.')
                    source=json.loads(row['original_json']);source.update(amount_cents=actual,paid_at=day)
                    sync_payments(cur,{}, {'settled:'+str(sid):source},method,user)
                    db.execute('UPDATE imported_fixed_settlements SET actual_cents=? WHERE id=?',(actual,sid))
                updated=file.read_bytes()
            cur.execute(f'INSERT INTO expense_program_history(revision,data,username,operation,saved_at) VALUES({",".join([p()]*5)})',(state['revision'],state['data'],user['username'],'Importo reale rata saldata',stamp()))
            cur.execute(f'UPDATE expense_program_state SET data={p()},revision=revision+1,updated_at={p()} WHERE id=1',(updated,stamp()))
        return RedirectResponse('/gestionale/tutte-le-spese',303)
    @app.get(ROOT+'/originale')
    def original(request:Request):
        admin(request)
        with connect() as conn:row=conn.cursor().execute("SELECT data FROM expense_program_history WHERE revision=0 ORDER BY id LIMIT 1").fetchone()
        if not row:raise HTTPException(404,'Originale non disponibile.')
        return Response(bytes(row['data']),media_type='application/octet-stream',headers={'Content-Disposition':'attachment; filename="spese-originale.db"','Cache-Control':'no-store'})
    @app.get(ROOT+'/app')
    def planner(request:Request):
        admin(request);csrf(request)
        selected=request.session.get('active_store') or 'spinza'
        config=dict(store=selected,id=next((key for key,value in STORE_MAP.items() if value==selected),None))
        html=(ASSETS/'index.html').read_text().replace('</head>','<script>window.EXPENSE_SITE_CONTEXT='+json.dumps(config)+';</script></head>')
        return Response(html,media_type='text/html',headers={'Cache-Control':'no-store'})
    @app.get(ROOT+'/assets/{name}')
    def asset(request:Request,name:str):
        admin(request)
        if name not in ('style_pro.css','app_pro.js'):raise HTTPException(404)
        return Response((ASSETS/name).read_bytes(),media_type='text/css' if name.endswith('.css') else 'application/javascript',headers={'Cache-Control':'no-store'})
    @app.get(ROOT+'/bridge.js')
    def bridge(request:Request):
        admin(request)
        script=(ASSETS/'bridge.js').read_text().replace('__CSRF__',json.dumps(csrf(request)))
        return Response(script,media_type='application/javascript',headers={'Cache-Control':'no-store'})
    @app.api_route(ROOT+'/api/{path:path}',methods=['GET','POST','DELETE'])
    async def dispatch(request:Request,path:str):
        user=admin(request)
        if request.method!='GET':check(request)
        payload=await request.json() if request.method=='POST' else {}
        if not isinstance(payload,dict):raise HTTPException(422,'Dati non validi.')
        url='/api/'+path+('?' + request.url.query if request.url.query else '')
        try:return await run_in_threadpool(execute,request.method,url,payload,user)
        except sqlite3.Error:raise HTTPException(422,'Impossibile salvare: controlla i dati inseriti. Nessun dato è stato modificato.') from None
