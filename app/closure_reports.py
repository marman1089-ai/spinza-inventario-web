"""Manual closing reports: integer cents and store-scoped persistent channels."""
import json
import re
import secrets
from datetime import date, datetime
from zoneinfo import ZoneInfo
from fastapi import Request, HTTPException
from fastapi.responses import RedirectResponse
from .db import connect, using_postgres


def cents(value):
    value = str(value).strip().replace(',', '.')
    if not re.fullmatch(r'\d{1,9}(?:\.\d{1,2})?', value):
        raise HTTPException(422, 'Inserisci importi positivi con massimo due decimali, senza separatori delle migliaia.')
    whole, _, fraction = value.partition('.')
    return int(whole)*100 + int((fraction+'00')[:2])


def init_reports():
    ident = 'SERIAL PRIMARY KEY' if using_postgres() else 'INTEGER PRIMARY KEY AUTOINCREMENT'
    with connect() as conn:
        cur=conn.cursor()
        cur.execute('CREATE TABLE IF NOT EXISTS closure_report_channels (store TEXT NOT NULL, name TEXT NOT NULL, channel_key TEXT NOT NULL, PRIMARY KEY(store,channel_key))')
        cur.execute(f"CREATE TABLE IF NOT EXISTS closure_reports (id {ident}, store TEXT NOT NULL, closure_date TEXT NOT NULL, lines_json TEXT NOT NULL, total_cents BIGINT NOT NULL, notes TEXT NOT NULL, created_by TEXT NOT NULL, created_at TEXT NOT NULL, submission_token TEXT NOT NULL UNIQUE)")
        cur.execute('CREATE INDEX IF NOT EXISTS ix_closure_reports_scope ON closure_reports(store,closure_date)')
        cur.execute('CREATE TABLE IF NOT EXISTS closure_report_exports (report_id INTEGER PRIMARY KEY)')


def install(app, api):
    ph=api['_ph']; stores=api['STORES']
    def auth(request):
        user=api['require_login'](request)
        if not user: raise HTTPException(401, 'Accedi al gestionale.')
        return user, api['_effective_store'](request,user)
    def allowed(scope,store):
        if store not in stores or store=='ALL' or (scope!='ALL' and scope!=store):
            raise HTTPException(403, 'Negozio non consentito.')
    def channels(cur,store):
        rows=cur.execute(f'SELECT name FROM closure_report_channels WHERE store={ph()} ORDER BY name',(store,)).fetchall()
        return ['POS','Cash']+[r['name'] for r in rows]
    def token(request):
        return request.session.setdefault('closure_reports_csrf',secrets.token_urlsafe(32))
    def check(request,form):
        if not secrets.compare_digest(str(form.get('csrf','')),token(request)):
            raise HTTPException(403, 'Sessione scaduta: ricarica la pagina.')
    def export_report(cur, row):
        # Claim and accounting rows commit together; retries cannot duplicate income.
        claimed=cur.execute(f'INSERT INTO closure_report_exports(report_id) VALUES({ph()}) ON CONFLICT(report_id) DO NOTHING RETURNING report_id',(row['id'],)).fetchone()
        if not claimed: return
        for line in json.loads(row['lines_json']):
            if not line['cents']: continue
            method='Contanti' if line['name']=='Cash' else line['name']
            api['_ensure_payment_method'](cur,method,row['created_by'])
            notes=f"Report chiusura #{row['id']}" + (' Â· '+row['notes'] if row['notes'] else '')
            api['_insert_cash_entry_compat'](cur,row['store'],row['closure_date'],method,line['cents']/100,notes,row['created_by'])
            api['_log'](cur,store=row['store'],username=row['created_by'],action='CREATE',category='CASSA',name=f"Report chiusura #{row['id']} - {method}",delta=line['cents']/100)
    @app.on_event('startup')
    def startup_reports():
        init_reports()
        with connect() as conn:
            cur=conn.cursor()
            pending=cur.execute('SELECT r.* FROM closure_reports r LEFT JOIN closure_report_exports e ON e.report_id=r.id WHERE e.report_id IS NULL ORDER BY r.id').fetchall()
            for row in pending: export_report(cur,row)
    @app.get('/report-chiusura')
    def index(request:Request, store:str='', start:str='', end:str='', saved:bool=False):
        user,scope=auth(request)
        selected=store or (scope if scope!='ALL' else next(s for s in stores if s!='ALL'))
        allowed(scope,selected)
        for d in (start,end):
            if d:
                try: date.fromisoformat(d)
                except ValueError: raise HTTPException(422,'Data non valida.')
        if start and end and start>end: raise HTTPException(422,'Periodo non valido.')
        with connect() as conn:
            cur=conn.cursor(); names=channels(cur,selected)
            sql=f'SELECT * FROM closure_reports WHERE store={ph()}'; params=[selected]
            if start: sql+=f' AND closure_date>={ph()}'; params.append(start)
            if end: sql+=f' AND closure_date<={ph()}'; params.append(end)
            rows=cur.execute(sql+' ORDER BY closure_date DESC,id DESC',tuple(params)).fetchall()
        for r in rows: r['lines']=json.loads(r['lines_json'])
        return api['render']('closure_reports.html',request=request,user=user,brand=scope,stores=stores,selected=selected,channels=names,rows=rows,start=start,end=end,saved=saved,today=datetime.now(ZoneInfo('Europe/Rome')).date().isoformat(),csrf=token(request),submission=secrets.token_urlsafe(32),period_total=sum(r['total_cents'] for r in rows))
    @app.post('/report-chiusura/canali')
    async def add_channel(request:Request):
        user,scope=auth(request); form=await request.form(); check(request,form)
        store=str(form.get('store','')); allowed(scope,store)
        name=' '.join(str(form.get('name','')).split())
        if not name or len(name)>60: raise HTTPException(422,'Il nome del canale deve contenere da 1 a 60 caratteri.')
        key=name.casefold()
        if key in ('pos','cash','contanti'): raise HTTPException(422,'POS e Cash sono giÃ  presenti.')
        with connect() as conn:
            conn.cursor().execute(f'INSERT INTO closure_report_channels(store,name,channel_key) VALUES({ph()},{ph()},{ph()}) ON CONFLICT(store,channel_key) DO NOTHING',(store,name,key))
        return RedirectResponse('/report-chiusura?store='+store,303)
    @app.post('/report-chiusura/canali/elimina')
    async def delete_channel(request:Request):
        user,scope=auth(request); form=await request.form(); check(request,form)
        store=str(form.get('store','')); allowed(scope,store)
        key=' '.join(str(form.get('name','')).split()).casefold()
        if not key or key in ('pos','cash','contanti'):
            raise HTTPException(422,'POS e Cash sono canali di base e non possono essere eliminati.')
        with connect() as conn:
            # Historical report snapshots and accounting entries remain intact.
            conn.cursor().execute(f'DELETE FROM closure_report_channels WHERE store={ph()} AND channel_key={ph()}',(store,key))
        return RedirectResponse('/report-chiusura?store='+store,303)
    @app.post('/report-chiusura/salva')
    async def save(request:Request):
        user,scope=auth(request); form=await request.form(); check(request,form)
        store=str(form.get('store','')); allowed(scope,store)
        try: day=date.fromisoformat(str(form.get('closure_date',''))).isoformat()
        except ValueError: raise HTTPException(422,'Data della chiusura non valida.')
        notes=str(form.get('notes','')).strip()
        if len(notes)>2000: raise HTTPException(422,'Note troppo lunghe (massimo 2000 caratteri).')
        submission=str(form.get('submission',''))
        if not re.fullmatch(r'[A-Za-z0-9_-]{40,60}',submission): raise HTTPException(422,'Ricarica il modulo prima di salvare.')
        raw_names=form.getlist('channel'); raw_amounts=form.getlist('amount')
        if len(raw_names)!=len(raw_amounts) or not 2<=len(raw_names)<=100: raise HTTPException(422,'Canali non validi.')
        with connect() as conn:
            cur=conn.cursor(); known=channels(cur,store)
            if len(set(raw_names))!=len(raw_names) or not {'POS','Cash'}.issubset(raw_names) or any(n not in known for n in raw_names):
                raise HTTPException(422,'Canali non validi: ricarica il modulo.')
            lines=[dict(name=n,cents=cents(v)) for n,v in zip(raw_names,raw_amounts)]
            total=sum(r['cents'] for r in lines)
            cur.execute(f"INSERT INTO closure_reports(store,closure_date,lines_json,total_cents,notes,created_by,created_at,submission_token) VALUES({','.join([ph()]*8)}) ON CONFLICT(submission_token) DO NOTHING",(store,day,json.dumps(lines,ensure_ascii=False),total,notes,user['username'],datetime.now(ZoneInfo('Europe/Rome')).isoformat(timespec='seconds'),submission))
            row=cur.execute(f'SELECT * FROM closure_reports WHERE submission_token={ph()}',(submission,)).fetchone()
            export_report(cur,row)
        return RedirectResponse('/report-chiusura?store='+store+'&saved=true',303)
