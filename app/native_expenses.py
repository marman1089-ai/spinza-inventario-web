"""Native site pages for expense planning, reusing the verified financial rules.

One private database read per page; calculations run in memory. There is no iframe,
second application, frontend bundle or cascade of API calls. All writes use the
same atomic accounting service and preserve the original records and history.
"""
import csv
import io
import json
import re
import secrets
import sqlite3
from calendar import monthrange
from datetime import timedelta
from decimal import Decimal,ROUND_HALF_UP
from contextlib import contextmanager
from datetime import date
from urllib.parse import urlencode
from fastapi import Request,HTTPException
from fastapi.responses import RedirectResponse,Response
from starlette.concurrency import run_in_threadpool
from .db import connect
from . import expense_program as program
from .expense_program import legacy,STORE_MAP,p

ROOT='/gestionale/spese-pagamenti'
DETAIL_LABELS={
 'previous_month_actual_cents':'Spese reali del mese precedente','previous_month_actual_count':'Numero spese del mese precedente',
 'previous_month_tracking_days':'Giorni registrati nel mese precedente','current_month_tracking_days':'Giorni registrati nel mese corrente',
 'fixed_only_monthly_equivalent_cents':'Quota mensile delle sole spese fisse','variable_monthly_budget_total_cents':'Budget mensile dei fornitori',
 'variable_budget_commitment_cents':'Budget fornitori ancora impegnato','variable_orders_cents':'Ordini del mese',
 'variable_pending_orders_cents':'Ordini ancora da saldare','variable_paid_orders_cents':'Ordini già pagati',
 'variable_remaining_to_order_cents':'Budget fornitori ancora da ordinare','variable_over_budget_cents':'Sforamento dei budget fornitori',
 'variable_order_count':'Numero ordini','variable_pending_order_count':'Numero ordini aperti','variable_paid_order_count':'Numero ordini pagati',
 'variable_average_order_cents':'Importo medio degli ordini','variable_projection_cents':'Proiezione mensile degli ordini',
 'variable_projection_delta_cents':'Scostamento della proiezione dal budget','fixed_daily_reserve_cents':'Quota giornaliera delle rate fisse',
 'fixed_reserved_today_cents':'Accantonato oggi per le spese fisse','other_pending_cents':'Altri pagamenti ancora aperti',
 'variable_budget_cents':'Budget flessibile','current_month_expenses_cents':'Spese realmente pagate nel mese',
 'current_variable_expenses_cents':'Spese variabili realmente pagate','current_fixed_paid_cents':'Rate fisse realmente pagate',
 'current_month_recharges_cents':'Ricariche del mese','aware_gross_cents':'Quota consapevole maturata prima delle spese',
 'aware_funds_cents':'Fondi consapevoli disponibili','pending_count':'Numero pagamenti aperti','pending_amount_cents':'Importo pagamenti aperti',
 'paid_count':'Numero pagamenti eseguiti','overdue_count':'Numero pagamenti scaduti','overdue_amount_cents':'Importo scaduto',
 'due_7_count':'Numero scadenze entro sette giorni','due_7_amount_cents':'Importo in scadenza entro sette giorni',
 'due_30_count':'Numero scadenze entro trenta giorni','due_30_amount_cents':'Importo in scadenza entro trenta giorni',
 'fixed_expenses_count':'Numero spese fisse attive','variable_budget_count':'Numero budget variabili attivi'}
TABS={'home':'Cruscotto','payments':'Spese da pagare','selected':'Spese selezionate','fixed':'Spese fisse','variables':'Spese variabili','budgets':'Budget spese','history':'Storico spese','recharges':'Ricariche','analysis':'Analisi approfondite'}

@contextmanager
def memory_data():
    with connect() as conn:row=conn.cursor().execute('SELECT data FROM expense_program_state WHERE id=1').fetchone()
    if not row:
        yield None;return
    db=sqlite3.connect(':memory:');db.row_factory=sqlite3.Row
    try:
        db.deserialize(bytes(row['data']))
        yield db
    finally:db.close()


def view_data(db,store,today,tab,status,period,anchor,q,page,aware_day,month='',distance=0,distance_unit='days',due_filter='all'):
    sid=next((key for key,value in STORE_MAP.items() if value==store),None)
    scope='global' if store=='ALL' else 'store'
    if scope=='store' and sid is None:raise HTTPException(422,'Negozio non valido.')
    dashboard=legacy.global_dashboard(db,today) if scope=='global' else legacy.store_dashboard(db,sid,today)
    overview=legacy.expense_overview(db,scope,sid,today)
    candidates=[dict(r) for r in legacy.selection_candidates(db) if scope=='global' or r['store_id']==sid]
    selected_keys={r[0] for r in db.execute('SELECT selection_key FROM expense_selections')}
    for r in candidates:r['selected']=r['selection_key'] in selected_keys
    selected=[r for r in candidates if r['selected']]
    where='' if scope=='global' else ' AND store_id=?';args=() if scope=='global' else (sid,)
    payments=[dict(r) for r in db.execute('SELECT * FROM payments WHERE status=?'+where+' ORDER BY due_date,id',(status,*args))]
    if month and status=='paid':payments=[r for r in payments if str(r['paid_at'] or '').startswith(month)]
    if status=='pending':
        if distance>0:
            horizon=(legacy.add_months(today,distance) if distance_unit=='months' else today+timedelta(days=distance)).isoformat()
            payments=[r for r in payments if r['due_date'] and r['due_date']<=horizon]
        if due_filter=='overdue':payments=[r for r in payments if r['due_date'] and r['due_date']<today.isoformat()]
        elif due_filter=='no_date':payments=[r for r in payments if not r['due_date']]
    if q:payments=[r for r in payments if q.casefold() in (r['description']+' '+(r['category'] or '')).casefold()]
    fixed=[legacy.serialize_fixed_expense(r,today) for r in db.execute('SELECT f.*,s.name AS store_name FROM fixed_expenses f JOIN stores s ON s.id=f.store_id WHERE f.active=1'+('' if scope=='global' else ' AND f.store_id=?')+' ORDER BY f.next_due_date,f.id',args)]
    variables=[legacy.serialize_variable_budget(db,r,today) for r in db.execute('SELECT b.*,s.name AS store_name FROM variable_budgets b JOIN stores s ON s.id=b.store_id WHERE b.active=1'+('' if scope=='global' else ' AND b.store_id=?')+' ORDER BY b.id',args)]
    variable_orders=[dict(r) for r in db.execute('SELECT v.*,b.description FROM variable_purchases v JOIN variable_budgets b ON b.id=v.budget_id WHERE 1=1'+('' if scope=='global' else ' AND v.store_id=?')+' ORDER BY v.purchase_date DESC,v.id DESC',args)] if tab=='variables' else []
    history=legacy.expense_history(db,scope,sid,period,anchor) if tab=='history' else None
    recharges=[dict(r) for r in db.execute('SELECT * FROM recharges WHERE 1=1'+where+' ORDER BY created_at DESC,id DESC',args)] if tab=='recharges' else []
    if month and tab=='recharges':recharges=[r for r in recharges if str(r['created_at']).startswith(month)]
    day=min(monthrange(today.year,today.month)[1],max(1,aware_day)) if aware_day else dashboard['aware_days']
    target=today.replace(day=day)
    simulated_fixed=sum(legacy.fixed_reserved_amount(r,target) for r in fixed)
    gross=int((Decimal(dashboard['variable_budget_cents'])*day/dashboard['month_days']).quantize(Decimal('1'),rounding=ROUND_HALF_UP))
    simulation=dict(day=day,committed=simulated_fixed+dashboard['variable_budget_commitment_cents'],funds=dashboard['aware_funds_cents'] if day==dashboard['aware_days'] else gross-dashboard['current_variable_expenses_cents'])
    return dict(dashboard=dashboard,overview=overview,candidates=candidates,selected=selected,selected_total=sum(r['amount_cents'] for r in selected),
                details_metrics=[dict(label=label,value=dashboard[key],money=key.endswith('_cents')) for key,label in DETAIL_LABELS.items() if key in dashboard],payments=payments[(page-1)*25:page*25],payment_count=len(payments),fixed=fixed,variables=variables,variable_orders=variable_orders,
                history=history,recharges=recharges,simulation=simulation,source_stores=[dict(r) for r in db.execute('SELECT * FROM stores')],sid=sid,scope=scope,aware_day=day)


def install(app,api):
    def admin(request):
        user=api['require_login'](request)
        if not user or not api['is_admin'](request):raise HTTPException(403,'Sezione riservata agli admin.')
        return user
    def csrf(request):return request.session.setdefault('expense_program_csrf',secrets.token_urlsafe(32))
    def destination(tab,message='',error=''):
        return ROOT+'?'+urlencode(dict(tab=tab,**({'message':message} if message else {}),**({'error':error} if error else {})))
    @app.get(ROOT)
    def index(request:Request,tab:str='home',status:str='pending',period:str='month',anchor:str='',q:str='',page:int=1,aware_day:int=0,message:str='',error:str='',month:str='',distance:int=0,distance_unit:str='days',due_filter:str='all'):
        user=admin(request);store=api['_effective_store'](request,user);today=api['_rome_today']()
        if tab not in TABS or status not in ('pending','paid') or period not in ('week','month','year','all'):raise HTTPException(422,'Filtro non valido.')
        if month and not re.fullmatch(r'\d{4}-\d{2}',month):raise HTTPException(422,'Mese non valido.')
        if not 0<=distance<=3650 or distance_unit not in ('days','months') or due_filter not in ('all','overdue','no_date'):raise HTTPException(422,'Filtro scadenze non valido.')
        try:anchor_day=date.fromisoformat(anchor) if anchor else today
        except ValueError:raise HTTPException(422,'Data non valida.')
        def calculate(db):
            for sid,label in STORE_MAP.items():db.execute('UPDATE stores SET name=? WHERE id=?',(api['STORES'][label],sid))
            return view_data(db,store,today,tab,status,period,anchor_day,q,max(1,page),aware_day,month,distance,distance_unit,due_filter)
        # Most requests need exactly one private read. Only a month rollover
        # persists new unpaid cycles and then reloads the authoritative state.
        with memory_data() as db:
            if db is None:data=None
            elif db.execute('SELECT 1 FROM fixed_expenses WHERE active=1 AND next_due_date<? LIMIT 1',(today.replace(day=1).isoformat(),)).fetchone():
                program.execute('GET','/api/stores',{},user)
                with memory_data() as fresh:data=calculate(fresh)
            else:data=calculate(db)
        return api['render']('native_expenses.html',request=request,user=user,data=data,tabs=TABS,tab=tab,status=status,period=period,anchor=anchor_day.isoformat(),q=q,page=max(1,page),today=today.isoformat(),
            csrf=csrf(request),action_token=lambda:secrets.token_urlsafe(32),site_store=store,message=message,error=error,month=month,distance=distance,distance_unit=distance_unit,due_filter=due_filter,
            tab_url=lambda t,**kw:ROOT+'?'+urlencode(dict(tab=t,**kw)))

    @app.post(ROOT+'/azione')
    async def action(request:Request):
        user=admin(request);form=await request.form()
        if not secrets.compare_digest(str(form.get('csrf','')),csrf(request)):raise HTTPException(403,'Ricarica la pagina.')
        tab=str(form.get('tab','home'));kind=str(form.get('action',''))
        if tab not in TABS:raise HTTPException(422,'Sezione non valida.')
        token=str(form.get('operation',''))
        if not re.fullmatch(r'[A-Za-z0-9_-]{43}',token):raise HTTPException(422,'Ricarica il modulo.')
        payload={key:str(value) for key,value in form.items() if key not in ('csrf','tab','action','operation')}
        store=api['_effective_store'](request,user)
        sid=next((key for key,value in STORE_MAP.items() if value==store),None)
        if sid:payload['store_id']=sid
        try:
            ident=int(payload.pop('id','0'))
            paths={'payment_add':('POST','/api/payments'),'fixed_add':('POST','/api/fixed-expenses'),
                   'variable_add':('POST','/api/variable-budgets'),'recharge_add':('POST','/api/recharges'),
                   'budget_limit':('POST','/api/expenses/limit'),'setup':('POST','/api/setup'),
                   'selection':('POST','/api/expenses/selection'),'payment_pay':('POST',f'/api/payments/{ident}/pay'),
                   'fixed_pay':('POST',f'/api/fixed-expenses/{ident}/pay'),'variable_order':('POST',f'/api/variable-budgets/{ident}/orders'),
                   'payment_delete':('DELETE',f'/api/payments/{ident}'),'fixed_delete':('DELETE',f'/api/fixed-expenses/{ident}'),
                   'variable_delete':('DELETE',f'/api/variable-budgets/{ident}'),'order_delete':('DELETE',f'/api/variable-orders/{ident}')}
            if kind not in paths:raise ValueError('Operazione non disponibile.')
            method,path=paths[kind]
            if kind=='selection':payload['selected']=payload.get('selected')=='1'
            if kind in ('payment_pay','fixed_pay') or (kind=='variable_order' and payload.get('payment_status')=='paid'):
                if payload.get('payment_method') not in ('Bonifico','Carta','Contanti'):raise ValueError('Scegli il metodo di pagamento.')
            if kind=='fixed_pay':payload['__expected_due']=payload.pop('expected_due','')
            payload['__native_action_token']=token
            response=await run_in_threadpool(program.execute,method,path,payload,user)
            if response.status_code>=400:
                body=json.loads(response.body);return RedirectResponse(destination(tab,error=body.get('error','Operazione non riuscita.')),303)
            return RedirectResponse(destination(tab,message='Modifica salvata.'),303)
        except (ValueError,TypeError):return RedirectResponse(destination(tab,error='Controlla i campi inseriti. Nessuna modifica salvata.'),303)

    @app.get(ROOT+'/backup')
    def backup(request:Request):
        admin(request)
        with connect() as conn:row=conn.cursor().execute('SELECT data FROM expense_program_state WHERE id=1').fetchone()
        if not row:raise HTTPException(404,'Dati non disponibili.')
        return Response(bytes(row['data']),media_type='application/octet-stream',headers={'Content-Disposition':'attachment; filename="spese-copia-attuale.db"','Cache-Control':'no-store'})

    @app.get(ROOT+'/esporta-programma')
    def export_program(request:Request):
        user=admin(request);store=api['_effective_store'](request,user)
        sid=next((key for key,value in STORE_MAP.items() if value==store),None)
        return program.execute('GET','/api/export.csv?'+('scope=global' if store=='ALL' else 'store_id='+str(sid)),{},user)

    @app.get(ROOT+'/esporta')
    def export(request:Request,period:str='all',anchor:str=''):
        user=admin(request);store=api['_effective_store'](request,user)
        if period not in ('week','month','year','all'):raise HTTPException(422,'Periodo non valido.')
        sid=next((key for key,value in STORE_MAP.items() if value==store),None)
        try:day=date.fromisoformat(anchor) if anchor else api['_rome_today']()
        except ValueError:raise HTTPException(422,'Data non valida.')
        with memory_data() as db:
            if db is None:raise HTTPException(404,'Dati non disponibili.')
            history=legacy.expense_history(db,'global' if store=='ALL' else 'store',sid,period,day)
        out=io.StringIO();writer=csv.writer(out,delimiter=';');writer.writerow(['Data','Negozio','Tipo','Descrizione','Categoria','Importo realmente pagato','Note'])
        for r in history['items']:writer.writerow([r['event_at'],r['store_name'],r['expense_type'],r['description'],r['category'],f"{r['amount_cents']/100:.2f}".replace('.',','),r['notes']])
        return Response('\ufeff'+out.getvalue(),media_type='text/csv',headers={'Content-Disposition':'attachment; filename="spese-pagate.csv"','Cache-Control':'no-store'})
