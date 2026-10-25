"""Digital reports, reviewed invoice receipts, stock counts and admin control."""
import csv
import hashlib
import io
import json
import math
import os
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError
from fastapi import Request, Form, HTTPException
from fastapi.responses import RedirectResponse, Response, JSONResponse
from .db import connect, using_postgres
from .document_ai import extract_document

KINDS = {'fatture': ('invoices_docs', 'doc_date', 'Fatture'),
    'chiusure': ('closures', 'closure_date', 'Chiusure'),
    'spese-secondarie': ('secondary_expenses', 'expense_date', 'Spese secondarie')}

def today():
    try:
        return datetime.now(ZoneInfo('Europe/Rome')).date()
    except ZoneInfoNotFoundError:
        return date.today()

def ph():
    return '%s' if using_postgres() else '?'

def lock(conn):
    if not using_postgres():
        conn.execute('BEGIN IMMEDIATE')

def suffix():
    return ' FOR UPDATE' if using_postgres() else ''

def number(value, optional=False, nonnegative=False):
    if value is None or str(value).strip() == '':
        if optional:
            return None
        raise HTTPException(422, 'Inserisci una quantità o un importo valido.')
    try:
        result = float(Decimal(str(value).strip().replace(',', '.')))
    except (InvalidOperation, ValueError, OverflowError):
        raise HTTPException(422, 'Numero non valido: usa per esempio 12,50 senza separatore delle migliaia.')
    if not math.isfinite(result) or abs(result) > 1e12 or (nonnegative and result < 0):
        raise HTTPException(422, 'Quantità o importo fuori intervallo.')
    return result

def iso(value):
    try:
        return date.fromisoformat(str(value)).isoformat()
    except (ValueError, TypeError):
        raise HTTPException(422, 'Data non valida.')

def period(days=7, end='', start=''):
    if not 1 <= days <= 30:
        raise HTTPException(422, 'Scegli da 1 a 30 giorni.')
    last = date.fromisoformat(iso(end)) if end else today()
    first = date.fromisoformat(iso(start)) if start else last - timedelta(days=days-1)
    if first > last:
        raise HTTPException(422, 'La data iniziale deve precedere quella finale.')
    return first.isoformat(), last.isoformat()

def timestamp():
    return datetime.now(timezone.utc).isoformat(timespec='microseconds')

def money(value):
    return f'{value:.2f}'.replace('.', ',') if value is not None else 'non indicato'

def csvsafe(value):
    value = str(value if value is not None else '')
    return "'" + value if value.lstrip().startswith(('=', '+', '-', '@', '\t', '\r')) else value

def init_control_db():
    ident = 'SERIAL PRIMARY KEY' if using_postgres() else 'INTEGER PRIMARY KEY AUTOINCREMENT'
    real = 'DOUBLE PRECISION' if using_postgres() else 'REAL'
    with connect() as conn:
        cur = conn.cursor()
        cur.execute(f'''CREATE TABLE IF NOT EXISTS digital_documents (
            kind TEXT NOT NULL, doc_id INTEGER NOT NULL, store TEXT NOT NULL,
            doc_date TEXT NOT NULL, supplier TEXT NOT NULL DEFAULT '', amount {real},
            currency TEXT NOT NULL DEFAULT 'EUR', description TEXT NOT NULL DEFAULT '',
            raw_text TEXT NOT NULL DEFAULT '', lines_json TEXT NOT NULL DEFAULT '[]',
            warnings TEXT NOT NULL DEFAULT '', state TEXT NOT NULL DEFAULT 'bozza',
            revision INTEGER NOT NULL DEFAULT 1, updated_by TEXT NOT NULL, updated_at TEXT NOT NULL,
            PRIMARY KEY(kind, doc_id))''')
        cur.execute(f'''CREATE TABLE IF NOT EXISTS stock_count_sessions (
            id {ident}, store TEXT NOT NULL, area TEXT NOT NULL, counted_at TEXT NOT NULL,
            created_by TEXT NOT NULL, period_days INTEGER NOT NULL, notes TEXT NOT NULL DEFAULT '')''')
        cur.execute(f'''CREATE TABLE IF NOT EXISTS stock_count_lines (
            id {ident}, session_id INTEGER NOT NULL, product_id INTEGER NOT NULL,
            name TEXT NOT NULL, location TEXT NOT NULL, unit TEXT NOT NULL,
            expected {real} NOT NULL, actual {real} NOT NULL, difference {real} NOT NULL)''')
        cur.execute('''CREATE TABLE IF NOT EXISTS inventory_preferences (
            store TEXT PRIMARY KEY, kitchen_days INTEGER NOT NULL DEFAULT 7)''')
        cur.execute('''CREATE TABLE IF NOT EXISTS document_receipts (
            store TEXT NOT NULL, doc_id INTEGER NOT NULL, fingerprint TEXT NOT NULL,
            applied_by TEXT NOT NULL, applied_at TEXT NOT NULL, mode TEXT NOT NULL,
            PRIMARY KEY(store, doc_id), UNIQUE(store, fingerprint))''')
        cur.execute('CREATE INDEX IF NOT EXISTS ix_counts_store_area ON stock_count_sessions(store, area, counted_at)')
        if using_postgres():
            cur.execute('ALTER TABLE order_lines ADD COLUMN IF NOT EXISTS source_product_id INTEGER')
        else:
            columns = {r['name'] for r in cur.execute('PRAGMA table_info(order_lines)').fetchall()}
            if 'source_product_id' not in columns:
                cur.execute('ALTER TABLE order_lines ADD COLUMN source_product_id INTEGER')

    from .document_jobs import init_jobs
    init_jobs()
    from .pos_reports import init_pos
    init_pos()


def install(app, api):
    render, stores = api['render'], api['STORES']

    def auth(request, admin=False, single=False):
        user = api['require_login'](request)
        if not user:
            raise HTTPException(401, 'Accedi al gestionale.')
        if admin and not api['is_admin'](request):
            raise HTTPException(403, 'Solo amministratore.')
        store = api['_effective_store'](request, user)
        if single and store not in stores:
            raise HTTPException(422, 'Seleziona un singolo negozio nel menu in alto.')
        return user, store

    def ctx(user, store, **kw):
        return dict(user=user, stores=stores, brand=store, active_store=store, **kw)

    def source(cur, kind, doc_id, store, data=False):
        if kind not in KINDS:
            raise HTTPException(404, 'Tipo documento non trovato.')
        table, datecol, _ = KINDS[kind]
        fields = 'id, store, filename, CASE WHEN data IS NULL THEN 0 ELSE 1 END AS has_original, ' + datecol + ' AS doc_date'
        fields += ', supplier' if kind == 'fatture' else ", '' AS supplier"
        if data:
            fields += ', data, content_type'
        sql, params = f'SELECT {fields} FROM {table} WHERE id={ph()}', [doc_id]
        if store != 'ALL':
            sql += f' AND store={ph()}'
            params.append(store)
        row = cur.execute(sql, tuple(params)).fetchone()
        if not row:
            raise HTTPException(404, 'Documento non trovato in questo negozio.')
        return dict(row)

    def digital(cur, kind, row):
        result = cur.execute(f'SELECT * FROM digital_documents WHERE kind={ph()} AND doc_id={ph()}', (kind, row['id'])).fetchone()
        result = dict(result) if result else dict(kind=kind, doc_id=row['id'], store=row['store'],
            doc_date=str(row['doc_date']), supplier=row['supplier'], amount=None, currency='EUR',
            description='', raw_text='', lines_json='[]', warnings='', state='non letto', revision=0)
        result['lines'] = json.loads(result['lines_json'])
        return result

    def documents(cur, store, kind, start, end):
        result = []
        for key in (KINDS if kind == 'tutti' else [kind]):
            if key not in KINDS:
                raise HTTPException(422, 'Categoria non valida.')
            table, datecol, label = KINDS[key]
            sql = f'''SELECT d.id,d.store,d.filename,d.{datecol} AS doc_date,
                {'d.supplier' if key == 'fatture' else "''"} AS supplier
                FROM {table} d LEFT JOIN digital_documents x ON x.kind={ph()} AND x.doc_id=d.id
                WHERE COALESCE(x.doc_date,CAST(d.{datecol} AS TEXT))>={ph()}
                AND COALESCE(x.doc_date,CAST(d.{datecol} AS TEXT))<={ph()}'''
            params = [key,start,end]
            if store != 'ALL':
                sql += f' AND d.store={ph()}'
                params.append(store)
            for r in cur.execute(sql,tuple(params)).fetchall():
                d = digital(cur,key,r)
                d.update(filename=r['filename'],label=label)
                result.append(d)
        return sorted(result,key=lambda d:(d['doc_date'],d['store'],d['kind'],d['doc_id']))

    @app.on_event('startup')
    def startup_control():
        from .startup_guard import needs_schema_sync
        if needs_schema_sync():
            init_control_db()
        # Il vecchio OCR automatico non viene piu utilizzato: la scansione
        # storica delle fatture impiegava molte query per ogni avvio Render.
        # Riattivabile esplicitamente solo se necessario per manutenzione.
        if os.getenv('ENABLE_LEGACY_DOCUMENT_OCR') == '1':
            from .document_jobs import enqueue_history, start_worker
            enqueue_history()
            start_worker()

    @app.on_event('shutdown')
    def shutdown_control():
        from .document_jobs import stop_worker
        stop_worker()

    @app.get('/archivio-digitale/{kind}/stato')
    def archive_status(request:Request,kind:str):
        user,store=auth(request)
        if kind not in KINDS:
            raise HTTPException(404,'Archivio non trovato.')
        from .document_jobs import archive_state
        return JSONResponse(archive_state(store,kind))

    @app.get('/admin/digitalizzazione')
    def history_status(request:Request):
        user,store=auth(request,admin=True)
        with connect() as conn:
            cur=conn.cursor();sql='SELECT * FROM document_ai_jobs WHERE 1=1';params=[]
            if store!='ALL':sql+=f' AND store={ph()}';params=[store]
            rows=cur.execute(sql+' ORDER BY updated_at DESC',tuple(params)).fetchall()
        states={state:sum(1 for r in rows if r['state']==state) for state in ['pending','working','done','error']}
        return render('digital_history.html',**ctx(user,store,rows=rows,states=states,ai_ready=bool(os.getenv('OPENAI_API_KEY'))))

    @app.post('/admin/digitalizzazione/accoda')
    def queue_history(request:Request):
        user,store=auth(request,admin=True)
        from .document_jobs import enqueue_history
        enqueue_history()
        return RedirectResponse('/admin/digitalizzazione',status_code=303)

    @app.get('/report-documenti')
    def report(request: Request, kind: str = 'tutti', days: int = 7, start: str = '', end: str = '', download: str = ''):
        user,store = auth(request,admin=True)
        first,last = period(days,end,start)
        with connect() as conn:
            rows = documents(conn.cursor(),store,kind,first,last)
        totals = {}
        text = [f'REPORT {KINDS[kind][2] if kind in KINDS else "DOCUMENTI"} — {stores.get(store,"Tutti i negozi")}',f'Dal {first} al {last}','']
        missing = 0
        for r in rows:
            status = 'VERIFICATO' if r['state']=='verificato' else 'DA VERIFICARE'
            text.append(f'{r["doc_date"]} | {r["label"]} #{r["doc_id"]} | {stores.get(r["store"],r["store"])} | {r["supplier"] or "Provenienza da verificare"} | {money(r["amount"])} {r["currency"]} | {status}')
            if r['description']:
                text.append(r['description'])
            for ln in r['lines']:
                text.append(f'  - {ln.get("description","")}: {ln.get("qty") if ln.get("qty") is not None else "quantità non indicata"} {ln.get("unit") or ""} | {money(ln.get("amount"))}')
            if r['warnings']:
                text.append('Note di lettura: '+r['warnings'])
            if r['state']=='verificato' and r['amount'] is not None:
                key = (r['label'],r['currency'])
                totals[key] = totals.get(key,Decimal('0'))+Decimal(str(r['amount']))
            else:
                missing += 1
            text.append('')
        for (label,currency),total in totals.items():
            text.append(f'Totale {label} verificato: {money(total)} {currency}')
        text.append(f'Documenti: {len(rows)}. Esclusi dai totali perché incompleti o da verificare: {missing}.')
        body = '\n'.join(text)
        if download=='txt':
            return Response(body.encode('utf-8-sig'),media_type='text/plain; charset=utf-8',headers={'Content-Disposition':f'attachment; filename="report-{first}-{last}.txt"'})
        if download=='csv':
            out = io.StringIO(newline='')
            writer = csv.writer(out,delimiter=';')
            writer.writerow(['Data','Tipo','ID','Negozio','Provenienza','Importo','Valuta','Descrizione','Stato','Prodotti'])
            for r in rows:
                writer.writerow([csvsafe(v) for v in [r['doc_date'],r['label'],r['doc_id'],stores.get(r['store']),r['supplier'],money(r['amount']) if r['amount'] is not None else '',r['currency'],r['description'],r['state'],'\n'.join(f'{l.get("description","")} | {l.get("qty")} {l.get("unit") or ""}' for l in r['lines'])]])
            return Response(out.getvalue().encode('utf-8-sig'),media_type='text/csv; charset=utf-8',headers={'Content-Disposition':f'attachment; filename="report-{first}-{last}.csv"'})
        return render('digital_report.html',**ctx(user,store,rows=rows,kinds=KINDS,kind=kind,days=days,start=first,end=last,report_text=body,ai_ready=bool(os.getenv('OPENAI_API_KEY'))))

    @app.get('/documenti/{kind}/{doc_id}')
    def document_page(request: Request, kind: str, doc_id: int, message: str = ''):
        user,store = auth(request)
        with connect() as conn:
            cur = conn.cursor()
            src = source(cur,kind,doc_id,store)
            doc = digital(cur,kind,src)
            job = cur.execute(f'SELECT * FROM document_ai_jobs WHERE kind={ph()} AND doc_id={ph()}',(kind,doc_id)).fetchone()
            ai_comparison = json.loads(job['extracted_json']) if user['role']=='admin' and job and job['extracted_json'] else None
            products = cur.execute(f'SELECT id,name,category,area,location,unit FROM products WHERE store={ph()} ORDER BY area,name,location',(src['store'],)).fetchall() if kind=='fatture' else []
            applied = cur.execute(f'SELECT id FROM invoice_imports WHERE store={ph()} AND invoice_doc_id={ph()}',(src['store'],doc_id)).fetchone() if kind=='fatture' else None
        for ln in doc['lines']:
            matches = [p for p in products if p['name'].strip().casefold()==ln.get('description','').strip().casefold()]
            if len(matches)==1 and str(matches[0]['unit']).strip().casefold()==str(ln.get('unit') or '').strip().casefold():
                ln.setdefault('product_id',matches[0]['id'])
                ln.setdefault('stock_qty',ln.get('qty'))
        return render('digital_document.html',**ctx(user,store,doc=doc,src=src,kind=kind,label=KINDS[kind][2],products=products,applied=bool(applied),message=message,ai_ready=bool(os.getenv('OPENAI_API_KEY')),job=job,ai_comparison=ai_comparison,can_correct=doc['revision']>0))

    def persist(cur, kind, src, d, revision, username):
        values = (kind,src['id'],src['store'],d['doc_date'],d['supplier'],d['amount'],d['currency'],
            d['description'],d['raw_text'],json.dumps(d['lines'],ensure_ascii=False),d['warnings'],d['state'],revision+1,username,timestamp())
        cols = 'doc_date,supplier,amount,currency,description,raw_text,lines_json,warnings,state,revision,updated_by,updated_at'
        updates = ','.join(f'{c}=excluded.{c}' for c in cols.split(','))
        cur.execute(f'''INSERT INTO digital_documents(kind,doc_id,store,{cols}) VALUES({','.join([ph()]*15)})
            ON CONFLICT(kind,doc_id) DO UPDATE SET {updates}''',values)

    @app.post('/documenti/{kind}/{doc_id}/analizza')
    def analyze(request: Request, kind: str, doc_id: int):
        user,store = auth(request,admin=True)
        with connect() as conn:
            cur = conn.cursor()
            src = source(cur,kind,doc_id,store,data=True)
            old = digital(cur,kind,src)
        if old['state']=='verificato':
            return JSONResponse({'ok':True,'message':'Già verificato: dati preservati.'})
        try:
            result = extract_document(src['data'],KINDS[kind][2])
        except ValueError as exc:
            return JSONResponse({'ok':False,'message':str(exc)},status_code=422)
        dt = result.get('date') or str(src['doc_date'])
        warnings = result.get('warnings') or []
        if not result.get('date'):
            warnings.append('Data non letta: proposta la data di archivio, da verificare.')
        try:
            dt = iso(dt)
        except HTTPException:
            dt = str(src['doc_date'])
            warnings.append('Data letta non valida: verificare la data di archivio.')
        lines = [dict(description=str(l.get('description') or ''),qty=number(l.get('qty'),optional=True),
            unit=str(l.get('unit') or ''),amount=number(l.get('amount'),optional=True)) for l in result['lines']]
        d = dict(doc_date=dt,supplier=result.get('supplier') or src['supplier'],amount=number(result.get('amount'),optional=True),
            currency=str(result.get('currency') or 'EUR').upper(),description=result.get('description') or '',raw_text=result.get('text') or '',
            lines=lines,warnings='\n'.join(warnings),state='bozza')
        with connect() as conn:
            lock(conn)
            cur = conn.cursor()
            source(cur,kind,doc_id,store)
            current = cur.execute(f'SELECT revision FROM digital_documents WHERE kind={ph()} AND doc_id={ph()}'+suffix(),(kind,doc_id)).fetchone()
            if (current['revision'] if current else 0)!=old['revision']:
                return JSONResponse({'ok':False,'message':'Documento modificato durante la lettura: ricarica la pagina.'},status_code=409)
            persist(cur,kind,src,d,old['revision'],user['username'])
            api['_log'](cur,store=src['store'],username=user['username'],action='DOC_AI',category=kind,name=f'Documento #{doc_id}: bozza da verificare',delta=0)
        return JSONResponse({'ok':True,'message':'Lettura completata: controlla e conferma i dati.'})

    @app.post('/documenti/{kind}/{doc_id}/riprova')
    def retry_document(request: Request, kind: str, doc_id: int):
        user,store=auth(request)
        with connect() as conn:
            src=source(conn.cursor(),kind,doc_id,store)
            if not src['has_original']:
                raise HTTPException(422,'Registrazione digitale senza foto.')
        from .document_jobs import enqueue
        enqueue(kind,doc_id,src['store'],user['username'],retry=True)
        return RedirectResponse(f'/documenti/{kind}/{doc_id}',status_code=303)

    @app.get('/documenti/{kind}/{doc_id}/stato')
    def document_status(request: Request, kind: str, doc_id: int):
        user,store=auth(request)
        with connect() as conn:
            cur=conn.cursor();source(cur,kind,doc_id,store)
            job=cur.execute(f'SELECT state,error FROM document_ai_jobs WHERE kind={ph()} AND doc_id={ph()}',(kind,doc_id)).fetchone()
            row=cur.execute(f'SELECT revision FROM digital_documents WHERE kind={ph()} AND doc_id={ph()}',(kind,doc_id)).fetchone()
        return JSONResponse(dict(state=job['state'] if job else 'none',error=job['error'] if job else '',revision=row['revision'] if row else 0,configured=bool(os.getenv('OPENAI_API_KEY'))))

    @app.get('/registrazione-digitale/{kind}')
    def manual_page(request: Request,kind: str):
        user,store=auth(request,admin=True,single=True)
        if kind not in KINDS:
            raise HTTPException(404,'Categoria non valida.')
        return render('digital_manual.html',**ctx(user,store,kind=kind,label=KINDS[kind][2],today=today().isoformat()))

    @app.post('/registrazione-digitale/{kind}')
    async def manual_create(request: Request,kind: str):
        user,store=auth(request,admin=True,single=True)
        if kind not in KINDS:
            raise HTTPException(404,'Categoria non valida.')
        form=await request.form()
        dt=iso(form.get('doc_date'));supplier=str(form.get('supplier') or '').strip()
        amount=number(form.get('amount'))
        if not supplier:
            raise HTTPException(422,'Inserisci la provenienza.')
        description=str(form.get('description') or '')
        table,datecol,_=KINDS[kind]
        with connect() as conn:
            cur=conn.cursor()
            cols=f'store,{datecol},uploaded_by,filename'
            vals=[store,dt,user['username'],'registrazione-digitale']
            if kind=='fatture':
                cols+=',supplier';vals.append(supplier)
            cur.execute(f'INSERT INTO {table}({cols}) VALUES({",".join([ph()]*len(vals))}) RETURNING id',tuple(vals))
            doc_id=cur.fetchone()['id']
            src=dict(id=doc_id,store=store)
            d=dict(doc_date=dt,supplier=supplier,amount=amount,currency='EUR',description=description,raw_text=description,lines=[],warnings='',state='verificato')
            persist(cur,kind,src,d,0,user['username'])
            api['_log'](cur,store=store,username=user['username'],action='DOC_MANUALE',category=kind,name=f'Registrazione digitale #{doc_id}',delta=0)
        return RedirectResponse(f'/documenti/{kind}/{doc_id}',status_code=303)

    @app.post('/documenti/{kind}/{doc_id}/salva')
    async def save_document(request: Request, kind: str, doc_id: int):
        user,store = auth(request)
        form = await request.form()
        dt,supplier = iso(form.get('doc_date')),str(form.get('supplier') or '').strip()
        amount = number(form.get('amount'),optional=True)
        currency = str(form.get('currency') or 'EUR').strip().upper()
        if len(currency)!=3 or not currency.isalpha():
            raise HTTPException(422,'Usa un codice valuta di tre lettere, per esempio EUR.')
        state = 'verificato' if form.get('confirm') else 'bozza'
        if state=='verificato' and (not supplier or amount is None):
            raise HTTPException(422,'Per verificare servono provenienza e importo: non inserire zero per un dato sconosciuto.')
        lines = []
        for idx,name in enumerate(form.getlist('line_description')):
            if not str(name).strip():
                continue
            def val(key):
                values = form.getlist(key)
                return values[idx] if idx<len(values) else ''
            pid = str(val('product_id') or '')
            lines.append(dict(description=str(name).strip(),qty=number(val('line_qty'),optional=True),unit=str(val('line_unit')),
                amount=number(val('line_amount'),optional=True),product_id=int(pid) if pid.isdigit() else None,
                ignore=pid=='ignore',stock_qty=number(val('stock_qty'),optional=True)))
        with connect() as conn:
            lock(conn)
            cur = conn.cursor()
            src = source(cur,kind,doc_id,store)
            cur.execute(f'SELECT revision FROM digital_documents WHERE kind={ph()} AND doc_id={ph()}'+suffix(),(kind,doc_id))
            old = digital(cur,kind,src)
            if user['role']!='admin' and old['revision']==0:
                raise HTTPException(403,'Puoi correggere una trascrizione esistente; la prima compilazione viene eseguita dall’AI.')
            if str(form.get('revision'))!=str(old['revision']):
                raise HTTPException(409,'Documento modificato da un altro utente. Ricarica prima di salvare.')
            applied = cur.execute(f'SELECT id FROM invoice_imports WHERE store={ph()} AND invoice_doc_id={ph()}',(src['store'],doc_id)).fetchone() if kind=='fatture' else None
            if user['role']!='admin':
                # Staff corrects extracted fields; product/stock associations stay under admin control.
                for idx,line in enumerate(lines):
                    previous = old['lines'][idx] if idx<len(old['lines']) else {}
                    for field in ('product_id','ignore','stock_qty'):
                        line[field] = previous.get(field)
            if applied:
                lines = old['lines']
            d = dict(doc_date=dt,supplier=supplier,amount=amount,currency=currency,description=str(form.get('description') or ''),
                raw_text=str(form.get('raw_text') or ''),lines=lines,warnings=str(form.get('warnings') or ''),state=state)
            cur.execute(f'INSERT INTO digital_document_versions(kind,doc_id,store,changed_by,changed_at,before_json,after_json) VALUES({",".join([ph()]*7)})',
                (kind,doc_id,src['store'],user['username'],timestamp(),json.dumps(old,ensure_ascii=False,default=str),json.dumps(d,ensure_ascii=False)))
            persist(cur,kind,src,d,old['revision'],user['username'])
            table,datecol,_ = KINDS[kind]
            if state=='verificato':
                if kind=='fatture':
                    cur.execute(f'UPDATE {table} SET {datecol}={ph()},supplier={ph()} WHERE id={ph()}',(dt,supplier,doc_id))
                else:
                    cur.execute(f'UPDATE {table} SET {datecol}={ph()} WHERE id={ph()}',(dt,doc_id))
            api['_log'](cur,store=src['store'],username=user['username'],action='DOC_VERIFICA' if state=='verificato' else 'DOC_BOZZA',category=kind,name=f'Documento #{doc_id}',delta=0)
        return RedirectResponse(f'/documenti/{kind}/{doc_id}?message=Dati+salvati',status_code=303)

    @app.post('/documenti/fatture/{doc_id}/carica-inventario')
    async def apply_invoice(request: Request, doc_id: int):
        user,store = auth(request,admin=True)
        form = await request.form()
        mode = str(form.get('mode') or '')
        if mode not in ('carica','gia-caricata'):
            raise HTTPException(422,'Indica se la merce deve essere caricata o è già entrata con un ordine.')
        with connect() as conn:
            lock(conn)
            cur = conn.cursor()
            src = source(cur,'fatture',doc_id,store,data=True)
            cur.execute(f'SELECT revision FROM digital_documents WHERE kind={ph()} AND doc_id={ph()}'+suffix(),('fatture',doc_id))
            d = digital(cur,'fatture',src)
            if d['state']!='verificato' or not d['lines']:
                raise HTTPException(422,'Verifica prima i dati e inserisci le righe prodotto.')
            if cur.execute(f'SELECT id FROM invoice_imports WHERE store={ph()} AND invoice_doc_id={ph()}',(src['store'],doc_id)).fetchone():
                return RedirectResponse(f'/documenti/fatture/{doc_id}?message=Fattura+già+registrata',status_code=303)
            if cur.execute(f'''SELECT d.id FROM invoices_docs d JOIN invoice_imports i ON i.invoice_doc_id=d.id AND i.store=d.store
                WHERE d.store={ph()} AND d.data={ph()} AND d.id<>{ph()}''', (src['store'],src['data'],doc_id)).fetchone():
                raise HTTPException(409,'Questo file risulta già importato da una fattura precedente.')
            fingerprint = hashlib.sha256(bytes(src['data'] or b'')).hexdigest()
            if cur.execute(f'SELECT doc_id FROM document_receipts WHERE store={ph()} AND fingerprint={ph()}',(src['store'],fingerprint)).fetchone():
                raise HTTPException(409,'Questo stesso file è già stato registrato con un’altra fattura.')
            mapped = []
            for line in d['lines']:
                if line.get('ignore'):
                    continue
                if not line.get('product_id') or line.get('stock_qty') is None:
                    raise HTTPException(422,'Associa ogni riga a un prodotto con la quantità in unità inventario, oppure indica Non inventario.')
                p = cur.execute(f'SELECT * FROM products WHERE id={ph()} AND store={ph()}'+suffix(),(line['product_id'],src['store'])).fetchone()
                if not p:
                    raise HTTPException(422,'Prodotto non disponibile in questo negozio.')
                mapped.append((line,p,number(line['stock_qty'])))
            if not mapped:
                raise HTTPException(422,'Nessuna riga di inventario selezionata.')
            cur.execute(f'INSERT INTO document_receipts(store,doc_id,fingerprint,applied_by,applied_at,mode) VALUES({",".join([ph()]*6)})',
                (src['store'],doc_id,fingerprint,user['username'],timestamp(),mode))
            cur.execute(f'INSERT INTO invoice_imports(store,invoice_doc_id,supplier,doc_date,area,created_by) VALUES({",".join([ph()]*6)}) RETURNING id',
                (src['store'],doc_id,d['supplier'],d['doc_date'],'mista',user['username']))
            import_id = cur.fetchone()['id']
            for line,p,qty in mapped:
                if mode=='carica':
                    cur.execute(f'UPDATE products SET qty=qty+{ph()},updated_at={api["_now"]()} WHERE id={ph()} AND qty+{ph()}>=0',(qty,p['id'],qty))
                    if cur.rowcount!=1:
                        raise HTTPException(409,'Lo storno renderebbe negativa una giacenza. Verifica le quantità.')
                cur.execute(f'INSERT INTO invoice_import_lines(import_id,raw_name,category,qty,unit,product_id,product_name) VALUES({",".join([ph()]*7)})',
                    (import_id,line['description'],p['category'],qty,p['unit'],p['id'],p['name']))
                api['_log'](cur,store=src['store'],username=user['username'],action='IMPORT_FATTURA' if mode=='carica' else 'FATTURA_RICONCILIATA',
                    category=p['category'],name=f'{p["name"]} | {p["location"]} | fattura #{doc_id}',delta=qty if mode=='carica' else 0)
        return RedirectResponse(f'/documenti/fatture/{doc_id}?message=Inventario+riconciliato',status_code=303)

    @app.get('/inventario-controllo')
    def stock_control(request: Request, area: str = 'bibite', days: int = 0, store: str = ''):
        user,current_store = auth(request)
        if store and api['is_admin'](request) and store in stores:
            request.session['active_store'] = store
            current_store = store
        store = current_store
        if store not in stores:
            raise HTTPException(422,'Seleziona un negozio nel menu in alto, oppure apri il conteggio dalla riga inventario del negozio.')
        if area not in ('bibite','prodotti'):
            raise HTTPException(422,'Area non valida.')
        with connect() as conn:
            cur = conn.cursor()
            pref = cur.execute(f'SELECT kitchen_days FROM inventory_preferences WHERE store={ph()}',(store,)).fetchone()
            days = 1 if area=='bibite' else (days or (pref['kitchen_days'] if pref else 7))
            first,last = period(days)
            products = cur.execute(f'SELECT * FROM products WHERE store={ph()} AND area={ph()} ORDER BY category,name,location',(store,area)).fetchall()
            sessions = cur.execute(f'SELECT * FROM stock_count_sessions WHERE store={ph()} AND area={ph()} ORDER BY counted_at DESC LIMIT 30',(store,area)).fetchall()
            counted = cur.execute(f'''SELECT l.product_id,MAX(s.counted_at) AS last_count FROM stock_count_lines l
                JOIN stock_count_sessions s ON s.id=l.session_id WHERE s.store={ph()} AND s.area={ph()} GROUP BY l.product_id''',(store,area)).fetchall()
            last_counts = {r['product_id']:r['last_count'] for r in counted}
            for product in products:
                product['last_count'] = last_counts.get(product['id'])
                product['count_due'] = not product['last_count'] or product['last_count'][:10] <= (today()-timedelta(days=days)).isoformat()
            history = []
            for s in sessions:
                sl = cur.execute(f'SELECT * FROM stock_count_lines WHERE session_id={ph()} ORDER BY name,location',(s['id'],)).fetchall()
                history.append(dict(s,lines=sl))
            moves = cur.execute(f"SELECT * FROM logs WHERE store={ph()} AND action IN ('MOVE','CONSUMO','RETTIFICA','CONTEGGIO') AND CAST(ts AS TEXT)>={ph()} ORDER BY ts DESC,id DESC LIMIT 150",(store,first)).fetchall()
            invoices = documents(cur,store,'fatture',first,last) if user['role']=='admin' else []
            applied = {r['invoice_doc_id'] for r in cur.execute(f'SELECT invoice_doc_id FROM invoice_imports WHERE store={ph()}',(store,)).fetchall()}
        due = any(p['count_due'] for p in products)
        return render('stock_control.html',**ctx(user,store,area=area,days=days,start=first,end=last,products=products,
            history=history,moves=moves,invoices=invoices,applied=applied,due=due))

    @app.post('/inventario-controllo/preferenze')
    def preferences(request: Request, kitchen_days: int = Form(...)):
        user,store = auth(request,admin=True,single=True)
        period(kitchen_days)
        with connect() as conn:
            conn.cursor().execute(f'INSERT INTO inventory_preferences(store,kitchen_days) VALUES({ph()},{ph()}) ON CONFLICT(store) DO UPDATE SET kitchen_days=excluded.kitchen_days',(store,kitchen_days))
        return RedirectResponse('/inventario-controllo?area=prodotti',status_code=303)

    @app.post('/inventario-controllo/conteggio')
    async def count(request: Request):
        user,store = auth(request,single=True)
        form = await request.form()
        area = str(form.get('area'))
        if area not in ('bibite','prodotti'):
            raise HTTPException(422,'Area non valida.')
        try:
            days = int(form.get('days') or 7) if area=='prodotti' else 1
        except ValueError:
            raise HTTPException(422,'Periodo non valido.')
        period(days)
        values = [(int(key[7:]),number(val,nonnegative=True)) for key,val in form.items()
                  if key.startswith('actual_') and key[7:].isdigit() and str(val).strip()]
        if not values:
            raise HTTPException(422,'Inserisci almeno una quantità contata. Zero significa assente; vuoto significa non contato.')
        with connect() as conn:
            lock(conn)
            cur = conn.cursor()
            planned = []
            for pid,actual in sorted(values):
                p = cur.execute(f'SELECT * FROM products WHERE id={ph()} AND store={ph()} AND area={ph()}'+suffix(),(pid,store,area)).fetchone()
                if not p:
                    raise HTTPException(409,'Prodotti modificati: ricarica il conteggio.')
                expected = number(form.get(f'expected_{pid}'))
                if abs(float(p['qty'])-expected)>1e-8:
                    raise HTTPException(409,f'La giacenza di {p["name"]} è cambiata durante il conteggio. Ricarica e verifica prima di salvare.')
                planned.append((p,actual,expected))
            cur.execute(f'INSERT INTO stock_count_sessions(store,area,counted_at,created_by,period_days,notes) VALUES({",".join([ph()]*6)}) RETURNING id',
                (store,area,timestamp(),user['username'],days,str(form.get('notes') or '')))
            sid = cur.fetchone()['id']
            for p,actual,expected in planned:
                diff = actual-expected
                cur.execute(f'INSERT INTO stock_count_lines(session_id,product_id,name,location,unit,expected,actual,difference) VALUES({",".join([ph()]*8)})',
                    (sid,p['id'],p['name'],p['location'],p['unit'],expected,actual,diff))
                cur.execute(f'UPDATE products SET qty={ph()},updated_at={api["_now"]()} WHERE id={ph()}',(actual,p['id']))
                api['_log'](cur,store=store,username=user['username'],action='CONTEGGIO',category=p['category'],
                    name=f'{p["name"]} | {p["location"]}: previsto {expected:g}, contato {actual:g} | conteggio #{sid}',delta=diff)
        return RedirectResponse(f'/inventario-controllo?area={area}',status_code=303)

    @app.post('/inventario-controllo/movimento')
    def movement(request: Request, product_id: int = Form(...), qty: str = Form(...), reason: str = Form(...), action: str = Form(...), target: str = Form('')):
        user,store = auth(request,single=True)
        amount = number(qty,nonnegative=True)
        if amount<=0 or not reason.strip() or action not in ('trasferisci','consumo','carico','rettifica-scarico'):
            raise HTTPException(422,'Scegli un movimento, una quantità positiva e una motivazione.')
        with connect() as conn:
            lock(conn)
            cur = conn.cursor()
            p = cur.execute(f'SELECT * FROM products WHERE id={ph()} AND store={ph()}'+suffix(),(product_id,store)).fetchone()
            if not p:
                raise HTTPException(404,'Prodotto non trovato.')
            delta = amount if action=='carico' else -amount
            cur.execute(f'UPDATE products SET qty=qty+{ph()},updated_at={api["_now"]()} WHERE id={ph()} AND qty+{ph()}>=0',(delta,product_id,delta))
            if cur.rowcount!=1:
                raise HTTPException(409,'Quantità superiore alla giacenza disponibile.')
            name = f'{p["name"]} | {p["location"]}'
            if action=='trasferisci':
                target = target.strip().upper()
                if not target or len(target)>80 or target==p['location'].upper():
                    raise HTTPException(422,'Scegli una posizione di destinazione diversa.')
                cur.execute(f'''INSERT INTO products(store,category,subgroup,name,area,location,unit,qty,min_qty,supplier,category_color)
                    VALUES({','.join([ph()]*11)}) ON CONFLICT(store,area,category,name,location) DO UPDATE SET qty=products.qty+excluded.qty''',
                    (store,p['category'],p.get('subgroup') or '',p['name'],p['area'],target,p['unit'],amount,0,p.get('supplier') or '',p.get('category_color') or '#64748b'))
                name += f' → {target}'
            api['_log'](cur,store=store,username=user['username'],action='MOVE' if action=='trasferisci' else ('CONSUMO' if action=='consumo' else 'RETTIFICA'),
                category=p['category'],name=name+' | '+reason.strip(),delta=amount if action=='trasferisci' else delta)
        return RedirectResponse(f'/inventario-controllo?area={p["area"]}',status_code=303)

    @app.post('/ordinati/annulla')
    def cancel_order(request: Request, order_id: int = Form(...)):
        user,store = auth(request,admin=True)
        with connect() as conn:
            lock(conn)
            cur = conn.cursor()
            sql = f"SELECT * FROM orders WHERE id={ph()} AND status='in_corso'"
            params = [order_id]
            if store != 'ALL':
                sql += f' AND store={ph()}'
                params.append(store)
            order = cur.execute(sql+suffix(),tuple(params)).fetchone()
            if not order:
                raise HTTPException(409,'Ordine già ricevuto, annullato o non disponibile nel negozio selezionato.')
            cur.execute(f"UPDATE orders SET status='annullato',closed_at={api['_now']()} WHERE id={ph()} AND status='in_corso'",(order_id,))
            if cur.rowcount != 1:
                raise HTTPException(409,'Stato ordine cambiato. Ricarica la pagina.')
            api['_log'](cur,store=order['store'],username=user['username'],action='ORDER_ANNULLATO',category=order['supplier'],name=f'Ordine #{order_id} annullato senza modificare le giacenze',delta=0)
        return RedirectResponse('/ordinati',status_code=303)

    @app.get('/admin/controllo')
    def dashboard(request: Request, month: str = ''):
        user,store = auth(request,admin=True)
        try:
            anchor = date.fromisoformat((month or today().strftime('%Y-%m'))+'-01')
        except ValueError:
            raise HTTPException(422,'Mese non valido.')
        first = anchor.isoformat()
        next_month = (anchor.replace(day=28)+timedelta(days=4)).replace(day=1)
        last = (next_month-timedelta(days=1)).isoformat()
        p = ph()
        def scoped(alias=''):
            return ('',[]) if store=='ALL' else (f' AND {alias}store={p}',[store])
        with connect() as conn:
            cur = conn.cursor()
            scope,params = scoped()
            pending = cur.execute("SELECT * FROM orders WHERE status='in_corso'"+scope+' ORDER BY ts DESC',tuple(params)).fetchall()
            scope,params = scoped('o.')
            partial = cur.execute(f'''SELECT o.id,o.store,o.supplier,l.name,l.qty,l.received_qty FROM orders o JOIN order_lines l ON l.order_id=o.id
                WHERE o.status='chiuso' AND (l.is_missing=1 OR l.received_qty<l.qty) AND CAST(COALESCE(o.closed_at,o.ts) AS TEXT)>={p}
                AND CAST(COALESCE(o.closed_at,o.ts) AS TEXT)<{p}'''+scope+' ORDER BY o.id DESC',tuple([first,next_month.isoformat()]+params)).fetchall()
            docs = documents(cur,store,'tutti',first,last)
            scope,params = scoped()
            logs = cur.execute(f'SELECT * FROM logs WHERE CAST(ts AS TEXT)>={p} AND CAST(ts AS TEXT)<{p}'+scope+' ORDER BY ts DESC,id DESC LIMIT 300',tuple([first,next_month.isoformat()]+params)).fetchall()
            counts = cur.execute(f'SELECT * FROM stock_count_sessions WHERE counted_at>={p} AND counted_at<{p}'+scope+' ORDER BY counted_at DESC',tuple([first,next_month.isoformat()]+params)).fetchall()
            discrepancies = []
            for c in counts:
                for ln in cur.execute(f'SELECT * FROM stock_count_lines WHERE session_id={p} AND difference<>0',(c['id'],)).fetchall():
                    discrepancies.append(dict(ln,store=c['store'],counted_at=c['counted_at'],area=c['area']))
            low = cur.execute('SELECT * FROM products WHERE qty<=min_qty'+scope+' ORDER BY store,area,name',tuple(params)).fetchall()
            scope,params = scoped('r.')
            # Sum each stored own value exactly once, matching the existing report model.
            sales = cur.execute(f'''SELECT r.store,g.name,g.quantity,g.amount FROM sales_report_groups g
                JOIN sales_report_periods r ON r.id=g.period_id WHERE r.month_key={p}
                AND (COALESCE(g.quantity,0)<>0 OR COALESCE(g.amount,0)<>0)'''+scope+' ORDER BY r.store,g.name',tuple([anchor.strftime('%Y-%m')]+params)).fetchall()
            scope,params = scoped()
            stock = cur.execute('SELECT store,area,name,location,unit,qty,min_qty FROM products WHERE 1=1'+scope+' ORDER BY store,area,name,location',tuple(params)).fetchall()
            imported = {(r['store'],r['invoice_doc_id']) for r in cur.execute('SELECT store,invoice_doc_id FROM invoice_imports WHERE 1=1'+scope,tuple(params)).fetchall()}
            unreconciled = [d for d in docs if d['kind']=='fatture' and (d['store'],d['doc_id']) not in imported]
            last_counts = cur.execute(f"""SELECT l.product_id,MAX(c.counted_at) AS counted_at FROM stock_count_lines l
                JOIN stock_count_sessions c ON c.id=l.session_id GROUP BY l.product_id""").fetchall()
            last_map = {r['product_id']:r['counted_at'] for r in last_counts}
            prefs = {r['store']:r['kitchen_days'] for r in cur.execute('SELECT * FROM inventory_preferences').fetchall()}
            count_status = {}
            for product in cur.execute('SELECT id,store,area FROM products WHERE 1=1'+scope,tuple(params)).fetchall():
                days = 1 if product['area']=='bibite' else prefs.get(product['store'],7)
                key = (product['store'],product['area'])
                status = count_status.setdefault(key,dict(store=product['store'],area=product['area'],days=days,total=0,due=0))
                status['total'] += 1
                last_count = last_map.get(product['id'])
                if not last_count or last_count[:10] <= (today()-timedelta(days=days)).isoformat():
                    status['due'] += 1

        with connect() as pos_conn:
            pos_cur=pos_conn.cursor()
            sql=f"SELECT * FROM pos_import_batches WHERE kind='products' AND status='active' AND start_date>={ph()} AND end_date<={ph()}"
            args=[first,last]
            if store!='ALL':sql+=f' AND store={ph()}';args.append(store)
            pos_batches=pos_cur.execute(sql,tuple(args)).fetchall()
            if pos_batches:
                sales=[]
                for pos_batch in pos_batches:
                    for item in json.loads(pos_batch['parsed_json'])['rows']:
                        sales.append(dict(store=pos_batch['store'],name=item['name'],quantity=item['quantity'],amount=item['gross']))
        return render('admin_control.html',**ctx(user,store,month=anchor.strftime('%Y-%m'),pending=pending,partial=partial,
            docs=docs,unverified=[r for r in docs if r['state']!='verificato'],logs=logs,discrepancies=discrepancies,low=low,
            count_status=list(count_status.values()),unreconciled=unreconciled,sales=sales,sales_total=money(sum(Decimal(str(r['amount'] or 0)) for r in sales)),
            sales_qty=sum(float(r['quantity'] or 0) for r in sales),stock=stock))
