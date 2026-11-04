"""Manual closing reports: integer cents and store-scoped persistent channels."""
import json
import calendar
from urllib.parse import urlencode
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
        cur.execute('CREATE TABLE IF NOT EXISTS closure_report_expenses (report_id INTEGER PRIMARY KEY, lines_json TEXT NOT NULL)')
        cur.execute('CREATE TABLE IF NOT EXISTS closure_report_cash_checks (report_id INTEGER PRIMARY KEY, counted_cents BIGINT, notes TEXT NOT NULL, updated_by TEXT NOT NULL, updated_at TEXT NOT NULL)')
        cur.execute('CREATE TABLE IF NOT EXISTS closure_report_cash_funds (report_id INTEGER PRIMARY KEY, initial_cents BIGINT NOT NULL)')
        cur.execute('CREATE TABLE IF NOT EXISTS closure_report_deleted_tokens (submission_token TEXT PRIMARY KEY)')


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
    def can_delete(user,row):
        return user.get('role')=='admin' or str(row['closure_date'])==datetime.now(ZoneInfo('Europe/Rome')).date().isoformat()
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
        expenses=cur.execute(f'SELECT lines_json FROM closure_report_expenses WHERE report_id={ph()}',(row['id'],)).fetchone()
        for expense in json.loads(expenses['lines_json']) if expenses else []:
            notes=f"Report chiusura #{row['id']} - {expense['name']}"
            if expense.get('mode')=='cash_fund': notes+=' (fondo cassa)'
            elif expense.get('mode')=='included': notes+=' (già compresa nell’incasso)'
            api['_insert_cash_expense_compat'](cur,row['store'],row['closure_date'],'Spese chiusura','','',expense['cents']/100,notes,row['created_by'])
            api['_log'](cur,store=row['store'],username=row['created_by'],action='CREATE',category='CASSA',name=notes,delta=-expense['cents']/100)
    @app.on_event('startup')
    def startup_reports():
        init_reports()
        with connect() as conn:
            cur=conn.cursor()
            pending=cur.execute('SELECT r.* FROM closure_reports r LEFT JOIN closure_report_exports e ON e.report_id=r.id WHERE e.report_id IS NULL ORDER BY r.id').fetchall()
            for row in pending: export_report(cur,row)
    @app.get('/report-chiusura')
    def index(request:Request, store:str='', start:str='', end:str='', saved:bool=False, month:str='', day:str='', deleted:bool=False):
        user,scope=auth(request)
        selected=store or (scope if scope!='ALL' else next(s for s in stores if s!='ALL'))
        allowed(scope,selected)
        today=datetime.now(ZoneInfo('Europe/Rome')).date()
        try:
            selected_day=date.fromisoformat(day) if day else None
            if month and not re.fullmatch(r'\d{4}-\d{2}',month): raise ValueError()
            anchor=date.fromisoformat(month+'-01') if month else (selected_day or today).replace(day=1)
            if not 2<=anchor.year<=9998: raise ValueError()
        except ValueError: raise HTTPException(422,'Mese o giorno non valido.')
        for d in (start,end):
            if d:
                try: date.fromisoformat(d)
                except ValueError: raise HTTPException(422,'Data non valida.')
        if start and end and start>end: raise HTTPException(422,'Periodo non valido.')
        with connect() as conn:
            cur=conn.cursor(); names=channels(cur,selected)
            sql=f'SELECT * FROM closure_reports WHERE store={ph()}'; params=[selected]
            if selected_day: sql+=f' AND closure_date={ph()}'; params.append(selected_day.isoformat())
            if start: sql+=f' AND closure_date>={ph()}'; params.append(start)
            if end: sql+=f' AND closure_date<={ph()}'; params.append(end)
            rows=cur.execute(sql+' ORDER BY closure_date DESC,id DESC',tuple(params)).fetchall()
            expense_rows=cur.execute(f'SELECT e.report_id,e.lines_json FROM closure_report_expenses e JOIN closure_reports r ON r.id=e.report_id WHERE r.store={ph()}',(selected,)).fetchall()
            fund_rows=cur.execute(f'SELECT f.report_id,f.initial_cents FROM closure_report_cash_funds f JOIN closure_reports r ON r.id=f.report_id WHERE r.store={ph()}',(selected,)).fetchall()
            cash_checks={}
            if user.get('role')=='admin':
                cash_checks={r['report_id']:r for r in cur.execute(f'SELECT c.* FROM closure_report_cash_checks c JOIN closure_reports r ON r.id=c.report_id WHERE r.store={ph()}',(selected,)).fetchall()}
            counts=cur.execute(f'SELECT closure_date,COUNT(*) AS n FROM closure_reports WHERE store={ph()} AND closure_date>={ph()} AND closure_date<={ph()} GROUP BY closure_date',(selected,anchor.isoformat(),anchor.replace(day=calendar.monthrange(anchor.year,anchor.month)[1]).isoformat())).fetchall()
        by_day={str(r['closure_date']):r['n'] for r in counts}
        def link(**kw): return '/report-chiusura?'+urlencode(dict(store=selected,**kw))
        weeks=[]
        for week in calendar.Calendar(firstweekday=0).monthdayscalendar(anchor.year,anchor.month):
            cells=[]
            for number in week:
                if not number: cells.append(None); continue
                value=anchor.replace(day=number).isoformat()
                cells.append(dict(number=number,date=value,count=by_day.get(value,0),today=value==today.isoformat(),selected=selected_day is not None and value==selected_day.isoformat(),url=link(month=anchor.strftime('%Y-%m'),day=value)+'#closing-history'))
            weeks.append(cells)
        prev=(anchor.replace(year=anchor.year-1,month=12) if anchor.month==1 else anchor.replace(month=anchor.month-1))
        next_month=(anchor.replace(year=anchor.year+1,month=1) if anchor.month==12 else anchor.replace(month=anchor.month+1))
        month_names=['Gennaio','Febbraio','Marzo','Aprile','Maggio','Giugno','Luglio','Agosto','Settembre','Ottobre','Novembre','Dicembre']
        calendar_ctx=dict(weeks=weeks,month_label=month_names[anchor.month-1]+' '+str(anchor.year),month_value=anchor.strftime('%Y-%m'),prev_month=link(month=prev.strftime('%Y-%m'))+'#closing-calendar',next_month=link(month=next_month.strftime('%Y-%m'))+'#closing-calendar',current_month=link(month=today.strftime('%Y-%m'))+'#closing-calendar',selected_day=selected_day.isoformat() if selected_day else '',form_date=selected_day.isoformat() if selected_day else today.isoformat(),days_saved=len(by_day))
        expenses_by_id={r['report_id']:json.loads(r['lines_json']) for r in expense_rows}
        funds_by_id={r['report_id']:r['initial_cents'] for r in fund_rows}
        for r in rows:
            r['lines']=json.loads(r['lines_json'])
            r['expenses']=expenses_by_id.get(r['id'],[])
            r['expense_total']=sum(e['cents'] for e in r['expenses'])
            r['fund_expenses']=sum(e['cents'] for e in r['expenses'] if e.get('mode')=='cash_fund')
            r['fund_initial']=funds_by_id.get(r['id'])
            r['fund_remaining']=r['fund_initial']-r['fund_expenses'] if r['fund_initial'] is not None else None
            r['net_cents']=r['total_cents']-(r['expense_total']-r['fund_expenses'])
            r['can_delete']=can_delete(user,r)
            if user.get('role')=='admin':
                cash=sum(line['cents'] for line in r['lines'] if line['name']=='Cash')
                r['expected_cash']=cash+(r['fund_initial'] or 0)-r['expense_total']
                r['cash_check']=cash_checks.get(r['id'])
                counted=r['cash_check']['counted_cents'] if r['cash_check'] else None
                r['cash_difference']=counted-r['expected_cash'] if counted is not None else None
        return api['render']('closure_reports.html',**calendar_ctx,request=request,user=user,brand=scope,stores=stores,selected=selected,channels=names,rows=rows,start=start,end=end,saved=saved,deleted=deleted,today=datetime.now(ZoneInfo('Europe/Rome')).date().isoformat(),csrf=token(request),submission=secrets.token_urlsafe(32),period_total=sum(r['total_cents'] for r in rows))
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
    @app.post('/report-chiusura/elimina')
    async def delete_report(request:Request):
        user,scope=auth(request); form=await request.form(); check(request,form)
        store=str(form.get('store','')); allowed(scope,store)
        try: report_id=int(str(form.get('report_id','')))
        except ValueError: raise HTTPException(422,'Report non valido.')
        with connect() as conn:
            cur=conn.cursor()
            if not using_postgres(): cur.execute('BEGIN IMMEDIATE')
            lock=' FOR UPDATE' if using_postgres() else ''
            row=cur.execute(f'SELECT * FROM closure_reports WHERE id={ph()} AND store={ph()}'+lock,(report_id,store)).fetchone()
            if not row: raise HTTPException(404,'Report non trovato.')
            if not can_delete(user,row):
                raise HTTPException(403,'Puoi eliminare un report solo nel giorno della chiusura. Per le altre date serve un admin.')
            # Match the complete report reference, never #1 against #10.
            prefix=f"Report chiusura #{report_id}"
            income_prefixes=(prefix+' · ',prefix+' Â· ')
            cur.execute(f'DELETE FROM cash_entries WHERE store={ph()} AND flow_date={ph()} AND (notes={ph()} OR substr(notes,1,{ph()})={ph()} OR substr(notes,1,{ph()})={ph()})',
                        (store,row['closure_date'],prefix,len(income_prefixes[0]),income_prefixes[0],len(income_prefixes[1]),income_prefixes[1]))
            expense_prefix=prefix+' - '
            cur.execute(f'DELETE FROM cash_expenses WHERE store={ph()} AND flow_date={ph()} AND substr(notes,1,{ph()})={ph()}',
                        (store,row['closure_date'],len(expense_prefix),expense_prefix))
            cur.execute(f'INSERT INTO closure_report_deleted_tokens(submission_token) VALUES({ph()}) ON CONFLICT(submission_token) DO NOTHING',(row['submission_token'],))
            cur.execute(f'DELETE FROM closure_report_cash_checks WHERE report_id={ph()}',(report_id,))
            cur.execute(f'DELETE FROM closure_report_cash_funds WHERE report_id={ph()}',(report_id,))
            cur.execute(f'DELETE FROM closure_report_expenses WHERE report_id={ph()}',(report_id,))
            cur.execute(f'DELETE FROM closure_report_exports WHERE report_id={ph()}',(report_id,))
            cur.execute(f'DELETE FROM closure_reports WHERE id={ph()} AND store={ph()}',(report_id,store))
            api['_log'](cur,store=store,username=user['username'],action='DELETE',category='CASSA',name=prefix+' - eliminato con entrate e uscite collegate',delta=0)
        return RedirectResponse('/report-chiusura?'+urlencode(dict(store=store,deleted='true'))+'#closing-history',303)
    @app.post('/report-chiusura/verifica-cash')
    async def check_cash(request:Request):
        user,scope=auth(request)
        if user.get('role')!='admin': raise HTTPException(403,'Verifica contanti riservata all’admin.')
        form=await request.form(); check(request,form)
        store=str(form.get('store','')); allowed(scope,store)
        try: report_id=int(str(form.get('report_id','')))
        except ValueError: raise HTTPException(422,'Report non valido.')
        raw_counted=str(form.get('counted_cash','')).strip()
        counted=cents(raw_counted) if raw_counted else None
        notes=str(form.get('cash_notes','')).strip()
        if len(notes)>2000: raise HTTPException(422,'Note troppo lunghe (massimo 2000 caratteri).')
        if counted is None and not notes: raise HTTPException(422,'Inserisci i contanti contati oppure una nota.')
        with connect() as conn:
            cur=conn.cursor()
            if not using_postgres(): cur.execute('BEGIN IMMEDIATE')
            lock=' FOR UPDATE' if using_postgres() else ''
            row=cur.execute(f'SELECT id,closure_date FROM closure_reports WHERE id={ph()} AND store={ph()}'+lock,(report_id,store)).fetchone()
            if not row: raise HTTPException(404,'Report non trovato.')
            cur.execute(f'INSERT INTO closure_report_cash_checks(report_id,counted_cents,notes,updated_by,updated_at) VALUES({",".join([ph()]*5)}) ON CONFLICT(report_id) DO UPDATE SET counted_cents=excluded.counted_cents,notes=excluded.notes,updated_by=excluded.updated_by,updated_at=excluded.updated_at',
                        (report_id,counted,notes,user['username'],datetime.now(ZoneInfo('Europe/Rome')).isoformat(timespec='seconds')))
            api['_log'](cur,store=store,username=user['username'],action='UPDATE',category='CASSA',name=f'Verifica contanti report #{report_id}',delta=0)
        return RedirectResponse('/report-chiusura?'+urlencode(dict(store=store,day=str(row['closure_date'])))+f'#cash-check-{report_id}',303)
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
        expense_names=form.getlist('expense_name'); expense_amounts=form.getlist('expense_amount')
        if len(expense_names)!=len(expense_amounts) or len(expense_names)>100:
            raise HTTPException(422,'Elenco spese non valido (massimo 100 voci).')
        expense_modes=form.getlist('expense_mode')
        # Older open forms keep the previous add-to-Cash behaviour.
        if not expense_modes: expense_modes=['add_to_cash']*len(expense_names)
        if len(expense_modes)!=len(expense_names): raise HTTPException(422,'Modalità spese non valide.')
        expenses=[]
        for name,amount,mode in zip(expense_names,expense_amounts,expense_modes):
            if mode not in ('add_to_cash','included','cash_fund'): raise HTTPException(422,'Modalità spesa non valida.')
            name=' '.join(str(name).split()); amount=str(amount).strip()
            if not name and not amount: continue
            if not name or len(name)>120: raise HTTPException(422,'Inserisci il nome della spesa (massimo 120 caratteri).')
            value=cents(amount)
            if value<=0: raise HTTPException(422,'Inserisci un importo maggiore di zero per la spesa.')
            expenses.append(dict(name=name,cents=value,mode=mode))
        fund_expenses=sum(e['cents'] for e in expenses if e['mode']=='cash_fund')
        fund_initial=None
        raw_fund=str(form.get('cash_fund_initial','')).strip()
        if raw_fund or fund_expenses:
            fund_initial=cents(raw_fund)
            if fund_expenses>fund_initial: raise HTTPException(422,'Le spese dal fondo cassa superano il fondo iniziale.')
        with connect() as conn:
            cur=conn.cursor()
            if not using_postgres(): cur.execute('BEGIN IMMEDIATE')
            if cur.execute(f'SELECT submission_token FROM closure_report_deleted_tokens WHERE submission_token={ph()}',(submission,)).fetchone():
                raise HTTPException(409,'Questo report è stato eliminato. Ricarica la pagina per inserire una nuova chiusura.')
            known=channels(cur,store)
            if len(set(raw_names))!=len(raw_names) or not {'POS','Cash'}.issubset(raw_names) or any(n not in known for n in raw_names):
                raise HTTPException(422,'Canali non validi: ricarica il modulo.')
            lines=[dict(name=n,cents=cents(v)) for n,v in zip(raw_names,raw_amounts)]
            # Only expenses not yet included in the entered revenue increase Cash.
            expense_total=sum(e['cents'] for e in expenses if e['mode']=='add_to_cash')
            for line in lines:
                if line['name']=='Cash': line['cents']+=expense_total
            total=sum(r['cents'] for r in lines)
            cur.execute(f"INSERT INTO closure_reports(store,closure_date,lines_json,total_cents,notes,created_by,created_at,submission_token) VALUES({','.join([ph()]*8)}) ON CONFLICT(submission_token) DO NOTHING RETURNING id",(store,day,json.dumps(lines,ensure_ascii=False),total,notes,user['username'],datetime.now(ZoneInfo('Europe/Rome')).isoformat(timespec='seconds'),submission))
            inserted=cur.fetchone()
            if inserted:
                if fund_initial is not None:
                    cur.execute(f'INSERT INTO closure_report_cash_funds(report_id,initial_cents) VALUES({ph()},{ph()})',(inserted['id'],fund_initial))
                cur.execute(f'INSERT INTO closure_report_expenses(report_id,lines_json) VALUES({ph()},{ph()})',(inserted['id'],json.dumps(expenses,ensure_ascii=False)))
            row=cur.execute(f'SELECT * FROM closure_reports WHERE submission_token={ph()}',(submission,)).fetchone()
            export_report(cur,row)
        return RedirectResponse('/report-chiusura?store='+store+'&saved=true',303)
