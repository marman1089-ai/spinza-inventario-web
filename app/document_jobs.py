"""Durable automatic transcription queue for new and historical photo documents."""
import json
import logging
import os
import threading
from datetime import datetime, timedelta, timezone
from fastapi import HTTPException
from .db import connect, using_postgres
from .document_ai import extract_document

KINDS={'fatture':('invoices_docs','doc_date','Fatture'),'chiusure':('closures','closure_date','Chiusure'),
       'spese-secondarie':('secondary_expenses','expense_date','Spese secondarie')}
_wakeup=threading.Event()
_stop=threading.Event()
_thread=None


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
            cur.execute(f"UPDATE document_ai_jobs SET state='pending',error='',requested_by={p()},updated_at={p()} WHERE kind={p()} AND doc_id={p()} AND state='error'",(username,stamp(),kind,doc_id))
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
    # Also rescan on archive visits, so existing uploads never require uploading again.
    total=0
    selected={kind:KINDS[kind]} if kind in KINDS else KINDS
    with connect() as conn:
        cur=conn.cursor()
        for key,(table,_,_) in selected.items():
            sql=f"SELECT id,store FROM {table} WHERE data IS NOT NULL AND COALESCE(filename,'')<>'registrazione-digitale'"
            args=[]
            if store and store!='ALL':
                sql+=f' AND store={p()}';args.append(store)
            rows=cur.execute(sql,tuple(args)).fetchall()
            for row in rows:
                cur.execute(f"""INSERT INTO document_ai_jobs(kind,doc_id,store,requested_by,updated_at)
                    VALUES({','.join([p()]*5)}) ON CONFLICT(kind,doc_id) DO NOTHING""",(key,row['id'],row['store'],'archivio',stamp()))
                total+=max(cur.rowcount,0)
                job=cur.execute(f'SELECT * FROM document_ai_jobs WHERE kind={p()} AND doc_id={p()}',(key,row['id'])).fetchone()
                current=cur.execute(f'SELECT * FROM digital_documents WHERE kind={p()} AND doc_id={p()}',(key,row['id'])).fetchone()
                if job['state']=='done':
                    try:
                        extracted=json.loads(job['extracted_json'] or '{}')
                    except (ValueError,TypeError):
                        extracted={}
                    if current and extracted:
                        merge_missing_fields(cur,key,row['id'],current,extracted)
                    missing_text=not current or (not str(current.get('raw_text') or '').strip() and not str(extracted.get('raw_text') or extracted.get('text') or '').strip())
                    if missing_text:
                        # A completed marker without a usable transcript must be recovered automatically.
                        cur.execute(f"UPDATE document_ai_jobs SET state='pending',updated_at={p()} WHERE kind={p()} AND doc_id={p()}",(stamp(),key,row['id']))

    _wakeup.set()
    return total


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
        src=cur.execute(f'SELECT * FROM {table} WHERE id={p()}',(doc_id,)).fetchone()
        if not src:
            raise ValueError('Documento originale non più disponibile.')
        if not src.get('data'):
            raise ValueError('Registrazione scritta: nessuna foto da leggere.')
        old=cur.execute(f'SELECT * FROM digital_documents WHERE kind={p()} AND doc_id={p()}',(kind,doc_id)).fetchone()
        old_revision=old['revision'] if old else 0
        applied=cur.execute(f'SELECT id FROM invoice_imports WHERE invoice_doc_id={p()} AND store={p()}',(doc_id,src['store'])).fetchone() if kind=='fatture' else None
    data=bytes(src['data'])
    if not data.startswith(b'%PDF'):
        from .pdf_tools import ensure_pdf
        data,_,_=ensure_pdf(data,src.get('filename') or 'foto',src.get('content_type') or 'application/octet-stream')
    result=extract_document(data,label)
    if not str(result.get('text') or '').strip():
        raise ValueError('Nessun testo leggibile nella foto: originale conservato, lettura da riprovare.')
    warnings=list(result.get('warnings') or [])
    dt=result.get('date')
    try:
        if not dt:
            raise HTTPException(422,'')
        dt=iso(dt)
    except HTTPException:
        dt=str(src[datecol]);warnings.append('Data non leggibile: controllare la data proposta.')
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
        unit=str(l.get('unit') or ''),amount=number(l.get('amount'),optional=True)) for l in result.get('lines',[])]
    extracted=dict(doc_date=dt,supplier=supplier,amount=amount,currency=currency,description=result.get('description') or '',
                   raw_text=result.get('text') or '',lines=lines,warnings='\n'.join(warnings))
    with connect() as conn:
        begin(conn);cur=conn.cursor()
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
        cur.execute(f'''UPDATE document_ai_jobs SET state='done',error='',extracted_json={p()},updated_at={p()}
            WHERE kind={p()} AND doc_id={p()}''',(json.dumps(extracted,ensure_ascii=False),stamp(),kind,doc_id))
        cur.execute(f'INSERT INTO logs(store,username,action,category,name,delta) VALUES({",".join([p()]*6)})',
            (src['store'],username,'DOC_AI_ARCHIVIO' if preserve else 'DOC_AI',kind,f'Documento #{doc_id}: trascrizione AI disponibile',0))
    return extracted


def process_one():
    if not os.environ.get('OPENAI_API_KEY','').strip():
        return False
    with connect() as conn:
        begin(conn);cur=conn.cursor()
        retry_before=(datetime.now(timezone.utc)-timedelta(minutes=15)).isoformat()
        cur.execute(f"UPDATE document_ai_jobs SET state='pending',error='' WHERE state='error' AND attempts<3 AND updated_at<{p()}",(retry_before,))
        stale=(datetime.now(timezone.utc)-timedelta(minutes=10)).isoformat()
        cur.execute(f"UPDATE document_ai_jobs SET state='pending' WHERE state='working' AND updated_at<{p()}",(stale,))
        row=cur.execute("SELECT * FROM document_ai_jobs WHERE state='pending' ORDER BY updated_at,kind,doc_id LIMIT 1"+
            (' FOR UPDATE SKIP LOCKED' if using_postgres() else '')).fetchone()
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


def start_worker():
    global _thread
    if not os.environ.get('OPENAI_API_KEY','').strip():
        return
    if _thread and _thread.is_alive():
        return
    _stop.clear()
    with connect() as conn:
        stale=(datetime.now(timezone.utc)-timedelta(minutes=10)).isoformat()
        conn.cursor().execute(f"UPDATE document_ai_jobs SET state='pending' WHERE state='working' AND updated_at<{p()}",(stale,))
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
