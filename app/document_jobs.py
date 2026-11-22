"""Durable automatic transcription queue for new and historical photo documents."""
import json
import logging
import os
import threading
from datetime import datetime, timedelta, timezone
from fastapi import HTTPException
from .document_review import available_document
from .db import connect, using_postgres
from .document_ai import extract_document

KINDS={'fatture':('invoices_docs','doc_date','Fatture'),'chiusure':('closures','closure_date','Chiusure'),
       'spese-secondarie':('secondary_expenses','expense_date','Spese secondarie')}
_wakeup=threading.Event()
_stop=threading.Event()
_thread=None
HISTORY_START='2026-11-01'
HISTORY_END='2026-12-01'
NEW_UPLOADS_SINCE='2026-11-21T18:55:20.629613+00:00'


def p():
    return '%s' if using_postgres() else '?'


def stamp():
    return datetime.now(timezone.utc).isoformat()


def begin(conn):
    if not using_postgres():
        conn.execute('BEGIN IMMEDIATE')


def init_jobs():
    ident='SERIAL PRIMARY KEY' if using_postgres() else 'INTEGER PRIMARY KEY AUTOINCREMENT'
    with connect() as conn:
        cur=conn.cursor()
        cur.execute('''CREATE TABLE IF NOT EXISTS document_ai_jobs (
            kind TEXT NOT NULL,doc_id INTEGER NOT NULL,store TEXT NOT NULL,state TEXT NOT NULL DEFAULT 'pending',
            error TEXT NOT NULL DEFAULT '',requested_by TEXT NOT NULL,attempts INTEGER NOT NULL DEFAULT 0,
            extracted_json TEXT NOT NULL DEFAULT '',updated_at TEXT NOT NULL,PRIMARY KEY(kind,doc_id))''')
        cur.execute("""CREATE TABLE IF NOT EXISTS document_details (
            kind TEXT NOT NULL, doc_id INTEGER NOT NULL, details_json TEXT NOT NULL,
            PRIMARY KEY(kind,doc_id))""")
        cur.execute("""CREATE TABLE IF NOT EXISTS document_submissions (
            kind TEXT NOT NULL, doc_id INTEGER NOT NULL, username TEXT NOT NULL,
            state TEXT NOT NULL DEFAULT 'pending', updated_at TEXT NOT NULL,
            PRIMARY KEY(kind,doc_id))""")
        cur.execute(f'''CREATE TABLE IF NOT EXISTS digital_document_versions (
            id {ident},kind TEXT NOT NULL,doc_id INTEGER NOT NULL,store TEXT NOT NULL,
            changed_by TEXT NOT NULL,changed_at TEXT NOT NULL,before_json TEXT NOT NULL,after_json TEXT NOT NULL)''')


def enqueue(kind,doc_id,store,username='system',retry=False):
    if kind not in KINDS:
        raise ValueError('Tipo documento non valido')
    with connect() as conn:
        cur=conn.cursor()
        cur.execute(f'''INSERT INTO document_ai_jobs(kind,doc_id,store,requested_by,updated_at)
            VALUES({','.join([p()]*5)}) ON CONFLICT(kind,doc_id) DO NOTHING''',(kind,doc_id,store,username,stamp()))
        if retry:
            cur.execute(f"UPDATE document_ai_jobs SET state='pending',error='',attempts=0,requested_by={p()},updated_at={p()} WHERE kind={p()} AND doc_id={p()} AND state='error'",(username,stamp(),kind,doc_id))
    _wakeup.set()


def merge_missing_fields(cur,kind,doc_id,current,extracted):
    """Fill missing AI text without replacing reviewed values or inventory mappings."""
    if not current:
        return False
    extracted=dict(extracted)
    if not extracted.get('raw_text') and extracted.get('text'):
        extracted['raw_text']=extracted['text']
    changes={}
    for field in ('raw_text','description','supplier'):
        if not str(current.get(field) or '').strip() and str(extracted.get(field) or '').strip():
            changes[field]=str(extracted[field])
    if current.get('amount') is None and extracted.get('amount') is not None:
        changes['amount']=extracted['amount']
    applied=cur.execute(f'SELECT id FROM invoice_imports WHERE invoice_doc_id={p()} AND store={p()}',(doc_id,current['store'])).fetchone() if kind=='fatture' else None
    if not json.loads(current.get('lines_json') or '[]') and extracted.get('lines') and not applied:
        changes['lines_json']=json.dumps(extracted['lines'],ensure_ascii=False)
    if not changes:
        return False
    if 'amount' in changes or 'supplier' in changes:
        changes['state']='bozza'
    updates=','.join(f'{key}={p()}' for key in changes)
    cur.execute(f'UPDATE digital_documents SET {updates},revision=revision+1,updated_at={p()} WHERE kind={p()} AND doc_id={p()}',
        (*changes.values(),stamp(),kind,doc_id))
    return True


def enqueue_history(store=None,kind=None):
    # Historical originals are never submitted to AI after the stop request.
    return 0


def archive_state(store,kind):
    with connect() as conn:
        cur=conn.cursor()
        sql=f'SELECT * FROM document_ai_jobs WHERE kind={p()}'
        args=[kind]
        if store!='ALL':sql+=f' AND store={p()}';args.append(store)
        jobs=cur.execute(sql,tuple(args)).fetchall()
    result={state:sum(j['state']==state for j in jobs) for state in ['pending','working','done','error']}
    result['configured']=bool(os.environ.get('OPENAI_API_KEY','').strip())
    result['token']='|'.join(f"{j['doc_id']}:{j['state']}:{j['updated_at']}" for j in sorted(jobs,key=lambda j:j['doc_id']))
    return result


def ensure_archive(store,kind):
    enqueue_history(store,kind)
    start_worker()
    return archive_state(store,kind)


def run_document(kind,doc_id,username='system'):
    from .control import number,iso
    table,datecol,label=KINDS[kind]
    with connect() as conn:
        cur=conn.cursor()
        src=cur.execute(f'SELECT d.*,t.details_json AS prior_details FROM {table} d LEFT JOIN document_details t ON t.kind={p()} AND t.doc_id=d.id WHERE d.id={p()}',(kind,doc_id)).fetchone()
        if not src:
            raise ValueError('Documento originale non più disponibile.')
        if not src.get('data'):
            raise ValueError('Registrazione scritta: nessuna foto da leggere.')
        submission=cur.execute(f"SELECT state FROM document_submissions WHERE kind={p()} AND doc_id={p()} AND updated_at>={p()}",(kind,doc_id,NEW_UPLOADS_SINCE)).fetchone()
        if not submission or submission['state'] not in ('pending','automatic'):
            raise ValueError('Storico sospeso: vengono letti soltanto i nuovi caricamenti.')
        old=cur.execute(f'SELECT * FROM digital_documents WHERE kind={p()} AND doc_id={p()}',(kind,doc_id)).fetchone()
        old_revision=old['revision'] if old else 0
        applied=cur.execute(f'SELECT id FROM invoice_imports WHERE invoice_doc_id={p()} AND store={p()}',(doc_id,src['store'])).fetchone() if kind=='fatture' else None
    prior_details=json.loads(src.get('prior_details') or '{}')
    data=bytes(src['data'])
    if not data.startswith(b'%PDF'):
        from .pdf_tools import ensure_pdf
        data,_,_=ensure_pdf(data,src.get('filename') or 'foto',src.get('content_type') or 'application/octet-stream')
    catalog=None
    if kind=='fatture':
        with connect() as conn:
            catalog=[dict(r) for r in conn.cursor().execute(f'SELECT id,name,unit,area,location FROM products WHERE store={p()} ORDER BY id',(src['store'],)).fetchall()]
    result=extract_document(data,label,inventory=catalog,new_upload=True)
    if not str(result.get('text') or '').strip():
        raise ValueError('Nessun testo leggibile nella foto: originale conservato, lettura da riprovare.')
    warnings=list(result.get('warnings') or [])
    dt=(result.get('printed_date') if kind=='chiusure' else None) or result.get('date')
    try:
        if not dt:
            raise HTTPException(422,'')
        dt=iso(dt)
    except HTTPException:
        raise ValueError('Data del documento non leggibile. Originale conservato; nessuna data sostitutiva inserita.') from None
    # Preserve the real document date. Cash reconciliation applies its own day offset.
    supplier=result.get('supplier') or (src.get('supplier') or '')
    amount=number(result.get('amount'),optional=True)
    if not supplier:
        warnings.append('Provenienza non leggibile: da correggere.')
    if amount is None:
        warnings.append('Totale non leggibile: da correggere.')
    currency=str(result.get('currency') or 'EUR').upper()
    if len(currency)!=3 or not currency.isalpha():
        currency='EUR';warnings.append('Valuta non riconosciuta: verificare.')
    lines=[dict(description=str(l.get('description') or ''),qty=number(l.get('qty'),optional=True),
        unit=str(l.get('unit') or ''),amount=number(l.get('amount'),optional=True),
        product_id=l.get('product_id') if any(r['id']==l.get('product_id') for r in (catalog or [])) else None,
        stock_qty=number(l.get('stock_qty'),optional=True),matching_note=str(l.get('matching_note') or ''),
        needs_review=bool(l.get('needs_review',True))) for l in result.get('lines',[])]
    extracted=dict(doc_date=dt,supplier=supplier,amount=amount,currency=currency,description=result.get('description') or '',
                   raw_text=result.get('text') or '',lines=lines,warnings='\n'.join(warnings))
    with connect() as conn:
        begin(conn);cur=conn.cursor()
        details={**prior_details,**{k:v for k,v in result.items() if k not in ('text','lines','description','warnings')}}
        cur.execute(f'INSERT INTO document_details(kind,doc_id,details_json) VALUES({p()},{p()},{p()}) ON CONFLICT(kind,doc_id) DO UPDATE SET details_json=excluded.details_json',
                    (kind,doc_id,json.dumps(details,ensure_ascii=False)))
        current=cur.execute(f'SELECT * FROM digital_documents WHERE kind={p()} AND doc_id={p()}'+(' FOR UPDATE' if using_postgres() else ''),(kind,doc_id)).fetchone()
        # Keep corrections and verified documents. AI extraction is retained separately for admin comparison.
        preserve=bool(current and (current['revision']!=old_revision or current['state']=='verificato' or current.get('updated_by')!='AI'))
        if not preserve:
            if applied and old:
                lines=json.loads(old['lines_json'])
            cols='kind,doc_id,store,doc_date,supplier,amount,currency,description,raw_text,lines_json,warnings,state,revision,updated_by,updated_at'
            values=(kind,doc_id,src['store'],dt,supplier,amount,currency,extracted['description'],extracted['raw_text'],
                json.dumps(lines,ensure_ascii=False),extracted['warnings'],'bozza',old_revision+1,'AI',stamp())
            update=','.join(f'{c}=excluded.{c}' for c in cols.split(',')[3:])
            cur.execute(f'INSERT INTO digital_documents({cols}) VALUES({",".join([p()]*15)}) ON CONFLICT(kind,doc_id) DO UPDATE SET {update}',values)
        else:
            merge_missing_fields(cur,kind,doc_id,current,extracted)
            note='Controllo AI dell’archivio completato; dati già corretti conservati. Confronto disponibile all’amministratore.'
            cur.execute(f'UPDATE digital_documents SET warnings=warnings||{p()},revision=revision+1 WHERE kind={p()} AND doc_id={p()}',('\n'+note,kind,doc_id))
        # A successful new upload is archived automatically, without human confirmation.
        cur.execute(f"UPDATE document_submissions SET state='confirmed',updated_at={p()} WHERE kind={p()} AND doc_id={p()} AND state IN ('pending','automatic')",(stamp(),kind,doc_id))
        cur.execute(f'UPDATE admin_document_reviews SET reviewed_at={p()},reviewed_by={p()} WHERE kind={p()} AND doc_id={p()}',(stamp(),'AI',kind,doc_id))
        if kind=='fatture':
            cur.execute(f'UPDATE {table} SET {datecol}={p()},supplier={p()} WHERE id={p()}',(dt,supplier,doc_id))
        else:
            cur.execute(f'UPDATE {table} SET {datecol}={p()} WHERE id={p()}',(dt,doc_id))
        cur.execute(f'''UPDATE document_ai_jobs SET state='done',error='',extracted_json={p()},updated_at={p()}
            WHERE kind={p()} AND doc_id={p()}''',(json.dumps(extracted,ensure_ascii=False),stamp(),kind,doc_id))
        cur.execute(f'INSERT INTO logs(store,username,action,category,name,delta) VALUES({",".join([p()]*6)})',
            (src['store'],username,'DOC_AI_ARCHIVIO' if preserve else 'DOC_AI',kind,f'Documento #{doc_id}: trascrizione AI disponibile',0))
    return extracted


def process_one():
    if os.getenv('DOCUMENT_AI_ENABLED', 'new') == '0':
        return False
    if not os.environ.get('OPENAI_API_KEY','').strip():
        return False
    with connect() as conn:
        begin(conn);cur=conn.cursor()
        eligible=f"EXISTS(SELECT 1 FROM document_submissions s WHERE s.kind=document_ai_jobs.kind AND s.doc_id=document_ai_jobs.doc_id AND s.state IN ('pending','automatic') AND s.updated_at>={p()})"
        row=cur.execute("SELECT * FROM document_ai_jobs WHERE state='pending' AND attempts=0 AND "+eligible+" ORDER BY updated_at,kind,doc_id LIMIT 1"+
            (' FOR UPDATE SKIP LOCKED' if using_postgres() else ''),(NEW_UPLOADS_SINCE,)).fetchone()
        if not row:
            return False
        cur.execute(f"UPDATE document_ai_jobs SET state='working',attempts=attempts+1,updated_at={p()} WHERE kind={p()} AND doc_id={p()} AND state='pending'",(stamp(),row['kind'],row['doc_id']))
        if cur.rowcount!=1:
            return True
    try:
        run_document(row['kind'],row['doc_id'],row['requested_by'])
    except Exception as exc:
        message=str(exc.detail) if isinstance(exc,HTTPException) else str(exc) if isinstance(exc,ValueError) else 'Lettura non riuscita. Riprovare o contattare l’amministratore.'
        with connect() as conn:
            conn.cursor().execute(f"UPDATE document_ai_jobs SET state='error',error={p()},updated_at={p()} WHERE kind={p()} AND doc_id={p()}",
                (message[:800],stamp(),row['kind'],row['doc_id']))
    return True


def finalize_ready_uploads():
    """Finish already-read fresh uploads without spending another API call."""
    with connect() as conn:
        cur=conn.cursor()
        for kind,(table,datecol,_) in KINDS.items():
            rows=cur.execute(f"""SELECT s.doc_id,x.doc_date,x.supplier FROM document_submissions s
                JOIN document_ai_jobs j ON j.kind=s.kind AND j.doc_id=s.doc_id
                JOIN digital_documents x ON x.kind=s.kind AND x.doc_id=s.doc_id
                WHERE s.kind={p()} AND s.state IN ('pending','automatic') AND s.updated_at>={p()} AND j.state='done'""",(kind,NEW_UPLOADS_SINCE)).fetchall()
            for row in rows:
                cur.execute(f"UPDATE document_submissions SET state='confirmed',updated_at={p()} WHERE kind={p()} AND doc_id={p()}",(stamp(),kind,row['doc_id']))
                cur.execute(f'UPDATE admin_document_reviews SET reviewed_at={p()},reviewed_by={p()} WHERE kind={p()} AND doc_id={p()}',(stamp(),'AI',kind,row['doc_id']))
                if kind=='fatture':cur.execute(f'UPDATE {table} SET {datecol}={p()},supplier={p()} WHERE id={p()}',(row['doc_date'],row['supplier'],row['doc_id']))
                else:cur.execute(f'UPDATE {table} SET {datecol}={p()} WHERE id={p()}',(row['doc_date'],row['doc_id']))


def start_worker():
    if os.getenv('DOCUMENT_AI_ENABLED', 'new') == '0':
        return
    global _thread
    if not os.environ.get('OPENAI_API_KEY','').strip():
        return
    if _thread and _thread.is_alive():
        return
    _stop.clear()
    with connect() as conn:
        stale=(datetime.now(timezone.utc)-timedelta(minutes=10)).isoformat()
        conn.cursor().execute(f"UPDATE document_ai_jobs SET state='paused' WHERE state IN ('pending','working') AND NOT EXISTS(SELECT 1 FROM document_submissions s WHERE s.kind=document_ai_jobs.kind AND s.doc_id=document_ai_jobs.doc_id AND s.state IN ('pending','automatic') AND s.updated_at>={p()})",(NEW_UPLOADS_SINCE,))
        conn.cursor().execute("UPDATE document_ai_jobs SET state='error',error='Lettura interrotta. Originale conservato; nessuna rilettura automatica.' WHERE state='working'")

    def loop():
        while not _stop.is_set():
            try:
                active=process_one()
            except Exception:
                logging.exception('Coda documenti temporaneamente non disponibile')
                active=False
            if not active:
                _wakeup.wait(15);_wakeup.clear()
    _thread=threading.Thread(target=loop,name='document-digitalization',daemon=True)
    _thread.start()


def stop_worker():
    _stop.set();_wakeup.set()
