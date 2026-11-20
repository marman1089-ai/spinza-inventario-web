"""Kitchen invoice drafts: durable opt-in AI queue and reviewed arrivals, never stock writes."""
import hashlib
import json
import math
import os
import secrets
import threading
from collections import defaultdict
from datetime import date,datetime,timedelta,timezone
from urllib.parse import urlencode
from fastapi import HTTPException,Request
from fastapi.responses import RedirectResponse,JSONResponse
from starlette.concurrency import run_in_threadpool
from .db import connect,using_postgres
from .document_ai import extract_document
from .document_review import available_document

_wakeup=threading.Event()
_stop=threading.Event()
_lock=threading.Lock()
_thread=None


def ph():return '%s' if using_postgres() else '?'
def now():return datetime.now(timezone.utc).isoformat()
def configured():return bool(os.getenv('OPENAI_API_KEY','').strip())
def digest(value):return hashlib.sha256(json.dumps(value,sort_keys=True,ensure_ascii=False,default=str).encode()).hexdigest()
def transaction(conn):
    if not using_postgres():conn.cursor().execute('BEGIN IMMEDIATE')

def convert_quantity(value,source_unit,target_unit):
    import re
    def scale(unit):
        unit=str(unit or '').strip().casefold().replace(' ','').replace(',','.').rstrip('.')
        aliases={'grammi':'g','grammo':'g','gr':'g','chilogrammi':'kg','chili':'kg','litri':'l','litro':'l','lt':'l','pezzi':'pz','pezzo':'pz','conf':'conf','confezioni':'conf'}
        unit=aliases.get(unit,unit)
        match=re.fullmatch(r'(\d+(?:\.\d+)?)(kg|g|l|ml)',unit)
        factor=float(match[1]) if match else 1
        base=match[2] if match else unit
        if base in ('kg','g'):return 'mass',factor*(0.001 if base=='g' else 1)
        if base in ('l','ml'):return 'volume',factor*(0.001 if base=='ml' else 1)
        return base,factor
    left,a=scale(source_unit);right,b=scale(target_unit)
    return float(value)*a/b if left and left==right and b else None

def schema(cur):
    cur.execute("""CREATE TABLE IF NOT EXISTS kitchen_invoice_ai_jobs (
        doc_id BIGINT PRIMARY KEY,store TEXT NOT NULL,week_start TEXT NOT NULL,
        state TEXT NOT NULL,source_hash TEXT NOT NULL,review_signature TEXT NOT NULL,
        catalog_json TEXT NOT NULL,payload TEXT NOT NULL DEFAULT '',error TEXT NOT NULL DEFAULT '',
        requested_by TEXT NOT NULL,updated_at TEXT NOT NULL,revision INTEGER NOT NULL DEFAULT 1)""")

def catalog(cur,store):
    return [dict(r) for r in cur.execute(f"SELECT id,name,category,unit,location FROM products WHERE store={ph()} AND area='prodotti' ORDER BY id",(store,)).fetchall()]

def review_signature(cur,doc_id,store):
    rows=cur.execute(f'SELECT * FROM kitchen_invoice_reviews WHERE doc_id={ph()} AND store={ph()} ORDER BY id',(doc_id,store)).fetchall()
    for row in rows:
        row['lines']=cur.execute(f'SELECT product_id,qty FROM kitchen_invoice_review_lines WHERE review_id={ph()} ORDER BY product_id',(row['id'],)).fetchall()
    return digest(rows)

def source(cur,doc_id,store):
    row=cur.execute(f"SELECT id,store,doc_date,supplier,filename,content_type,data FROM invoices_docs WHERE id={ph()} AND store={ph()} AND {available_document('fatture')}",(doc_id,store)).fetchone()
    if not row:raise ValueError('Fattura non trovata o ancora da verificare dall’admin.')
    return dict(row)

def source_hash(row):
    return digest([str(row['doc_date']),row['supplier'],row['filename'],hashlib.sha256(bytes(row['data'] or b'')).hexdigest()])

def normalize_result(result,products):
    if not isinstance(result,dict) or not isinstance(result.get('lines'),list) or not str(result.get('text') or '').strip():
        raise ValueError('Nessuna trascrizione completa leggibile: originale conservato, riprova con una foto più chiara.')
    if len(result['lines'])>500:raise ValueError('Troppi prodotti: dividi il documento in più fatture.')
    allowed={int(p['id']):p for p in products}
    warnings=[str(x)[:1000] for x in result.get('warnings',[]) if isinstance(x,str)]
    lines=[]
    for raw in result['lines']:
        if not isinstance(raw,dict):raise ValueError('Righe della lettura non valide.')
        line={key:raw.get(key) for key in ['description','qty','unit','amount','product_id','stock_qty','matching_note','needs_review']}
        line['description']=str(line['description'] or '')[:500]
        line['matching_note']=str(line['matching_note'] or '')[:1500]
        pid=line['product_id']
        if isinstance(pid,bool) or not isinstance(pid,int) or pid not in allowed:
            line['product_id']=None;line['stock_qty']=None;line['needs_review']=True
        else:
            value=line['stock_qty']
            if isinstance(value,bool) or not isinstance(value,(int,float)) or not math.isfinite(value) or value<0 or value>1e9:
                line['stock_qty']=None;line['needs_review']=True
            line['inventory_unit']=allowed[pid]['unit'] or 'unità da verificare'
        for field in ['qty','amount']:
            value=line[field]
            if value is not None and (isinstance(value,bool) or not isinstance(value,(int,float)) or not math.isfinite(value)):
                line[field]=None
        if line['qty'] is not None and line['qty']<0:
            line['stock_qty']=None;line['needs_review']=True
            warnings.append('Presente una quantità negativa/reso: non registrarlo come acquisto positivo.')
        lines.append(line)
    result=dict(result,lines=lines,warnings=list(dict.fromkeys(warnings)))
    return result

def enqueue_docs(api,store,week,doc_id,username,force=False):
    if not configured():raise HTTPException(422,'Chiave API assente: configura OPENAI_API_KEY su Render e riavvia.')
    end=(date.fromisoformat(week)+timedelta(days=6)).isoformat()
    count=0
    with connect() as conn:
        transaction(conn);cur=conn.cursor();schema(cur)
        rows=cur.execute(f"SELECT id FROM invoices_docs WHERE store={ph()} AND doc_date>={ph()} AND doc_date<={ph()} AND {available_document('fatture')}"+(f' AND id={ph()}' if doc_id else '')+' ORDER BY id',(store,week,end,*([doc_id] if doc_id else []))).fetchall()
        if doc_id and not rows:raise HTTPException(404,'Fattura non disponibile in questa settimana.')
        products=catalog(cur,store)
        if not products:raise HTTPException(422,'Non ci sono prodotti cucina in questo negozio.')
        for row in rows:
            existing=cur.execute(f'SELECT state FROM kitchen_invoice_ai_jobs WHERE doc_id={ph()}',(row['id'],)).fetchone()
            reviewed=cur.execute(f"SELECT id FROM kitchen_invoice_reviews WHERE doc_id={ph()} AND store={ph()} AND status='done'",(row['id'],store)).fetchone()
            if existing and existing['state'] in ('pending','working'):continue
            if not force and (existing or reviewed):continue
            original=source(cur,row['id'],store)
            if not original['data']:continue
            values=(row['id'],store,week,'pending',source_hash(original),review_signature(cur,row['id'],store),json.dumps(products,ensure_ascii=False),username,now())
            cur.execute(f"""INSERT INTO kitchen_invoice_ai_jobs(doc_id,store,week_start,state,source_hash,review_signature,catalog_json,requested_by,updated_at)
                VALUES({','.join([ph()]*9)}) ON CONFLICT(doc_id) DO UPDATE SET
                store=excluded.store,week_start=excluded.week_start,state='pending',source_hash=excluded.source_hash,
                review_signature=excluded.review_signature,catalog_json=excluded.catalog_json,payload='',error='',
                requested_by=excluded.requested_by,updated_at=excluded.updated_at,revision=kitchen_invoice_ai_jobs.revision+1
                WHERE kitchen_invoice_ai_jobs.state NOT IN ('pending','working')""",values)
            count+=max(cur.rowcount,0)
    start_worker();_wakeup.set()
    return count

def process_one():
    if not configured():return False
    with connect() as conn:
        transaction(conn);cur=conn.cursor()
        job=cur.execute("SELECT * FROM kitchen_invoice_ai_jobs WHERE state='pending' ORDER BY updated_at,doc_id LIMIT 1"+(' FOR UPDATE SKIP LOCKED' if using_postgres() else '')).fetchone()
        if not job:return False
        cur.execute(f"UPDATE kitchen_invoice_ai_jobs SET state='working',updated_at={ph()} WHERE doc_id={ph()} AND revision={ph()}",(now(),job['doc_id'],job['revision']))
    try:
        with connect() as conn:
            original=source(conn.cursor(),job['doc_id'],job['store'])
        if source_hash(original)!=job['source_hash']:raise ValueError('La fattura è cambiata dopo l’accodamento: avvia una nuova lettura.')
        data=bytes(original['data'] or b'')
        if not data.startswith(b'%PDF'):
            from .pdf_tools import ensure_pdf
            data,_,_=ensure_pdf(data,original['filename'],original['content_type'])
        products=json.loads(job['catalog_json'])
        result=normalize_result(extract_document(data,'Fattura acquisti cucina',inventory=products),products)
        if str(result.get('date') or '')!=str(original['doc_date']):
            result['warnings'].append('La data letta non coincide con quella archiviata: controllala prima di confermare.')
        with connect() as conn:
            cur=conn.cursor();current=source(cur,job['doc_id'],job['store'])
            if source_hash(current)!=job['source_hash']:raise ValueError('Fattura sostituita durante la lettura: risultato non applicato.')
            cur.execute(f"UPDATE kitchen_invoice_ai_jobs SET state='ready',payload={ph()},error='',updated_at={ph()} WHERE doc_id={ph()} AND revision={ph()} AND state='working'",(json.dumps(result,ensure_ascii=False,allow_nan=False),now(),job['doc_id'],job['revision']))
    except Exception as exc:
        # Never expose request bodies, credentials, or provider error responses.
        message=str(exc) if isinstance(exc,ValueError) else 'Lettura non riuscita. Riprova o controlla la configurazione AI su Render.'
        with connect() as conn:
            conn.cursor().execute(f"UPDATE kitchen_invoice_ai_jobs SET state='error',error={ph()},updated_at={ph()} WHERE doc_id={ph()} AND revision={ph()}",(message[:800],now(),job['doc_id'],job['revision']))
    return True

def start_worker():
    global _thread
    if not configured():return
    with _lock:
        if _thread and _thread.is_alive():return
        _stop.clear()
        with connect() as conn:
            cur=conn.cursor();schema(cur)
            stale=(datetime.now(timezone.utc)-timedelta(minutes=30)).isoformat()
            cur.execute(f"UPDATE kitchen_invoice_ai_jobs SET state='pending' WHERE state='working' AND updated_at<{ph()}",(stale,))
        def loop():
            while not _stop.is_set():
                try:active=process_one()
                except Exception:active=False
                if not active:_wakeup.wait(15);_wakeup.clear()
        _thread=threading.Thread(target=loop,name='kitchen-invoice-ai',daemon=True);_thread.start()

def status_token(rows):return digest(sorted([(r['doc_id'],r['state'],r['revision'],r['updated_at']) for r in rows]))

def view_context(request,store,week,doc_id):
    token=request.session.setdefault('kitchen_ai_csrf',secrets.token_urlsafe(32))
    with connect() as conn:
        cur=conn.cursor();schema(cur)
        rows=cur.execute(f'SELECT doc_id,state,error,updated_at,revision FROM kitchen_invoice_ai_jobs WHERE store={ph()} AND week_start={ph()}',(store,week)).fetchall()
        job=cur.execute(f'SELECT * FROM kitchen_invoice_ai_jobs WHERE store={ph()} AND doc_id={ph()}',(store,doc_id)).fetchone() if doc_id else None
    proposal=json.loads(job['payload']) if job and job['payload'] else None
    return dict(ai_status_token=status_token(rows),ai_pending=any(r['state'] in ('pending','working') for r in rows),ai_jobs={r['doc_id']:r for r in rows},ai_job=job,ai_proposal=proposal,ai_configured=configured(),ai_csrf=token,ai_model=os.getenv('OPENAI_DOCUMENT_MODEL','gpt-6.1-terra'))

def install(app,api):
    def auth(request,store):
        user=api['require_login'](request)
        if not user or not api['is_admin'](request):raise HTTPException(403,'Funzione riservata agli admin.')
        if store not in api['STORES']:raise HTTPException(422,'Seleziona un negozio.')
        return user
    def csrf(request,form):
        if not request.session.get('kitchen_ai_csrf') or not secrets.compare_digest(str(form.get('csrf','')),request.session['kitchen_ai_csrf']):raise HTTPException(403,'Ricarica la pagina e riprova.')
    def back(store,week,doc_id=0):return '/inventario/fatture-settimana?'+urlencode(dict(store=store,week=week,doc=doc_id))
    @app.on_event('startup')
    def startup():
        api['_ensure_kitchen_invoice_tables']()
        with connect() as conn:schema(conn.cursor())
        start_worker()
    @app.on_event('shutdown')
    def shutdown():_stop.set();_wakeup.set()
    @app.post('/inventario/fatture-settimana/ai/accoda')
    async def queue(request:Request):
        form=await request.form();store=str(form.get('store',''));user=auth(request,store);csrf(request,form)
        week=api['_kitchen_week'](str(form.get('week','')))
        try:doc_id=int(form.get('doc_id') or 0)
        except ValueError:raise HTTPException(422,'Fattura non valida.')
        if doc_id<0:raise HTTPException(422,'Fattura non valida.')
        force=form.get('force')=='1' and bool(doc_id)
        await run_in_threadpool(enqueue_docs,api,store,week,doc_id,user['username'],force)
        if request.headers.get('accept')=='application/json':return JSONResponse({'redirect':back(store,week,doc_id)})
        return RedirectResponse(back(store,week,doc_id),303)
    @app.get('/inventario/fatture-settimana/ai/stato')
    def status(request:Request,store:str,week:str):
        auth(request,store);week=api['_kitchen_week'](week)
        with connect() as conn:
            rows=conn.cursor().execute(f'SELECT doc_id,state,revision,updated_at FROM kitchen_invoice_ai_jobs WHERE store={ph()} AND week_start={ph()} ORDER BY doc_id',(store,week)).fetchall()
        return JSONResponse({'jobs':rows,'token':status_token(rows)},headers={'Cache-Control':'no-store'})
    @app.post('/inventario/fatture-settimana/ai/conferma')
    async def confirm(request:Request):
        form=await request.form();store=str(form.get('store',''));user=auth(request,store);csrf(request,form)
        try:doc_id=int(form.get('doc_id',''));revision=int(form.get('revision',''));invoice_date=date.fromisoformat(str(form.get('invoice_date',''))).isoformat()
        except (ValueError,TypeError):raise HTTPException(422,'Documento o data non validi.')
        if form.get('reviewed')!='1':raise HTTPException(422,'Conferma di aver verificato tutte le righe e le conversioni.')
        supplier=str(form.get('supplier','')).strip()
        if not supplier or len(supplier)>180:raise HTTPException(422,'Controlla il fornitore.')
        with connect() as conn:
            transaction(conn);cur=conn.cursor()
            job=cur.execute(f'SELECT * FROM kitchen_invoice_ai_jobs WHERE doc_id={ph()} AND store={ph()}'+(' FOR UPDATE' if using_postgres() else ''),(doc_id,store)).fetchone()
            if not job:raise HTTPException(404,'Lettura non trovata.')
            if job['state']=='applied':return RedirectResponse(back(store,job['week_start'],doc_id),303)
            if job['state']!='ready' or job['revision']!=revision:raise HTTPException(409,'La lettura è cambiata. Ricarica la pagina.')
            try:original=source(cur,doc_id,store)
            except ValueError:raise HTTPException(409,'Fattura non più disponibile. Nessun arrivo salvato.')
            if source_hash(original)!=job['source_hash'] or review_signature(cur,doc_id,store)!=job['review_signature']:raise HTTPException(409,'Fattura o quantità modificate nel frattempo. Ricarica e avvia una nuova lettura prima di sostituire i dati.')
            products=catalog(cur,store);allowed={str(p['id']):p for p in products}
            if digest(products)!=digest(json.loads(job['catalog_json'])):raise HTTPException(409,'Catalogo cucina modificato: rileggi la fattura con le unità aggiornate.')
            proposal=json.loads(job['payload']);lines=proposal['lines'];totals=defaultdict(float);reviewed_lines=[]
            if not lines:raise HTTPException(422,'Non sono state lette righe prodotto. Usa la trascrizione manuale.')
            for idx,line in enumerate(lines):
                pid=str(form.get('product_'+str(idx),''))
                if pid=='ignore':reviewed_lines.append(dict(line,ignored=True));continue
                if pid not in allowed:raise HTTPException(422,f'Riga {idx+1}: scegli il prodotto cucina oppure Non cucina.')
                try:value=float(str(form.get('qty_'+str(idx),'')).replace(',','.'))
                except ValueError:raise HTTPException(422,f'Riga {idx+1}: inserisci la quantità nell’unità inventario.')
                if not math.isfinite(value) or value<=0 or value>1e9:raise HTTPException(422,f'Riga {idx+1}: quantità non valida. Resi e rettifiche non vanno inseriti come acquisti positivi.')
                totals[pid]+=value
                if totals[pid]>1e9:raise HTTPException(422,'Quantità complessiva troppo alta.')
                reviewed_lines.append(dict(line,product_id=int(pid),stock_qty=value,inventory_unit=allowed[pid]['unit'],ignored=False))
            week=api['_kitchen_week'](invoice_date)
            # Keep a single reviewed set for this invoice, even if its date/week was corrected.
            old=cur.execute(f'SELECT id FROM kitchen_invoice_reviews WHERE store={ph()} AND doc_id={ph()}',(store,doc_id)).fetchall()
            for row in old:cur.execute(f'DELETE FROM kitchen_invoice_review_lines WHERE review_id={ph()}',(row['id'],))
            cur.execute(f'DELETE FROM kitchen_invoice_reviews WHERE store={ph()} AND doc_id={ph()}',(store,doc_id))
            row=cur.execute(f"INSERT INTO kitchen_invoice_reviews(store,week_start,doc_id,status,updated_by,updated_at) VALUES({','.join([ph()]*6)}) RETURNING id",(store,week,doc_id,'done',user['username'],now())).fetchone()
            rid=row['id']
            for pid,qty in totals.items():cur.execute(f'INSERT INTO kitchen_invoice_review_lines(review_id,product_id,qty,unit) VALUES({ph()},{ph()},{ph()},{ph()})',(rid,int(pid),qty,allowed[pid]['unit']))
            cur.execute(f'UPDATE invoices_docs SET doc_date={ph()},supplier={ph()} WHERE id={ph()} AND store={ph()}',(invoice_date,supplier,doc_id,store))
            proposal.update(reviewed_lines=reviewed_lines,reviewed_by=user['username'],reviewed_at=now(),confirmed_date=invoice_date,confirmed_supplier=supplier)
            cur.execute(f"UPDATE kitchen_invoice_ai_jobs SET state='applied',week_start={ph()},payload={ph()},updated_at={ph()} WHERE doc_id={ph()}",(week,json.dumps(proposal,ensure_ascii=False),now(),doc_id))
            api['_log'](cur,store=store,username=user['username'],action='FATTURA_CUCINA_AI_CONFERMATA',category='FATTURE',name=f'Fattura #{doc_id}: arrivi verificati per il report consumi',delta=0)
        if request.headers.get('accept')=='application/json':return JSONResponse({'redirect':back(store,week,doc_id)})
        return RedirectResponse(back(store,week,doc_id),303)
