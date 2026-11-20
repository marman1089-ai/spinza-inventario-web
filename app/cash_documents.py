"""Dated admin receipts, independent of accounting movements."""
import secrets
from datetime import date
from fastapi import Request, HTTPException
from fastapi.responses import RedirectResponse, Response
from .db import connect, using_postgres

def ph(): return '%s' if using_postgres() else '?'
def schema(cur):
    ident='SERIAL PRIMARY KEY' if using_postgres() else 'INTEGER PRIMARY KEY AUTOINCREMENT'
    blob='BYTEA' if using_postgres() else 'BLOB'
    cur.execute(f"CREATE TABLE IF NOT EXISTS cash_expense_documents (id {ident}, receipt_date TEXT NOT NULL, store TEXT NOT NULL, description TEXT NOT NULL, spending_token TEXT NOT NULL DEFAULT '', filename TEXT NOT NULL, data {blob} NOT NULL, uploaded_by TEXT NOT NULL)")

def save(cur,day,store,description,token,document,user):
    schema(cur)
    content,filename=document
    cur.execute(f"INSERT INTO cash_expense_documents(receipt_date,store,description,spending_token,filename,data,uploaded_by) VALUES({','.join([ph()]*7)})",(day,store,description,token,filename,content,user['username']))

def install(app,api):
    root='/contanti-nei-negozi/ricevute'
    def admin(request):
        user=api['require_login'](request)
        if not user or not api['is_admin'](request): raise HTTPException(403,'Sezione riservata agli admin.')
        return user
    def csrf(request): return request.session.setdefault('cash_documents_csrf',secrets.token_urlsafe(32))
    def spending_options(cur):
        options=[dict(token=r['token'],day=r['day'],description=r['description'],amount=(r['reburger_cents']+r['sc_cents'])/100) for r in cur.execute('SELECT * FROM cash_company_spending ORDER BY day DESC').fetchall()]
        options += [dict(token='expense:'+str(r['id']),day=r['flow_date'],description=(r['supplier'] or r['category'] or r['notes'] or 'Uscita')+' · '+api['STORES'].get(r['store'],r['store']),amount=r['amount']) for r in cur.execute("SELECT * FROM cash_expenses WHERE lower(trim(payment_method)) IN ('cash','contanti','contante') ORDER BY flow_date DESC,id DESC").fetchall()]
        return sorted(options,key=lambda r:r['day'],reverse=True)
    @app.on_event('startup')
    def init():
        with connect() as conn: schema(conn.cursor())
    @app.get(root)
    def index(request:Request,day:str='',store:str='',spending_token:str='',description:str=''):
        user=admin(request)
        with connect() as conn:
            cur=conn.cursor();schema(cur)
            options=spending_options(cur)
            if spending_token:
                spending=next((r for r in options if r['token']==spending_token),None)
                if spending:
                    day=day or spending['day'];description=description or spending['description']
            sql='SELECT id,receipt_date,store,description,spending_token,filename,uploaded_by FROM cash_expense_documents WHERE 1=1';params=[]
            if day and not spending_token: sql+=f' AND receipt_date={ph()}';params.append(day)
            if store: sql+=f' AND store={ph()}';params.append(store)
            if spending_token: sql+=f' AND spending_token={ph()}';params.append(spending_token)
            rows=cur.execute(sql+' ORDER BY receipt_date DESC,id DESC',tuple(params)).fetchall()
        return api['render']('cash_documents.html',user=user,brand='ALL',stores=api['STORES'],rows=rows,spending_options=options,day=day,selected=store,spending_token=spending_token,description=description,csrf=csrf(request))
    @app.post(root)
    async def upload(request:Request):
        user=admin(request);form=await request.form()
        if not secrets.compare_digest(str(form.get('csrf','')),csrf(request)): raise HTTPException(403,'Ricarica la pagina.')
        day=str(form.get('day',''));store=str(form.get('store',''))
        try: date.fromisoformat(day)
        except ValueError: raise HTTPException(422,'Inserisci la data della ricevuta.')
        if store not in api['STORES'] and store!='aziende': raise HTTPException(422,'Seleziona il negozio o la spesa aziendale.')
        if store=='ALL': raise HTTPException(422,'Seleziona un singolo negozio.')
        token=str(form.get('spending_token',''))
        document=await api['_prepare_document_upload']([f for f in form.getlist('files') if getattr(f,'filename','')],'ricevuta')
        with connect() as conn:
            cur=conn.cursor()
            if token and not any(r['token']==token for r in spending_options(cur)): raise HTTPException(422,'Spesa non trovata. Carica senza collegamento.')
            save(cur,day,store,str(form.get('description','')).strip(),token,document,user)
        return RedirectResponse(root,303)
    @app.post(root+'/{doc_id}/collega')
    async def link(request:Request,doc_id:int):
        admin(request);form=await request.form()
        if not secrets.compare_digest(str(form.get('csrf','')),csrf(request)): raise HTTPException(403,'Ricarica la pagina.')
        token=str(form.get('spending_token',''))
        with connect() as conn:
            cur=conn.cursor()
            if token and not any(r['token']==token for r in spending_options(cur)): raise HTTPException(422,'Spesa non trovata.')
            cur.execute(f'UPDATE cash_expense_documents SET spending_token={ph()} WHERE id={ph()}',(token,doc_id))
        return RedirectResponse(root,303)
    @app.get(root+'/{doc_id}')
    def download(request:Request,doc_id:int):
        admin(request)
        with connect() as conn: row=conn.cursor().execute(f'SELECT data FROM cash_expense_documents WHERE id={ph()}',(doc_id,)).fetchone()
        if not row: raise HTTPException(404,'Ricevuta non trovata.')
        return Response(bytes(row['data']),media_type='application/pdf',headers={'Content-Disposition':f'inline; filename="ricevuta-{doc_id}.pdf"'})
