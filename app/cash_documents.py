"""Dated admin receipts, independent of accounting movements."""
import secrets
from datetime import date
from fastapi import Request, HTTPException
from fastapi.responses import RedirectResponse, Response
from .db import connect, using_postgres

def ph(): return '%s' if using_postgres() else '?'
def company_payer(row):
    parts=[]
    for key,label in [('reburger_cents','Reburger Slow Fast Food SRL'),('sc_cents','S&C SRLS')]:
        value=int(row.get(key) or 0)
        if value: parts.append(label+' · € '+f"{value/100:.2f}".replace('.',','))
    return ' + '.join(parts)

def schema(cur):
    ident='SERIAL PRIMARY KEY' if using_postgres() else 'INTEGER PRIMARY KEY AUTOINCREMENT'
    blob='BYTEA' if using_postgres() else 'BLOB'
    cur.execute(f"CREATE TABLE IF NOT EXISTS cash_expense_documents (id {ident}, receipt_date TEXT NOT NULL, store TEXT NOT NULL, description TEXT NOT NULL, spending_token TEXT NOT NULL DEFAULT '', filename TEXT NOT NULL, data {blob} NOT NULL, uploaded_by TEXT NOT NULL)")
    if using_postgres():
        cur.execute("ALTER TABLE cash_expense_documents ADD COLUMN IF NOT EXISTS payer TEXT NOT NULL DEFAULT ''")
    elif 'payer' not in {r['name'] for r in cur.execute('PRAGMA table_info(cash_expense_documents)').fetchall()}:
        cur.execute("ALTER TABLE cash_expense_documents ADD COLUMN payer TEXT NOT NULL DEFAULT ''")

def save(cur,day,store,description,token,document,user,payer=""):
    schema(cur)
    if not payer and token and not token.startswith('expense:'):
        spending=cur.execute(f'SELECT reburger_cents,sc_cents FROM cash_company_spending WHERE token={ph()}',(token,)).fetchone()
        if spending: payer=company_payer(spending)
    content,filename=document
    cur.execute(f"INSERT INTO cash_expense_documents(receipt_date,store,description,spending_token,filename,data,uploaded_by,payer) VALUES({','.join([ph()]*8)})",(day,store,description,token,filename,content,user['username'],payer))

def install(app,api):
    root='/contanti-nei-negozi/ricevute'
    def admin(request):
        user=api['require_login'](request)
        if not user or not api['is_admin'](request): raise HTTPException(403,'Sezione riservata agli admin.')
        return user
    def csrf(request): return request.session.setdefault('cash_documents_csrf',secrets.token_urlsafe(32))
    def spending_options(cur):
        options=[]
        for r in cur.execute('SELECT token,day,description,reburger_cents,sc_cents FROM cash_company_spending ORDER BY day DESC').fetchall():
            options.append(dict(token=r['token'],day=str(r['day']),description=r['description'],amount=(r['reburger_cents']+r['sc_cents'])/100,payer=company_payer(r),store='spinza' if not r['reburger_cents'] else 'aziende'))
        for r in cur.execute("SELECT id,flow_date,store,category,supplier,notes,amount FROM cash_expenses WHERE lower(trim(payment_method)) IN ('cash','contanti','contante') ORDER BY flow_date DESC,id DESC").fetchall():
            company='S&C SRLS' if r['store']=='spinza' else 'Reburger Slow Fast Food SRL'
            options.append(dict(token='expense:'+str(r['id']),day=str(r['flow_date']),description=r.get('notes') or r.get('supplier') or r.get('category') or 'Uscita',amount=r['amount'],store=r['store'],payer=company+' · '+api['STORES'].get(r['store'],r['store'])))
        return sorted(options,key=lambda r:r['day'],reverse=True)
    @app.on_event('startup')
    def init():
        with connect() as conn: schema(conn.cursor())
    @app.get(root)
    def index(request:Request,day:str='',store:str='',spending_token:str='',description:str='',payer:str=''):
        user=admin(request)
        filter_store=store
        with connect() as conn:
            cur=conn.cursor();schema(cur)
            options=spending_options(cur)
            if spending_token:
                spending=next((r for r in options if r['token']==spending_token),None)
                if spending:
                    day=day or spending['day'];description=description or spending['description'];store=store or spending['store'];payer=payer or spending['payer']
            sql='SELECT id,receipt_date,store,description,spending_token,filename,uploaded_by,payer FROM cash_expense_documents WHERE 1=1';params=[]
            if day and not spending_token: sql+=f' AND receipt_date={ph()}';params.append(day)
            if filter_store: sql+=f' AND store={ph()}';params.append(filter_store)
            if spending_token: sql+=f' AND spending_token={ph()}';params.append(spending_token)
            rows=cur.execute(sql+' ORDER BY receipt_date DESC,id DESC',tuple(params)).fetchall()
        return api['render']('cash_documents.html',user=user,brand='ALL',stores=api['STORES'],rows=rows,spending_options=options,day=day,selected=store,spending_token=spending_token,description=description,payer=payer,csrf=csrf(request))
    @app.post(root)
    async def upload(request:Request):
        user=admin(request);form=await request.form()
        if not secrets.compare_digest(str(form.get('csrf','')),csrf(request)): raise HTTPException(403,'Ricarica la pagina.')
        day=str(form.get('day','')).strip();store=str(form.get('store','')).strip()
        description=str(form.get('description','')).strip();payer=str(form.get('payer','')).strip()
        token=str(form.get('spending_token',''))
        with connect() as conn:
            selected=next((r for r in spending_options(conn.cursor()) if r['token']==token),None) if token else None
        if token and not selected: raise HTTPException(422,'Spesa non trovata. Carica senza collegamento.')
        if selected:
            day=day or selected['day'];store=store or selected['store'];description=description or selected['description'];payer=payer or selected['payer']
        try: date.fromisoformat(day)
        except ValueError: raise HTTPException(422,'Inserisci la data della ricevuta.')
        if store not in api['STORES'] and store!='aziende' or store=='ALL': raise HTTPException(422,'Seleziona il negozio o la spesa aziendale.')
        if len(description)>250 or len(payer)>500: raise HTTPException(422,'Descrizione o soggetto pagante troppo lunghi.')
        document=await api['_prepare_document_upload']([f for f in form.getlist('files') if getattr(f,'filename','')],'ricevuta')
        with connect() as conn:
            cur=conn.cursor()
            if token and not any(r['token']==token for r in spending_options(cur)): raise HTTPException(422,'Spesa non trovata. Carica senza collegamento.')
            save(cur,day,store,description,token,document,user,payer=payer)
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
    @app.post(root+'/{doc_id}/elimina')
    async def delete(request:Request,doc_id:int):
        user=admin(request);form=await request.form()
        if not secrets.compare_digest(str(form.get('csrf','')),csrf(request)): raise HTTPException(403,'Ricarica la pagina.')
        with connect() as conn:
            cur=conn.cursor()
            row=cur.execute(f'SELECT store,receipt_date,filename FROM cash_expense_documents WHERE id={ph()}',(doc_id,)).fetchone()
            if not row: raise HTTPException(404,'Ricevuta non trovata.')
            cur.execute(f'DELETE FROM cash_expense_documents WHERE id={ph()}',(doc_id,))
            api['_log'](cur,store=row['store'],username=user['username'],action='DOC_DELETE',category='SPESE',name=f"Ricevuta #{doc_id} · {row['receipt_date']} · {row['filename']}",delta=0)
        return RedirectResponse(root,303)
    @app.get(root+'/{doc_id}')
    def download(request:Request,doc_id:int):
        admin(request)
        with connect() as conn: row=conn.cursor().execute(f'SELECT data FROM cash_expense_documents WHERE id={ph()}',(doc_id,)).fetchone()
        if not row: raise HTTPException(404,'Ricevuta non trovata.')
        return Response(bytes(row['data']),media_type='application/pdf',headers={'Content-Disposition':f'inline; filename="ricevuta-{doc_id}.pdf"'})
