"""Server-side control journal. No request bodies, credentials, or document contents."""
import csv
import io
import logging
from datetime import datetime, timezone, timedelta
from zoneinfo import ZoneInfo
from fastapi import Request
from fastapi.responses import JSONResponse, PlainTextResponse, Response
from .db import connect, using_postgres

ROME = ZoneInfo('Europe/Rome')


def create_control_schema(c):
    pk = 'BIGSERIAL PRIMARY KEY' if using_postgres() else 'INTEGER PRIMARY KEY AUTOINCREMENT'
    c.execute(f'''CREATE TABLE IF NOT EXISTS control_activity_events (
        id {pk}, user_id BIGINT NOT NULL, username TEXT NOT NULL, store TEXT NOT NULL,
        area TEXT NOT NULL DEFAULT '', happened DOUBLE PRECISION NOT NULL,
        kind TEXT NOT NULL, description TEXT NOT NULL, method TEXT NOT NULL,
        path TEXT NOT NULL, status INTEGER NOT NULL)''')
    c.execute('CREATE INDEX IF NOT EXISTS ix_control_activity_day ON control_activity_events(store,happened,user_id)')
    c.execute('CREATE INDEX IF NOT EXISTS ix_control_activity_user ON control_activity_events(user_id,store,happened)')
    c.execute('CREATE INDEX IF NOT EXISTS ix_control_activity_time ON control_activity_events(happened,user_id)')
    c.execute('CREATE INDEX IF NOT EXISTS ix_control_log_time ON logs(ts,store,username)')
    from .control_live import create_live_schema
    create_live_schema(c)


def describe_request(method, path, status):
    if status >= 400: return 'error', 'Richiesta non riuscita: '+path
    if path == '/logout': return 'logout', 'Uscita dal gestionale'
    if path in ('/login', '/admin-login') and method == 'POST': return 'access', 'Accesso al gestionale'
    section = next((label for prefix,label in [('/items','prodotto inventario'),('/inventario','inventario'),('/chiusure','chiusure'),('/fatture','fatture'),('/spese-secondarie','spese secondarie'),('/ordini','ordini'),('/trasfer','trasferimenti'),('/gestionale','gestione economica'),('/admin','amministrazione'),('/set-active-store','negozio attivo'),('/select','area inventario')] if path.startswith(prefix)), 'gestionale')
    if method in ('GET','HEAD'): return 'access', 'Apertura pagina: '+section
    verbs=[('delete','Eliminazione'),('upload','Caricamento documento'),('add','Aggiunta'),('edit','Modifica'),('modifica','Modifica'),('set','Aggiornamento'),('bozza','Salvataggio bozza'),('conferm','Conferma'),('count','Conteggio'),('conteggio','Conteggio'),('import','Importazione')]
    verb=next((v for k,v in verbs if k in path), 'Operazione')
    return ('error' if status>=400 else 'action'), verb+': '+section


def snapshot_target(request, user):
    """Read only the target's scope, before a successful deletion can remove it."""
    if not user: return
    import re
    match=re.match(r'^/(items|chiusure|fatture|spese-secondarie|ordini)/(\d+)(?:/|$)',request.url.path)
    if not match:return
    table={'items':'products','chiusure':'closures','fatture':'invoices_docs','spese-secondarie':'secondary_expenses','ordini':'orders'}[match[1]]
    ph='%s' if using_postgres() else '?'
    try:
        with connect() as conn:
            columns={
                'products':'store,area,category,name',
                'closures':'store,closure_date,filename',
                'invoices_docs':'store,supplier,doc_date,filename',
                'secondary_expenses':'store,expense_date,filename',
                'orders':'store,supplier,status',
            }[table]
            row=conn.cursor().execute(f'SELECT {columns} FROM {table} WHERE id={ph}',(int(match[2]),)).fetchone()
        if row and (user.get('role')=='admin' or row['store']==user.get('store')):
            request.state.control_target_store=row['store']
            request.state.control_target_area=row.get('area') or ''
            fields={'products':['category','name'],'closures':['closure_date','filename'],'invoices_docs':['supplier','doc_date','filename'],'secondary_expenses':['expense_date','filename'],'orders':['supplier','status']}[table]
            request.state.control_target_label=' · '.join(str(row[field]) for field in fields if row.get(field))[:500]
    except Exception:
        logging.getLogger(__name__).warning('Contesto oggetto non disponibile nel controllo',exc_info=True)


def record_request(request, response, previous_user, started, helpers):
    from .control_live import finish_operation
    finish_operation(request,response,previous_user,started)
    path=request.url.path
    if path.startswith(('/static/', '/admin/fasce-orarie', '/admin/controllo-live', '/controllo/presenza', '/controllo/assistenza', '/inventario/attivita')) or path in ('/favicon.ico','/health','/healthz'):
        return
    if request.method not in ('GET','POST','PUT','PATCH','DELETE'): return
    user=request.session.get('user') or previous_user
    if not user: return
    # Cached responses add no new evidence; document openings remain meaningful accesses.
    if request.method=='GET' and response is not None and response.status_code in (304,): return
    status=response.status_code if response is not None else 500
    kind,description=describe_request(request.method,path,status)
    context=getattr(request.state,'control_target_label','')
    if context:description+=' · '+context
    store=request.session.get('active_store') if user.get('role')=='admin' else user.get('store')
    if store not in helpers['STORES']: store=user.get('store') or 'spinza'
    store=getattr(request.state,'control_target_store',None) or store
    area=getattr(request.state,'control_target_area',None) or request.session.get('selected_area') or ''
    if area not in ('bibite','prodotti'): area=''
    from .inventory_activity import init_activity_db
    try:
        init_activity_db()
        ph='%s' if using_postgres() else '?'
        with connect() as conn:
            conn.cursor().execute(f'''INSERT INTO control_activity_events
                (user_id,username,store,area,happened,kind,description,method,path,status)
                VALUES({','.join([ph]*10)})''',
                (int(user['id']),user['username'],store,area,started,kind,description,request.method,path[:400],int(status)))
    except Exception:
        logging.getLogger(__name__).exception('Impossibile registrare evento di controllo')


def log_epoch():
    return "EXTRACT(EPOCH FROM (ts AT TIME ZONE 'UTC'))" if using_postgres() else "CAST(strftime('%s',ts) AS REAL)"


def journal_query(c, *, lo, hi, store, username=None, user_id=None, limit=None, offset=0):
    ph='%s' if using_postgres() else '?'
    scope=f' AND store={ph}' if store!='ALL' else ''
    store_args=(store,) if scope else ()
    who=f' AND user_id={ph}' if user_id is not None else ''
    user_args=(int(user_id),) if who else ()
    log_who=f' AND username={ph}' if username is not None else ''
    name_args=(username,) if log_who else ()
    # Indexed coarse UTC date bounds, followed by exact epoch bounds (also accepts ISO legacy timestamps).
    date_lo=(datetime.fromtimestamp(lo,timezone.utc).date()-timedelta(days=1)).isoformat()
    date_hi=(datetime.fromtimestamp(hi,timezone.utc).date()+timedelta(days=1)).isoformat()
    # Logs are independent evidence; they are shown separately from request receipts.
    sql=f'''SELECT * FROM (
        SELECT id AS source_id,'request' AS source,happened,user_id,username,store,area,kind,
               description,method,path,status,'' AS category,'' AS item,0 AS delta
        FROM control_activity_events WHERE happened>={ph} AND happened<{ph}{scope}{who}
        UNION ALL
        SELECT id AS source_id,'log' AS source,{log_epoch()} AS happened,0 AS user_id,username,store,
               COALESCE(area,'') AS area,'log' AS kind,action AS description,'' AS method,'' AS path,200 AS status,
               category,name AS item,delta
        FROM logs WHERE ts>={ph} AND ts<{ph} AND {log_epoch()}>={ph} AND {log_epoch()}<{ph}{scope}{log_who}
    ) journal ORDER BY happened,source,source_id'''
    args=(lo,hi)+store_args+user_args+(date_lo,date_hi,lo,hi)+store_args+name_args
    if limit is not None:
        sql+=f' LIMIT {ph} OFFSET {ph}'
        args+=(int(limit),int(offset))
    return c.execute(sql,args).fetchall()


def log_value_label(record):
    action=str(record['description']).upper()
    if action in ('CARICO','SCARICO','INVARIATO'):return 'Variazione giacenza'
    if action=='ADD':return 'Quantità inserita'
    if action=='EDIT':return 'Scorta minima registrata'
    if action=='MOVE':return 'Quantità spostata'
    if action=='IMPORT' and record.get('category')=='CSV':return 'Righe importate'
    if action.startswith('CONTEGGIO_'):return 'Giacenza rilevata'
    if action=='ORDER_ORDINATO':return 'Righe ordine'
    return 'Valore nel Log'


def register_control_detail(app,helpers):
    @app.get('/admin/fasce-orarie/dettaglio')
    def control_detail(request: Request, day: str='', store: str='', user_id: int=0, username: str='', start: float=0, end: float=0, page: int=1, download: str=''):
        user=helpers['require_login'](request)
        if not user or user.get('role')!='admin': return PlainTextResponse('Solo amministratore',status_code=403)
        from .inventory_activity import init_activity_db,day_bounds
        try: selected=datetime.strptime(day,'%Y-%m-%d').date()
        except ValueError: return PlainTextResponse('Data non valida',status_code=400)
        if store not in helpers['STORES'] or page<1 or page>100000: return PlainTextResponse('Filtri non validi',status_code=400)
        lo,hi=day_bounds(selected)
        import math
        if not math.isfinite(start) or not math.isfinite(end): return PlainTextResponse('Intervallo non valido',status_code=400)
        if start or end:
            if end<start or start<lo or end>hi: return PlainTextResponse('Intervallo non valido',status_code=400)
            lo,hi=max(lo,start),min(hi,end+0.001)
        init_activity_db()
        ph='%s' if using_postgres() else '?'
        with connect() as conn:
            c=conn.cursor()
            person=c.execute(f'SELECT username FROM users WHERE id={ph}',(user_id,)).fetchone()
            if not person:
                person=c.execute(f'SELECT username FROM control_activity_events WHERE user_id={ph} AND store={ph} ORDER BY happened DESC LIMIT 1',(user_id,store)).fetchone()
            if not person and username:
                person=c.execute(f'SELECT username FROM logs WHERE username={ph} AND store={ph} LIMIT 1',(username,store)).fetchone()
            if not person: return PlainTextResponse('Persona non trovata',status_code=404)
            records=journal_query(c,lo=lo,hi=hi,store=store,username=person['username'],user_id=user_id,limit=None if download=='csv' else 101,offset=0 if download=='csv' else (page-1)*100)
        if download=='csv':
            output=io.StringIO();writer=csv.writer(output,delimiter=';')
            writer.writerow(['Ora italiana','Utente','Negozio','Reparto','Tipo','Operazione','Articolo / dettaglio','Valore nel Log','Significato valore','Esito HTTP','Percorso'])
            def safe(v):
                text=str(v if v is not None else '')
                return "'"+text if text.lstrip().startswith(('=','+','-','@')) else text
            for r in records:
                writer.writerow([safe(v) for v in [datetime.fromtimestamp(r['happened'],ROME).isoformat(),r['username'],helpers['STORES'].get(r['store'],r['store']),r['area'],r['kind'],r['description'],(r['category']+' · '+r['item']).strip(' ·'),r['delta'] if r['source']=='log' else '',log_value_label(r) if r['source']=='log' else '',r['status'],r['path']]])
            return Response('\ufeff'+output.getvalue(),media_type='text/csv; charset=utf-8',headers={'Content-Disposition':'attachment; filename="registro_attivita.csv"','Cache-Control':'no-store'})
        more=len(records)>100
        data=[]
        for record in records[:100]:
            r=dict(record);r['metric_label']=log_value_label(r) if r['source']=='log' else '';r['time']=datetime.fromtimestamp(r['happened'],ROME).strftime('%H:%M:%S %Z');data.append(r)
        return JSONResponse({'username':person['username'],'store':helpers['STORES'][store],'day':day,'records':data,'page':page,'has_more':more},headers={'Cache-Control':'no-store'})
