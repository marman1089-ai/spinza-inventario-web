"""Date the retained initial quantities and queue exactly the invoices between counts."""
import secrets
from datetime import date,datetime,timezone,timedelta
from urllib.parse import urlencode
from fastapi import Request,HTTPException
from fastapi.responses import RedirectResponse
from starlette.concurrency import run_in_threadpool
from .db import connect,using_postgres
from .document_review import available_document

def ph():return '%s' if using_postgres() else '?'
def schema(cur):
    cur.execute('''CREATE TABLE IF NOT EXISTS inventory_initial_baselines (
        count_id BIGINT PRIMARY KEY,store TEXT NOT NULL,start_date TEXT NOT NULL,
        updated_by TEXT NOT NULL,updated_at TEXT NOT NULL)''')

def period_bounds(cur,count_id,store):
    current=cur.execute(f"SELECT * FROM inventory_period_count_headers WHERE id={ph()} AND store={ph()} AND area='prodotti'",(count_id,store)).fetchone()
    if not current:raise HTTPException(404,'Report cucina non trovato.')
    previous=cur.execute(f"SELECT * FROM inventory_period_count_headers WHERE store={ph()} AND area='prodotti' AND (counted_at<{ph()} OR (counted_at={ph()} AND id<{ph()})) ORDER BY counted_at DESC,id DESC LIMIT 1",(store,current['counted_at'],current['counted_at'],count_id)).fetchone()
    if previous:return str(previous['counted_at'])[:10],str(current['counted_at'])[:10],False
    baseline=cur.execute(f'SELECT start_date FROM inventory_initial_baselines WHERE count_id={ph()} AND store={ph()}',(count_id,store)).fetchone()
    if not baseline:raise HTTPException(422,'Imposta la data delle quantità iniziali nel primo report.')
    return baseline['start_date'],str(current['counted_at'])[:10],True

def install(app,api):
    def admin(request,form):
        user=api['require_login'](request)
        if not user or not api['is_admin'](request):raise HTTPException(403,'Operazione riservata agli admin.')
        token=request.session.get('inventory_report_csrf')
        if not token or not secrets.compare_digest(str(form.get('csrf','')),token):raise HTTPException(403,'Ricarica il report prima di continuare.')
        store=str(form.get('store',''))
        if store not in api['STORES']:raise HTTPException(422,'Negozio non valido.')
        try:count_id=int(form.get('report_id',''))
        except ValueError:raise HTTPException(422,'Report non valido.')
        return user,store,count_id
    @app.on_event('startup')
    def init():
        with connect() as conn:schema(conn.cursor())
    @app.post('/inventario/report/data-iniziale')
    async def set_baseline(request:Request):
        form=await request.form();user,store,count_id=admin(request,form)
        try:first=date.fromisoformat(str(form.get('start_date','')))
        except ValueError:raise HTTPException(422,'Inserisci la data delle quantità iniziali.')
        with connect() as conn:
            cur=conn.cursor();schema(cur)
            row=cur.execute(f"SELECT id,counted_at FROM inventory_period_count_headers WHERE store={ph()} AND area='prodotti' ORDER BY counted_at,id LIMIT 1",(store,)).fetchone()
            if not row or row['id']!=count_id:raise HTTPException(409,'La data iniziale si imposta soltanto sul primo report cucina.')
            if first>date.fromisoformat(str(row['counted_at'])[:10]):raise HTTPException(422,'La data iniziale non può essere successiva al primo conteggio.')
            saved=cur.execute(f'SELECT 1 FROM inventory_period_count_lines WHERE count_id={ph()} AND previous IS NOT NULL LIMIT 1',(count_id,)).fetchone()
            if not saved:raise HTTPException(422,'Questo vecchio report non conserva le quantità iniziali. Non possono essere ricostruite dalla giacenza attuale.')
            cur.execute(f"INSERT INTO inventory_initial_baselines(count_id,store,start_date,updated_by,updated_at) VALUES({','.join([ph()]*5)}) ON CONFLICT(count_id) DO UPDATE SET start_date=excluded.start_date,updated_by=excluded.updated_by,updated_at=excluded.updated_at",(count_id,store,first.isoformat(),user['username'],datetime.now(timezone.utc).isoformat()))
        return RedirectResponse('/inventario/report?'+urlencode(dict(area='prodotti',store=store,id=count_id)),303)
    @app.post('/inventario/report/ai-periodo')
    async def read_period(request:Request):
        form=await request.form();user,store,count_id=admin(request,form)
        from .kitchen_ai import enqueue_docs,configured
        if not configured():raise HTTPException(422,'Configura OPENAI_API_KEY su Render prima della lettura.')
        with connect() as conn:
            cur=conn.cursor();schema(cur)
            first,last,initial=period_bounds(cur,count_id,store)
            rows=cur.execute(f"SELECT id,doc_date FROM invoices_docs WHERE store={ph()} AND doc_date {'>=' if initial else '>'} {ph()} AND doc_date<={ph()} AND {available_document('fatture')} ORDER BY doc_date,id",(store,first,last)).fetchall()
        for row in rows:
            await run_in_threadpool(enqueue_docs,api,store,api['_kitchen_week'](str(row['doc_date'])[:10]),row['id'],user['username'],False)
        if rows:
            target='/inventario/fatture-settimana?'+urlencode(dict(store=store,week=api['_kitchen_week'](str(rows[0]['doc_date'])[:10]),doc=rows[0]['id'],report_id=count_id))
        else:target='/inventario/report?'+urlencode(dict(area='prodotti',store=store,id=count_id))
        return RedirectResponse(target,303)
