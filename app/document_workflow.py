"""Employee upload review and uniform read-only cards; originals remain private."""
import json
import secrets
from datetime import datetime, timezone
from fastapi import HTTPException, Request
from fastapi.responses import RedirectResponse, Response, JSONResponse
from .db import connect, using_postgres
from .document_jobs import KINDS, p, enqueue, start_worker


def submitted(kind, doc_id, store, username):
    with connect() as conn:
        conn.cursor().execute(f'INSERT INTO document_submissions(kind,doc_id,username,state,updated_at) VALUES({p()},{p()},{p()},{p()},{p()})',
                              (kind,doc_id,username,'automatic',datetime.now(timezone.utc).isoformat()))
    enqueue(kind,doc_id,store,username)
    start_worker()


def install(app, api):
    def access(request, kind, doc_id):
        user=api['require_login'](request)
        if not user:
            raise HTTPException(401,'Accedi al gestionale.')
        if kind not in KINDS:
            raise HTTPException(404,'Tipo documento non trovato.')
        table, datecol, _=KINDS[kind]
        store=api['_effective_store'](request,user)
        with connect() as conn:
            row=conn.cursor().execute(f"""SELECT d.id,d.store,d.{datecol} AS source_date,d.uploaded_by,
                s.username AS submission_user,s.state AS submission_state,
                x.doc_id AS digital_id,x.doc_date,x.supplier,x.amount,x.currency,x.description,
                x.raw_text,x.lines_json,x.warnings,x.state AS digital_state,x.revision,x.updated_by,x.updated_at,
                j.state AS job_state,j.error AS job_error,t.details_json
                FROM {table} d
                LEFT JOIN document_submissions s ON s.kind={p()} AND s.doc_id=d.id
                LEFT JOIN digital_documents x ON x.kind={p()} AND x.doc_id=d.id
                LEFT JOIN document_ai_jobs j ON j.kind={p()} AND j.doc_id=d.id
                LEFT JOIN document_details t ON t.kind={p()} AND t.doc_id=d.id
                WHERE d.id={p()}""",(kind,kind,kind,kind,doc_id)).fetchone()
        submission=dict(username=row['submission_user'],state=row['submission_state']) if row and row['submission_user'] is not None else None
        doc=None;job=None;details={}
        if row:
            if row['digital_id'] is not None:
                doc={key:row[key] for key in ('doc_date','supplier','amount','currency','description','raw_text','lines_json','warnings','revision','updated_by','updated_at')}
                doc.update(kind=kind,doc_id=doc_id,store=row['store'],state=row['digital_state'])
            if row['job_state'] is not None:job=dict(state=row['job_state'],error=row['job_error'])
            details=json.loads(row['details_json']) if row['details_json'] else {}
        if not row or (store!='ALL' and row['store']!=store):
            raise HTTPException(404,'Documento non trovato.')
        if user['role']!='admin' and submission and submission['state']!='confirmed' and submission['username']!=user['username']:
            raise HTTPException(403,'Invio non ancora confermato.')
        return user,row,submission,doc,job,details

    def csrf(request):
        return request.session.setdefault('document_workflow_csrf',secrets.token_urlsafe(32))

    def validate(request,form):
        if not secrets.compare_digest(str(form.get('csrf') or ''),csrf(request)):
            raise HTTPException(403,'Sessione scaduta: ricarica la scheda.')

    @app.get('/schede/{kind}/{doc_id}')
    def card(request:Request,kind:str,doc_id:int):
        user,src,sub,doc,job,details=access(request,kind,doc_id)
        if doc:
            doc=dict(doc);doc['lines']=json.loads(doc['lines_json'])
        return api['render']('business_document.html',request=request,user=user,stores=api['STORES'],brand=src['store'],
            kind=kind,label=KINDS[kind][2],src=src,submission=sub,doc=doc,job=job,details=details,csrf=csrf(request))

    @app.get('/schede/{kind}/{doc_id}/stato')
    def status(request:Request,kind:str,doc_id:int):
        user=api['require_login'](request)
        if not user:raise HTTPException(401,'Accedi al gestionale.')
        if kind not in KINDS:raise HTTPException(404,'Tipo documento non trovato.')
        scope=api['_effective_store'](request,user);table=KINDS[kind][0]
        with connect() as conn:
            row=conn.cursor().execute(f"""SELECT d.store,s.username,s.state AS submission,
                COALESCE(j.state,'none') AS state,COALESCE(j.error,'') AS error,COALESCE(x.revision,0) AS revision
                FROM {table} d
                LEFT JOIN document_submissions s ON s.kind={p()} AND s.doc_id=d.id
                LEFT JOIN document_ai_jobs j ON j.kind={p()} AND j.doc_id=d.id
                LEFT JOIN digital_documents x ON x.kind={p()} AND x.doc_id=d.id
                WHERE d.id={p()}""",(kind,kind,kind,doc_id)).fetchone()
        if not row or (scope!='ALL' and scope!=row['store']):raise HTTPException(404,'Documento non trovato.')
        if user['role']!='admin' and row['submission'] and row['submission']!='confirmed' and row['username']!=user['username']:
            raise HTTPException(403,'Invio non ancora confermato.')
        return JSONResponse({key:row[key] for key in ('state','error','revision')}|{'submission':row['submission'] or 'legacy'},headers={'Cache-Control':'no-store'})

    @app.get('/schede/{kind}/{doc_id}/anteprima')
    def preview(request:Request,kind:str,doc_id:int):
        user,src,sub,_,_,_=access(request,kind,doc_id)
        # This endpoint is the sole temporary photo exception for the uploader.
        if user['role']!='admin' and (not sub or sub['username']!=user['username'] or sub['state']!='pending'):
            raise HTTPException(403,'La foto originale è riservata all’admin dopo la conferma.')
        table=KINDS[kind][0]
        with connect() as conn:
            row=conn.cursor().execute(f'SELECT data,content_type FROM {table} WHERE id={p()}',(doc_id,)).fetchone()
        return Response(bytes(row['data']),media_type=row['content_type'],headers={'Cache-Control':'private, no-store','X-Content-Type-Options':'nosniff'})

    @app.post('/schede/{kind}/{doc_id}/decisione')
    async def decision(request:Request,kind:str,doc_id:int):
        user,src,sub,doc,job,_=access(request,kind,doc_id)
        form=await request.form();validate(request,form)
        if not sub or sub['state']!='pending' or (user['role']!='admin' and sub['username']!=user['username']):
            raise HTTPException(409,'Invio già concluso o non modificabile.')
        action=form.get('decision')
        if action not in ('confirm','reject'):
            raise HTTPException(422,'Scegli Conferma o Non conforme.')
        if action=='confirm' and (not doc or not job or job['state']!='done' or doc['amount'] is None or not doc['supplier']):
            raise HTTPException(422,'Lettura incompleta. Rifai la foto o chiedi la verifica dell’admin.')
        with connect() as conn:
            if not using_postgres():conn.execute('BEGIN IMMEDIATE')
            cur=conn.cursor()
            current=cur.execute(f'SELECT revision FROM digital_documents WHERE kind={p()} AND doc_id={p()}'+(' FOR UPDATE' if using_postgres() else ''),(kind,doc_id)).fetchone()
            if str(form.get('revision','0'))!=str(current['revision'] if current else 0):
                raise HTTPException(409,'La lettura è cambiata: ricarica e ricontrolla i dati.')
            state='confirmed' if action=='confirm' else 'rejected'
            cur.execute(f"UPDATE document_submissions SET state={p()},updated_at={p()} WHERE kind={p()} AND doc_id={p()} AND state='pending'",(state,datetime.now(timezone.utc).isoformat(),kind,doc_id))
            if cur.rowcount!=1:raise HTTPException(409,'Invio già concluso.')
            if action=='confirm':
                cur.execute(f"UPDATE digital_documents SET state='verificato',revision=revision+1,updated_by={p()} WHERE kind={p()} AND doc_id={p()}",(user['username'],kind,doc_id))
                # Employee confirmation completes this workflow, with the original retained for admin audit.
                cur.execute(f'UPDATE admin_document_reviews SET reviewed_at={p()},reviewed_by={p()} WHERE kind={p()} AND doc_id={p()}',(datetime.now(timezone.utc).isoformat(),user['username'],kind,doc_id))
                table,col,_=KINDS[kind]
                updates=f'{col}={p()}';values=[doc['doc_date']]
                if kind=='fatture':updates+=f',supplier={p()}';values.append(doc['supplier'])
                cur.execute(f'UPDATE {table} SET {updates} WHERE id={p()}',(*values,doc_id))
            else:
                cur.execute(f"UPDATE digital_documents SET state='non conforme',revision=revision+1 WHERE kind={p()} AND doc_id={p()}",(kind,doc_id))
            api['_log'](cur,store=src['store'],username=user['username'],action='DOC_CONFERMA_DIGITALE' if action=='confirm' else 'DOC_NON_CONFORME',category=kind,name=f'Documento #{doc_id}',delta=0)
        return RedirectResponse(f'/schede/{kind}/{doc_id}' if action=='confirm' else '/'+kind+'?rifai=1',303)
