"""Unified read-through expense archive. Documents are evidence, never automatically payments."""
import json
import secrets
from collections import Counter,defaultdict
from datetime import date
from decimal import Decimal,ROUND_HALF_UP
from urllib.parse import urlencode
from fastapi import Request,HTTPException
from fastapi.responses import RedirectResponse,JSONResponse
from .db import connect,using_postgres
from .expense_program import schema,p,local_copy,legacy_connect,STORE_MAP,stamp
from .cash_treasury import ensure_schema as cash_schema


def cents(value):return int((Decimal(str(value or 0))*100).quantize(Decimal('1'),rounding=ROUND_HALF_UP))
def record(key,store,day,description,amount,category,origin,status,url,method=''):
    return dict(key=key,store=store,day=str(day or '')[:10],description=description or 'Senza descrizione',amount=amount,category=category or 'Da classificare',origin=origin,status=status,url=url,method=method,provenance=[],possible_duplicate=False)


def collect(cur, include_documents=True):
    rows=[]
    postings={r['source_key']:r for r in cur.execute('SELECT * FROM expense_program_postings').fetchall()}
    cash_tokens={r['cash_token'] for r in postings.values() if r['cash_token']}
    for r in cur.execute('SELECT * FROM cash_expenses ORDER BY flow_date DESC,id DESC').fetchall():
        rows.append(record('uscita:'+str(r['id']),r['store'],r['flow_date'],r['notes'] or r['supplier'],cents(r['amount']),r['category'],
                           'Report chiusura' if (r['notes'] or '').startswith('Report chiusura #') else 'Uscite gestionale','paid','/gestionale/uscite',r['payment_method']))
        rows[-1]['created_by']=r['created_by']
    for r in cur.execute('SELECT * FROM cash_company_spending ORDER BY day DESC').fetchall():
        if r['token'] in cash_tokens:continue
        for key,store in (('sc_cents','spinza'),('reburger_cents','aziende')):
            if r[key]:rows.append(record('cash:'+r['token']+':'+key,store,r['day'],r['description'],r[key],'Da classificare','Spese in cash','paid','/contanti-nei-negozi/spese?day='+str(r['day']),'Contanti'))
    # Unreviewed report expenses are retained too, without counting them as posted accounting.
    for r in cur.execute('''SELECT r.id,r.store,r.closure_date,e.lines_json FROM closure_reports r
        JOIN closure_report_expenses e ON e.report_id=r.id LEFT JOIN closure_report_exports x ON x.report_id=r.id WHERE x.report_id IS NULL''').fetchall():
        for i,line in enumerate(json.loads(r['lines_json'] or '[]')):
            rows.append(record(f'report:{r["id"]}:{i}',r['store'],r['closure_date'],line.get('name'),int(line['cents']),'Spese chiusura','Report in verifica','document','/report-chiusura?'+urlencode(dict(store=r['store'],day=str(r['closure_date']))),'Contanti'))
    state=cur.execute('SELECT data FROM expense_program_state WHERE id=1').fetchone()
    if state:
        with local_copy(state['data']):
            with legacy_connect() as db:
                payments=db.execute('SELECT * FROM payments').fetchall()
                for r in payments:
                    key='payment:'+str(r['id']);link=postings.get(key)
                    if link and link['expense_id'] is not None:
                        native=next((x for x in rows if x['key']=='uscita:'+str(link['expense_id'])),None)
                        if native:
                            native['provenance'].append(dict(description='Programma spese · pagamento #'+str(r['id']),url='/gestionale/spese-pagamenti',key=''))
                            continue
                    rows.append(record(key,STORE_MAP.get(r['store_id'],'aziende'),r['paid_at'] if r['status']=='paid' else r['due_date'],r['description'],r['amount_cents'],r['category'],'Programma spese','paid' if r['status']=='paid' else 'open','/gestionale/spese-pagamenti','Non indicato' if r['status']=='paid' else ''))
                for r in db.execute('SELECT v.*,b.description,b.category FROM variable_purchases v JOIN variable_budgets b ON b.id=v.budget_id WHERE v.payment_id IS NULL'):
                    if 'purchase:'+str(r['id']) in postings:continue
                    rows.append(record('purchase:'+str(r['id']),STORE_MAP.get(r['store_id'],'aziende'),r['paid_at'] or r['due_date'] or r['purchase_date'],r['description'],r['amount_cents'],r['category'],'Ordini programma spese','paid' if r['status']=='paid' else 'open','/gestionale/spese-pagamenti'))
                for r in db.execute('SELECT * FROM baseline_expenses'):
                    rows.append(record('baseline:'+str(r['id']),STORE_MAP.get(r['store_id'],'aziende'),str(r['report_month'])+'-01',r['description'],r['amount_cents'],r['category'],'Riferimento storico del programma','reference','/gestionale/spese-pagamenti'))
                linked_fixed={(r['fixed_expense_id'],r['due_date']) for r in payments if r['fixed_expense_id']}
                for r in db.execute('SELECT * FROM fixed_expenses WHERE active=1'):
                    if (r['id'],r['next_due_date']) in linked_fixed:continue
                    rows.append(record('fixed:'+str(r['id']),STORE_MAP.get(r['store_id'],'aziende'),r['next_due_date'],r['description'],r['amount_cents'],r['category'],'Prossima rata fissa','open','/gestionale/spese-pagamenti'))
                for r in db.execute('SELECT * FROM imported_fixed_settlements'):
                    if 'settled:'+str(r['id']) in postings:continue
                    source=json.loads(r['original_json'])
                    rows.append(record('settled:'+str(r['id']),STORE_MAP.get(source['store_id'],'aziende'),r['due_date'],source['description'],r['actual_cents'],source.get('category'),'Rata fissa saldata prima dell’importazione','settled','/gestionale/tutte-le-spese'))
    if include_documents:
        for kind,table,datecol in [('fatture','invoices_docs','doc_date'),('spese-secondarie','secondary_expenses','expense_date')]:
            for r in cur.execute(f'''SELECT s.id,s.store,s.{datecol} AS day,s.filename,d.supplier,d.amount,d.description FROM {table} s
                LEFT JOIN digital_documents d ON d.kind={p()} AND d.doc_id=s.id AND d.store=s.store''',(kind,)).fetchall():
                rows.append(record('doc:'+kind+':'+str(r['id']),r['store'],r['day'],r['description'] or r['supplier'] or r['filename'],cents(r['amount']) if r['amount'] is not None else None,'Da classificare','Fatture' if kind=='fatture' else 'Spese secondarie','document',f'/schede/{kind}/{r["id"]}' if r['amount'] is not None else f'/{kind}/{r["id"]}'))
        for r in cur.execute('SELECT id,receipt_date,store,description,filename,spending_token FROM cash_expense_documents').fetchall():
            key='receipt:'+str(r['id']);link=None
            if r['spending_token'].startswith('expense:'):link='uscita:'+r['spending_token'].split(':',1)[1]
            elif r['spending_token']:
                link=next((x['key'] for x in rows if x['key'].startswith('cash:'+r['spending_token']+':')),None)
                if not link:
                    posting=next((x for x in postings.values() if x['cash_token']==r['spending_token']),None)
                    if posting:link='uscita:'+str(posting['expense_id'])
            target=next((x for x in rows if x['key']==link),None)
            url='/contanti-nei-negozi/ricevute/'+str(r['id'])
            if target:target['provenance'].append(dict(description='Ricevuta: '+(r['description'] or r['filename']),url=url,key=''))
            else:rows.append(record(key,r['store'],r['receipt_date'],r['description'] or r['filename'],None,'Da classificare','Ricevute cash','document',url))
    categories={r['source_key']:r['category'] for r in cur.execute('SELECT * FROM expense_archive_categories').fetchall()}
    bykey={r['key']:r for r in rows}
    hidden=set()
    for link in cur.execute('SELECT * FROM expense_archive_links').fetchall():
        source,target=bykey.get(link['source_key']),bykey.get(link['target_key'])
        if source and target:
            target['provenance'].append(dict(description=source['origin']+' · '+source['description'],url=source['url'],key=source['key']));hidden.add(source['key'])
    rows=[r for r in rows if r['key'] not in hidden]
    duplicates=defaultdict(list)
    for r in rows:
        r['category']=categories.get(r['key'],r['category'])
        if r['status']=='paid' and r['amount'] is not None:duplicates[(r['store'],r['day'],r['amount'])].append(r)
    for group in duplicates.values():
        if len({r['origin'] for r in group})>1:
            for r in group:r['possible_duplicate']=True
    return sorted(rows,key=lambda r:(r['day'],r['key']),reverse=True)


def archive_schema(cur):
    schema(cur);cash_schema(cur)
    from .cash_documents import schema as receipt_schema
    receipt_schema(cur)
    cur.execute('CREATE TABLE IF NOT EXISTS expense_archive_selected (source_key TEXT PRIMARY KEY)')
    cur.execute('CREATE TABLE IF NOT EXISTS expense_archive_categories (source_key TEXT PRIMARY KEY,category TEXT NOT NULL)')


PAGE_SIZE=25

def install(app,api):
    def admin(request):
        user=api['require_login'](request)
        if not user or not api['is_admin'](request):raise HTTPException(403,'Sezione riservata agli admin.')
        return user
    def csrf(request):return request.session.setdefault('expense_archive_csrf',secrets.token_urlsafe(32))
    @app.on_event('startup')
    def init_archive():
        with connect() as conn:archive_schema(conn.cursor())

    @app.get('/gestionale/tutte-le-spese')
    def index(request:Request,store:str='ALL',category:str='',origin:str='',status:str='',start:str='',end:str='',q:str='',page:int=1,selected_only:bool=False):
        user=admin(request)
        if store not in (*api['STORES'],'ALL','aziende'):raise HTTPException(422,'Negozio non valido.')
        try:
            if start:date.fromisoformat(start)
            if end:date.fromisoformat(end)
            if start and end and start>end:raise ValueError()
        except ValueError:raise HTTPException(422,'Periodo non valido.')
        with connect() as conn:
            cur=conn.cursor();allrows=collect(cur)
            selected={r['source_key'] for r in cur.execute('SELECT source_key FROM expense_archive_selected').fetchall()}
            for row in allrows:row['selected']=row['key'] in selected
        rows=[r for r in allrows if (store=='ALL' or r['store']==store) and (not category or r['category']==category) and (not origin or r['origin']==origin) and (not status or r['status']==status) and (not start or r['day']>=start) and (not end or r['day']<=end) and (not selected_only or r['selected']) and (not q or q.casefold() in (r['description']+' '+r['category']+' '+r['origin']).casefold())]
        page=max(1,page);count=len(rows)
        totals={s:sum(r['amount'] or 0 for r in rows if r['status']==s and not r['possible_duplicate']) for s in ('paid','open')}
        summary=Counter(r['status'] for r in rows)
        parameters=dict(store=store,category=category,origin=origin,status=status,start=start,end=end,q=q,selected_only=selected_only)
        return api['render']('expense_archive.html',request=request,user=user,rows=rows[(page-1)*PAGE_SIZE:page*PAGE_SIZE],count=count,page=page,
            next_url='/gestionale/tutte-le-spese?'+urlencode(dict(parameters,page=page+1)) if page*PAGE_SIZE<count else '',
            prev_url='/gestionale/tutte-le-spese?'+urlencode(dict(parameters,page=page-1)) if page>1 else '',
            totals=totals,summary=summary,selected_count=sum(r['selected'] for r in allrows),selected_total=sum((r['amount'] or 0) for r in allrows if r['selected'] and not r['possible_duplicate']),categories=sorted({r['category'] for r in allrows}),origins=sorted({r['origin'] for r in allrows}),csrf=csrf(request),program_csrf=request.session.setdefault('expense_program_csrf',secrets.token_urlsafe(32)),**parameters)
    @app.get('/gestionale/tutte-le-spese/pagamenti')
    def payment_options(request:Request,q:str='',exclude:str='',page:int=1):
        admin(request)
        page=max(1,page)
        with connect() as conn:rows=collect(conn.cursor())
        q=q.strip().casefold()[:200]
        options=[r for r in rows if r['status']=='paid' and r['key']!=exclude and (not q or q in (r['description']+' '+r['day']+' '+r['category']).casefold())]
        start=(page-1)*20
        return JSONResponse({'items':[dict(key=r['key'],description=r['description'],day=r['day'],amount=r['amount']) for r in options[start:start+20]],'has_more':start+20<len(options)},headers={'Cache-Control':'no-store'})

    @app.post('/gestionale/tutte-le-spese/classifica')
    async def classify(request:Request):
        user=admin(request);form=await request.form()
        if not secrets.compare_digest(str(form.get('csrf','')),csrf(request)):raise HTTPException(403,'Ricarica la pagina.')
        key=str(form.get('key',''));category=str(form.get('category','')).strip()
        if not category or len(category)>120:raise HTTPException(422,'Inserisci una categoria fino a 120 caratteri.')
        with connect() as conn:
            cur=conn.cursor()
            if key not in {r['key'] for r in collect(cur)}:raise HTTPException(404,'Voce non trovata.')
            cur.execute(f'INSERT INTO expense_archive_categories VALUES({p()},{p()}) ON CONFLICT(source_key) DO UPDATE SET category=excluded.category',(key,category))
        return RedirectResponse('/gestionale/tutte-le-spese',303)
    @app.post('/gestionale/tutte-le-spese/collega')
    async def link(request:Request):
        user=admin(request);form=await request.form()
        if not secrets.compare_digest(str(form.get('csrf','')),csrf(request)):raise HTTPException(403,'Ricarica la pagina.')
        source,target=str(form.get('source','')),str(form.get('target',''))
        with connect() as conn:
            cur=conn.cursor();rows={r['key']:r for r in collect(cur)}
            if source==target or source not in rows or target not in rows or rows[target]['status']!='paid':raise HTTPException(422,'Scegli il pagamento corrispondente.')
            # No chains: all provenance must remain visible at the surviving payment.
            if cur.execute(f'SELECT source_key FROM expense_archive_links WHERE target_key={p()}',(source,)).fetchone():raise HTTPException(422,'Questa voce ha già altri documenti collegati.')
            cur.execute(f'INSERT INTO expense_archive_links VALUES({p()},{p()},{p()},{p()}) ON CONFLICT(source_key) DO UPDATE SET target_key=excluded.target_key,username=excluded.username,linked_at=excluded.linked_at',(source,target,user['username'],stamp()))
        return RedirectResponse('/gestionale/tutte-le-spese',303)

    @app.post('/gestionale/tutte-le-spese/scollega')
    async def unlink(request:Request):
        admin(request);form=await request.form()
        if not secrets.compare_digest(str(form.get('csrf','')),csrf(request)):raise HTTPException(403,'Ricarica la pagina.')
        with connect() as conn:conn.cursor().execute(f'DELETE FROM expense_archive_links WHERE source_key={p()}',(str(form.get('source','')),))
        return RedirectResponse('/gestionale/tutte-le-spese',303)

    @app.post('/gestionale/tutte-le-spese/seleziona')
    async def select(request:Request):
        admin(request);form=await request.form()
        if not secrets.compare_digest(str(form.get('csrf','')),csrf(request)):raise HTTPException(403,'Ricarica la pagina.')
        key=str(form.get('key',''))
        with connect() as conn:
            cur=conn.cursor()
            if key not in {r['key'] for r in collect(cur)}:raise HTTPException(404,'Voce non trovata.')
            if form.get('selected')=='1':cur.execute(f'INSERT INTO expense_archive_selected VALUES({p()}) ON CONFLICT(source_key) DO NOTHING',(key,))
            else:cur.execute(f'DELETE FROM expense_archive_selected WHERE source_key={p()}',(key,))
        return RedirectResponse('/gestionale/tutte-le-spese',303)


def financial_expenses(cur, store='ALL'):
    """Same confirmed payments as the archive; never book estimates or evidence twice."""
    rows=collect(cur, include_documents=False)
    return [dict(id=int(r['key'].split(':')[1]) if r['key'].startswith('uscita:') else 0,
                 flow_date=r['day'], store=r['store'], category=r['category'], supplier='',
                 payment_method=r['method'], amount=r['amount']/100, notes=r['description'],
                 ts=r['day'], created_by=r.get('created_by') or r['origin'],
                 source_url='/gestionale/spese-pagamenti?tab=history' if r['origin']=='Programma spese' or any('Programma spese' in v['description'] for v in r['provenance']) else r['url'],
                 editable=r['key'].startswith('uscita:') and not any('Programma spese' in v['description'] for v in r['provenance']), source_key=r['key'])
            for r in rows if r['status']=='paid' and r['amount'] is not None
            and not r['possible_duplicate'] and (store=='ALL' or r['store']==store)]
