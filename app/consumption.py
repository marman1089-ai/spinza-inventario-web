"""Reuse kitchen consumption reports and already digitized invoices without another AI call."""
import json
import math
import secrets
from collections import defaultdict
from datetime import date,datetime,timezone
from fastapi import Request,HTTPException
from fastapi.responses import RedirectResponse
from .db import connect,using_postgres
from .kitchen_periods import ph,period_bounds,schema
from .document_review import available_document


def timeline(cur,store,area):
    first=cur.execute(f'SELECT id,counted_at,created_at,username FROM inventory_period_count_headers WHERE store={ph()} AND area={ph()} ORDER BY counted_at,id LIMIT 1',(store,area)).fetchone()
    activity=cur.execute(f'SELECT MIN(ts) AS first_at FROM logs WHERE store={ph()} AND area={ph()}',(store,area)).fetchone()
    baseline=cur.execute(f'SELECT start_date,updated_at FROM inventory_initial_baselines WHERE store={ph()} ORDER BY start_date LIMIT 1',(store,)).fetchone() if area=='prodotti' else None
    return dict(first_report=first,first_activity=activity['first_at'] if activity else None,baseline=baseline)


def install(app,api):
    def admin(request):
        user=api['require_login'](request)
        if not user or not api['is_admin'](request):raise HTTPException(403,'Sezione riservata agli admin.')
        return user

    @app.get('/gestionale/consumi')
    def reports(request:Request,store:str='',area:str='',start:str='',end:str='',page:int=1):
        user=admin(request)
        scope=api['_effective_store'](request,user)
        store=store or scope
        if store!='ALL' and store not in api['STORES']:raise HTTPException(422,'Negozio non valido.')
        if scope!='ALL' and store!=scope:raise HTTPException(403,'Seleziona il negozio nel menu principale.')
        if area not in ('','prodotti','bibite'):raise HTTPException(422,'Reparto non valido.')
        try:
            if start:date.fromisoformat(start)
            if end:date.fromisoformat(end)
            if start and end and start>end:raise ValueError()
        except ValueError:raise HTTPException(422,'Intervallo non valido.')
        page=max(1,page)
        api['_ensure_inventory_report_tables']()
        with connect() as conn:
            cur=conn.cursor();schema(cur)
            sql='SELECT * FROM inventory_period_count_headers WHERE 1=1';params=[]
            if store!='ALL':sql+=f' AND store={ph()}';params.append(store)
            if area:sql+=f' AND area={ph()}';params.append(area)
            if start:sql+=f' AND counted_at>={ph()}';params.append(start)
            if end:sql+=f' AND counted_at<={ph()}';params.append(end+' 23:59:59')
            rows=cur.execute(sql+f' ORDER BY counted_at DESC,id DESC LIMIT 101 OFFSET {ph()}',(*params,(page-1)*100)).fetchall()
        return api['render']('consumption.html',request=request,user=user,stores=api['STORES'],brand=scope,
            rows=rows[:100],has_next=len(rows)>100,page=page,store=store,area=area,start=start,end=end)

    @app.post('/inventario/report/collega-digitali')
    async def link(request:Request):
        user=admin(request);form=await request.form()
        expected=request.session.get('inventory_report_csrf')
        if not expected or not secrets.compare_digest(str(form.get('csrf') or ''),expected):raise HTTPException(403,'Ricarica il report.')
        store=str(form.get('store') or '')
        scope=api['_effective_store'](request,user)
        if store not in api['STORES'] or (scope!='ALL' and scope!=store):raise HTTPException(403,'Negozio non consentito.')
        try:report_id=int(form.get('report_id',''))
        except ValueError:raise HTTPException(422,'Report non valido.')
        api['_ensure_kitchen_invoice_tables']()
        with connect() as conn:
            if not using_postgres():conn.execute('BEGIN IMMEDIATE')
            cur=conn.cursor();schema(cur)
            if using_postgres():cur.execute('SELECT pg_advisory_xact_lock(hashtext(%s))',('kitchen-digital-link:'+store,))
            first,last,initial=period_bounds(cur,report_id,store)
            docs=cur.execute(f"SELECT d.id,d.doc_date,x.state,x.lines_json,CASE WHEN EXISTS(SELECT 1 FROM document_submissions s WHERE s.kind='fatture' AND s.doc_id=d.id AND s.state='confirmed') THEN 1 ELSE 0 END AS automatic FROM invoices_docs d JOIN digital_documents x ON x.kind='fatture' AND x.doc_id=d.id AND x.store=d.store WHERE d.store={ph()} AND d.doc_date {'>=' if initial else '>'} {ph()} AND d.doc_date<={ph()} AND {available_document('fatture','d.id')}",(store,first,last)).fetchall()
            linked=0
            for doc in docs:
                done=cur.execute(f"SELECT id FROM kitchen_invoice_reviews WHERE doc_id={ph()} AND store={ph()} AND status='done'",(doc['id'],store)).fetchone()
                if done:continue
                if doc['state']!='verificato' and not doc['automatic']:continue
                quantities=defaultdict(float);units={}
                for line in json.loads(doc['lines_json']):
                    if line.get('ignore'):continue
                    product=cur.execute(f'SELECT id,area,unit FROM products WHERE id={ph()} AND store={ph()}',(line.get('product_id'),store)).fetchone() if line.get('product_id') else None
                    if not product:raise HTTPException(422,f"Fattura {doc['id']}: associa le righe ai prodotti nella scheda digitale, oppure segnale come non inventario.")
                    if product['area']!='prodotti':continue
                    qty=line.get('stock_qty')
                    if qty is None and str(line.get('unit') or '').strip().casefold()==str(product['unit']).strip().casefold():qty=line.get('qty')
                    if isinstance(qty,bool) or not isinstance(qty,(int,float)) or not math.isfinite(qty) or qty<0 or qty>1e9:
                        raise HTTPException(422,f"Fattura {doc['id']}: conferma la quantità inventario di ogni prodotto cucina.")
                    quantities[product['id']]+=qty;units[product['id']]=product['unit']
                if not quantities:continue
                week=api['_kitchen_week'](str(doc['doc_date'])[:10])
                now=datetime.now(timezone.utc).isoformat()
                review=cur.execute(f"INSERT INTO kitchen_invoice_reviews(store,week_start,doc_id,status,updated_by,updated_at) VALUES({ph()},{ph()},{ph()},'done',{ph()},{ph()}) ON CONFLICT(store,week_start,doc_id) DO UPDATE SET status='done',updated_by=excluded.updated_by,updated_at=excluded.updated_at RETURNING id",(store,week,doc['id'],user['username'],now)).fetchone()
                rid=review['id'];cur.execute(f'DELETE FROM kitchen_invoice_review_lines WHERE review_id={ph()}',(rid,))
                for pid,qty in quantities.items():cur.execute(f'INSERT INTO kitchen_invoice_review_lines(review_id,product_id,qty,unit) VALUES({ph()},{ph()},{ph()},{ph()})',(rid,pid,qty,units[pid]))
                linked+=1
            api['_log'](cur,store=store,username=user['username'],action='CONSUMI_COLLEGA_DIGITALI',category='REPORT',name=f'{linked} fatture collegate al report {report_id}; giacenze invariate',delta=0,area='prodotti')
        return RedirectResponse(f'/inventario/report?area=prodotti&store={store}&id={report_id}',303)
