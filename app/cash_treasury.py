"""Physical cash by store, expense funding and inter-company advances (integer cents)."""
import re
import secrets
from collections import defaultdict
from datetime import date,datetime
from decimal import Decimal,ROUND_HALF_UP
from urllib.parse import urlencode
from zoneinfo import ZoneInfo
from fastapi import Request,HTTPException
from fastapi.responses import RedirectResponse
from .db import connect,using_postgres
from .closure_reports import cents

ORDER=('reburger_maggio','reburger_camaldoli','spinza')
LABELS={'reburger_maggio':'Piazza San Felice','reburger_camaldoli':'Via Camaldoli','spinza':'Spinza'}
COMPANIES={'sc':'S&C SRLS','reburger':'Reburger Slow Fast Food SRL'}
CASH_METHODS={'contanti','cash','contante'}

def company(store):return 'sc' if store=='spinza' else 'reburger'
def p():return '%s' if using_postgres() else '?'
def now():return datetime.now(ZoneInfo('Europe/Rome')).isoformat(timespec='seconds')
def amount(value):return int((Decimal(str(value or 0))*100).quantize(Decimal('1'),rounding=ROUND_HALF_UP))

def ensure_schema(cur):
    ident='SERIAL PRIMARY KEY' if using_postgres() else 'INTEGER PRIMARY KEY AUTOINCREMENT'
    cur.execute('''CREATE TABLE IF NOT EXISTS cash_treasury_openings (
        store TEXT PRIMARY KEY,start_date TEXT NOT NULL,amount_cents BIGINT NOT NULL,
        created_by TEXT NOT NULL,created_at TEXT NOT NULL)''')
    cur.execute('''CREATE TABLE IF NOT EXISTS cash_treasury_counts (
        store TEXT NOT NULL,day TEXT NOT NULL,amount_cents BIGINT NOT NULL,
        counted_by TEXT NOT NULL,counted_at TEXT NOT NULL,PRIMARY KEY(store,day))''')
    cur.execute(f'''CREATE TABLE IF NOT EXISTS cash_treasury_payments (
        id {ident},payment_date TEXT NOT NULL,name TEXT NOT NULL,company TEXT NOT NULL,
        beneficiary_store TEXT NOT NULL,total_cents BIGINT NOT NULL,expense_id INTEGER,
        token TEXT NOT NULL UNIQUE,created_by TEXT NOT NULL,created_at TEXT NOT NULL,
        cancelled_at TEXT NOT NULL DEFAULT '',cancel_reason TEXT NOT NULL DEFAULT '',
        expense_created INTEGER NOT NULL DEFAULT 1,original_method TEXT NOT NULL DEFAULT '',original_notes TEXT NOT NULL DEFAULT '',
        active_expense_id INTEGER REFERENCES cash_expenses(id) ON DELETE RESTRICT)''')
    cur.execute('''CREATE TABLE IF NOT EXISTS cash_treasury_sources (
        payment_id INTEGER NOT NULL,store TEXT NOT NULL,amount_cents BIGINT NOT NULL,
        PRIMARY KEY(payment_id,store))''')
    cur.execute(f'''CREATE TABLE IF NOT EXISTS cash_treasury_adjustments (
        id {ident},day TEXT NOT NULL,store TEXT NOT NULL,delta_cents BIGINT NOT NULL,
        notes TEXT NOT NULL,token TEXT NOT NULL UNIQUE,created_by TEXT NOT NULL,
        created_at TEXT NOT NULL,cancelled_at TEXT NOT NULL DEFAULT '',cancel_reason TEXT NOT NULL DEFAULT '')''')
    cur.execute(f'''CREATE TABLE IF NOT EXISTS cash_treasury_repayments (
        id {ident},day TEXT NOT NULL,source_store TEXT NOT NULL,target_store TEXT NOT NULL,
        amount_cents BIGINT NOT NULL,token TEXT NOT NULL UNIQUE,created_by TEXT NOT NULL,
        created_at TEXT NOT NULL,cancelled_at TEXT NOT NULL DEFAULT '',cancel_reason TEXT NOT NULL DEFAULT '')''')
    cur.execute('CREATE INDEX IF NOT EXISTS ix_treasury_payment_date ON cash_treasury_payments(payment_date)')
    cur.execute('CREATE INDEX IF NOT EXISTS ix_treasury_active_expense ON cash_treasury_payments(active_expense_id)')
    cur.execute('CREATE INDEX IF NOT EXISTS ix_treasury_cash_entries_period ON cash_entries(store,flow_date)')
    cur.execute('CREATE INDEX IF NOT EXISTS ix_treasury_cash_expenses_period ON cash_expenses(store,flow_date)')

def events(cur):
    openings={r['store']:dict(r) for r in cur.execute('SELECT * FROM cash_treasury_openings').fetchall()}
    stream=[]
    def add(store,day,delta,kind,name,actor='',ident=None,stamp=''):
        day=str(day)[:10]
        if store not in openings or day<openings[store]['start_date']:return
        stream.append(dict(store=store,day=day,delta=delta,kind=kind,name=name,actor=actor,id=ident,stamp=str(stamp or '')))
    for store,row in openings.items():add(store,row['start_date'],row['amount_cents'],'opening','Saldo iniziale contato',row['created_by'],stamp=row['created_at'])
    for row in cur.execute('SELECT e.id,e.store,e.flow_date,e.payment_method,e.amount,e.notes,e.created_by,e.ts FROM cash_entries e JOIN cash_treasury_openings b ON b.store=e.store AND CAST(e.flow_date AS TEXT)>=b.start_date').fetchall():
        if str(row['payment_method'] or '').strip().casefold() in CASH_METHODS:
            add(row['store'],row['flow_date'],amount(row['amount']),'income',row['notes'] or 'Incasso contanti',row['created_by'],row['id'],row['ts'])
    linked={r['expense_id'] for r in cur.execute("SELECT expense_id FROM cash_treasury_payments WHERE cancelled_at='' AND expense_id IS NOT NULL").fetchall()}
    unknown=[]
    for row in cur.execute('SELECT e.id,e.store,e.flow_date,e.payment_method,e.category,e.amount,e.notes,e.created_by,e.ts FROM cash_expenses e JOIN cash_treasury_openings b ON b.store=e.store AND CAST(e.flow_date AS TEXT)>=b.start_date').fetchall():
        if row['id'] in linked:continue
        method=str(row['payment_method'] or '').strip().casefold()
        closure=(row['category']=='Spese chiusura' and str(row['notes'] or '').startswith('Report chiusura #'))
        if method in CASH_METHODS or closure:
            add(row['store'],row['flow_date'],-amount(row['amount']),'expense',row['notes'] or row['category'],row['created_by'],row['id'],row['ts'])
        elif not method:
            unknown.append(dict(row))
    payments=[dict(r) for r in cur.execute('SELECT * FROM cash_treasury_payments ORDER BY payment_date DESC,id DESC').fetchall()]
    sources=defaultdict(list)
    for raw in cur.execute('SELECT * FROM cash_treasury_sources').fetchall():sources[raw['payment_id']].append(dict(raw))
    for row in payments:
        row['sources']=sources[row['id']]
        if row['cancelled_at']:continue
        for source in row['sources']:
            add(source['store'],row['payment_date'],-source['amount_cents'],'payment',row['name']+' → '+COMPANIES[row['company']],row['created_by'],row['id'],row['created_at'])
    repayments=[dict(r) for r in cur.execute('SELECT * FROM cash_treasury_repayments ORDER BY day DESC,id DESC').fetchall()]
    for row in repayments:
        if row['cancelled_at']:continue
        add(row['source_store'],row['day'],-row['amount_cents'],'repayment','Rimborso prestito → '+LABELS[row['target_store']],row['created_by'],row['id'],row['created_at'])
        add(row['target_store'],row['day'],row['amount_cents'],'repayment','Rimborso ricevuto da '+LABELS[row['source_store']],row['created_by'],row['id'],row['created_at'])
    for row in cur.execute("SELECT * FROM cash_treasury_adjustments WHERE cancelled_at=''").fetchall():
        add(row['store'],row['day'],row['delta_cents'],'adjustment',row['notes'],row['created_by'],row['id'],row['created_at'])
    stream.sort(key=lambda r:(r['day'],0 if r['kind']=='opening' else 1,r['stamp'],r['kind'],r['id'] or 0))
    return openings,stream,payments,repayments,unknown

def balances(openings,stream,asof):
    totals={store:0 for store in openings}
    for row in stream:
        if row['day']<=asof:totals[row['store']]+=row['delta']
    return {store:totals[store] if openings[store]['start_date']<=asof else None for store in openings}

def validate_cash(cur,today):
    openings,stream,_,_,_=events(cur)
    grouped=defaultdict(int)
    for row in stream:
        if row['day']<=today:grouped[(row['store'],row['day'])]+=row['delta']
    totals=defaultdict(int)
    for (store,day),delta in sorted(grouped.items(),key=lambda item:(item[0][1],item[0][0])):
        totals[store]+=delta
        if totals[store]<0:raise HTTPException(422,f'Contanti insufficienti a {LABELS[store]} al {day}: mancherebbero {-totals[store]/100:.2f} €. Controlla saldo iniziale e movimenti.')

def loan_stream(payments,repayments):
    result=[]
    for payment in payments:
        if payment['cancelled_at']:continue
        for source in payment['sources']:
            lender=company(source['store']);borrower=payment['company']
            if lender!=borrower:
                result.append(dict(day=payment['payment_date'],lender=lender,borrower=borrower,delta=source['amount_cents'],name=payment['name'],source=source['store'],id=payment['id'],kind='advance'))
    for row in repayments:
        if row['cancelled_at']:continue
        result.append(dict(day=row['day'],lender=company(row['target_store']),borrower=company(row['source_store']),delta=-row['amount_cents'],name='Rimborso prestito',source=row['source_store'],id=row['id'],kind='repayment'))
    return sorted(result,key=lambda r:(r['day'],r['kind'],r['id']))

def validate_loans(cur,today):
    _,_,payments,repayments,_=events(cur);running=defaultdict(int)
    days=defaultdict(int)
    for row in loan_stream(payments,repayments):
        if row['day']<=today:days[(row['day'],row['lender'],row['borrower'])]+=row['delta']
    for (day,lender,borrower),delta in sorted(days.items()):
        running[(lender,borrower)]+=delta
        if running[(lender,borrower)]<0:raise HTTPException(422,'Il rimborso supera il prestito disponibile alla data scelta, oppure un pagamento già rimborsato non può essere annullato.')

def expense_is_linked(cur,expense_id):
    return cur.execute(f"SELECT id FROM cash_treasury_payments WHERE expense_id={p()} AND cancelled_at=''",(expense_id,)).fetchone()

def install(app,api):
    def admin(request):
        user=api['require_login'](request)
        if not user or not api['is_admin'](request):raise HTTPException(403,'Sezione riservata agli admin.')
        return user
    def token(request):return request.session.setdefault('treasury_csrf',secrets.token_urlsafe(32))
    async def form_for(request):
        user=admin(request);form=await request.form()
        if not secrets.compare_digest(str(form.get('csrf','')),token(request)):raise HTTPException(403,'Sessione scaduta: ricarica la pagina.')
        return user,form
    def store_key(value):
        if value not in ORDER:raise HTTPException(422,'Negozio non valido.')
        return value
    def day_value(value):
        try:day=date.fromisoformat(str(value))
        except ValueError:raise HTTPException(422,'Data non valida.')
        if day>api['_rome_today']():raise HTTPException(422,'Inserisci una data di pagamento già avvenuta, non futura.')
        return day.isoformat()
    def submission(form):
        value=str(form.get('submission',''))
        if not re.fullmatch(r'[A-Za-z0-9_-]{40,60}',value):raise HTTPException(422,'Ricarica il modulo prima di salvare.')
        return value
    def begin(cur):
        if using_postgres():cur.execute('SELECT store FROM cash_treasury_openings ORDER BY store FOR UPDATE').fetchall()
        else:cur.execute('BEGIN IMMEDIATE')
    def require_base(cur,store,day):
        base=cur.execute(f'SELECT * FROM cash_treasury_openings WHERE store={p()}',(store,)).fetchone()
        if not base:raise HTTPException(422,'Prima configura il saldo iniziale di '+LABELS[store]+'.')
        if day<base['start_date']:raise HTTPException(422,'La data precede il saldo iniziale di '+LABELS[store]+'.')
    def logged(cur,user,store,name):api['_log'](cur,store=store,username=user['username'],action='CONTANTI',category='CONTANTI',name=name,delta=0)
    def back():return RedirectResponse('/contanti-nei-negozi',303)
    @app.on_event('startup')
    def startup():
        with connect() as conn:ensure_schema(conn.cursor())
    @app.get('/contanti-nei-negozi')
    def index(request:Request,asof:str='',start:str='',end:str='',who:str=''):
        user=admin(request);scope=api['_current_store_scope'](request,user)
        cutoff=day_value(asof) if asof else api['_rome_today']().isoformat()
        first=day_value(start) if start else '';last=day_value(end) if end else cutoff
        if first and first>last:raise HTTPException(422,'Intervallo non valido.')
        with connect() as conn:
            cur=conn.cursor();openings,stream,payments,repayments,unknown=events(cur)
            current=balances(openings,stream,cutoff)
            available_now=balances(openings,stream,api['_rome_today']().isoformat())
            counts={r['store']:r['amount_cents'] for r in cur.execute(
                f'SELECT store,amount_cents FROM cash_treasury_counts WHERE day={p()}',(cutoff,)
            ).fetchall()}
            unlinked=[dict(r) for r in cur.execute("SELECT id,store,flow_date,amount,category,notes,payment_method FROM cash_expenses WHERE store IN ('spinza','reburger_maggio','reburger_camaldoli') AND category<>'Spese chiusura' AND id NOT IN (SELECT expense_id FROM cash_treasury_payments WHERE cancelled_at='' AND expense_id IS NOT NULL) ORDER BY flow_date DESC,id DESC LIMIT 200").fetchall()]
        selected=ORDER if scope=='ALL' else (scope,)
        cards=[dict(store=st,label=LABELS[st],company=COMPANIES[company(st)],opening=openings.get(st),balance=current.get(st)) for st in selected]
        def summary(label, members):
            expected=sum(current[st] for st in members) if all(current.get(st) is not None for st in members) else None
            counted=sum(counts[st] for st in members) if all(st in counts for st in members) else None
            return dict(label=label,expected=expected,counted=counted,
                        difference=counted-expected if counted is not None and expected is not None else None)

        summaries=[summary(COMPANIES[key], [st for st in ORDER if company(st)==key]) for key in COMPANIES]
        summaries.append(summary('Totale dei tre negozi', ORDER))
        for card in cards:
            card.update(summary(card['label'], (card['store'],)))
        history=[];running=defaultdict(int)
        for row in stream:
            running[row['store']]+=row['delta']
            if row['store'] in selected and (not first or row['day']>=first) and row['day']<=last and (not who or who.casefold() in row['actor'].casefold()):
                history.append(dict(row,balance=running[row['store']]))
        loans=loan_stream(payments,repayments);debt=defaultdict(int)
        for row in loans:
            if row['day']<=cutoff:debt[(row['lender'],row['borrower'])]+=row['delta']
        loan_cards=[dict(lender=lender,borrower=borrower,balance=debt[(lender,borrower)]) for lender,borrower in [('sc','reburger'),('reburger','sc')]]
        return api['render']('cash_treasury.html',request=request,user=user,brand=scope,scope=scope,stores=api['STORES'],labels=LABELS,company_for={st:company(st) for st in ORDER},companies=COMPANIES,cards=cards,summaries=summaries,all_balances=available_now,openings=openings,store_order=ORDER,history=list(reversed(history)),payments=[r for r in payments if scope=='ALL' or r['beneficiary_store']==scope or any(s['store']==scope for s in r['sources'])],repayments=[r for r in repayments if scope=='ALL' or scope in (r['source_store'],r['target_store'])],loan_cards=loan_cards,loans=[r for r in reversed(loans) if r['day']<=cutoff],unknown=[r for r in unknown if r['store'] in selected and r['store'] in openings and str(r['flow_date'])>=openings[r['store']]['start_date']],unlinked=unlinked,today=api['_rome_today']().isoformat(),asof=cutoff,start=first,end=end,who=who,csrf=token(request),submission=secrets.token_urlsafe(32))
    @app.post('/contanti-nei-negozi/conteggio')
    async def save_count(request:Request):
        user,form=await form_for(request)
        store=store_key(form.get('store'))
        day=day_value(form.get('day'))
        raw=str(form.get('amount','')).strip()
        if not raw:
            raise HTTPException(422,'Inserisci il contante effettivamente contato.')
        value=cents(raw)
        with connect() as conn:
            cur=conn.cursor()
            cur.execute(f'''INSERT INTO cash_treasury_counts(store,day,amount_cents,counted_by,counted_at)
                VALUES({','.join([p()]*5)}) ON CONFLICT(store,day) DO UPDATE SET
                amount_cents=excluded.amount_cents,counted_by=excluded.counted_by,counted_at=excluded.counted_at''',
                (store,day,value,user['username'],now()))
            logged(cur,user,store,f'Conteggio contanti del {day}')
        return RedirectResponse('/contanti-nei-negozi?'+urlencode(dict(asof=day)),303)

    @app.post('/contanti-nei-negozi/saldo-iniziale')
    async def opening(request:Request):
        user,form=await form_for(request);store=store_key(str(form.get('store','')));day=day_value(form.get('day',''));value=cents(form.get('amount',''))
        with connect() as conn:
            cur=conn.cursor();begin(cur)
            busy=cur.execute(f'SELECT payment_id FROM cash_treasury_sources WHERE store={p()}',(store,)).fetchone()
            busy=busy or cur.execute(f'SELECT id FROM cash_treasury_adjustments WHERE store={p()}',(store,)).fetchone()
            busy=busy or cur.execute(f'SELECT id FROM cash_treasury_repayments WHERE source_store={p()} OR target_store={p()}',(store,store)).fetchone()
            if busy:raise HTTPException(409,'Il saldo iniziale ha già movimenti collegati. Per correggere il contante usa una rettifica, mantenendo lo storico.')
            cur.execute(f'''INSERT INTO cash_treasury_openings(store,start_date,amount_cents,created_by,created_at) VALUES({','.join([p()]*5)})
                ON CONFLICT(store) DO UPDATE SET start_date=excluded.start_date,amount_cents=excluded.amount_cents,created_by=excluded.created_by,created_at=excluded.created_at''',(store,day,value,user['username'],now()))
            validate_cash(cur,api['_rome_today']().isoformat());logged(cur,user,store,f'Saldo iniziale contanti del {day}: {value/100:.2f} €')
        return back()
    @app.post('/contanti-nei-negozi/paga')
    async def pay(request:Request):
        user,form=await form_for(request);key=submission(form);day=day_value(form.get('day',''))
        name=' '.join(str(form.get('name','')).split());target=str(form.get('company',''));beneficiary=store_key(str(form.get('beneficiary_store','')))
        if not name or len(name)>200:raise HTTPException(422,'Inserisci il nome della spesa (massimo 200 caratteri).')
        if target not in COMPANIES or company(beneficiary)!=target:raise HTTPException(422,'Il negozio beneficiario non appartiene all’azienda scelta.')
        total=cents(form.get('amount',''));sources={st:cents(str(form.get('from_'+st,'')).strip() or '0') for st in ORDER}
        if total<=0 or sum(sources.values())!=total:raise HTTPException(422,'La somma presa dai negozi deve coincidere esattamente con la spesa, maggiore di zero.')
        try:expense_id=int(str(form.get('expense_id','0')) or '0')
        except ValueError:raise HTTPException(422,'Uscita da collegare non valida.')
        with connect() as conn:
            cur=conn.cursor();begin(cur)
            previous=cur.execute(f'SELECT id,cancelled_at FROM cash_treasury_payments WHERE token={p()}',(key,)).fetchone()
            if previous:
                if previous['cancelled_at']:raise HTTPException(409,'Il pagamento è stato annullato: usa un nuovo modulo.')
                return back()
            for st,value in sources.items():
                if value:require_base(cur,st,day)
            existing=None
            if expense_id:
                existing=cur.execute(f'SELECT * FROM cash_expenses WHERE id={p()}'+(' FOR UPDATE' if using_postgres() else ''),(expense_id,)).fetchone()
                if not existing or existing['store']!=beneficiary or amount(existing['amount'])!=total or existing['category']=='Spese chiusura':raise HTTPException(422,'L’uscita da collegare deve avere stesso importo e negozio beneficiario; le spese dei report sono già gestite.')
                if expense_is_linked(cur,expense_id):raise HTTPException(409,'Questa uscita è già collegata a un pagamento.')
            cur.execute(f'''INSERT INTO cash_treasury_payments(payment_date,name,company,beneficiary_store,total_cents,expense_id,token,created_by,created_at,expense_created,original_method,original_notes,active_expense_id)
                VALUES({','.join([p()]*13)}) RETURNING id''',(day,name,target,beneficiary,total,expense_id or None,key,user['username'],now(),0 if existing else 1,str(existing['payment_method'] or '') if existing else '',str(existing['notes'] or '') if existing else '',expense_id or None))
            payment_id=cur.fetchone()['id']
            details='; '.join(LABELS[st]+f': {value/100:.2f} €' for st,value in sources.items() if value)
            notes=f'Pagamento contanti #{payment_id} - {name} | {COMPANIES[target]} | '+details
            if existing:
                # Preserve the original business expense date, update only its payment information.
                cur.execute(f'UPDATE cash_expenses SET payment_method={p()},notes={p()} WHERE id={p()}',('Contanti',str(existing['notes'] or '')+'\n'+notes,expense_id))
            else:
                api['_insert_cash_expense_compat'](cur,beneficiary,day,'Spese contanti','', 'Contanti',total/100,notes,user['username'])
                expense=cur.execute(f'SELECT id FROM cash_expenses WHERE store={p()} AND notes={p()} ORDER BY id DESC',(beneficiary,notes)).fetchone()
                expense_id=expense['id']
                cur.execute(f'UPDATE cash_treasury_payments SET expense_id={p()},active_expense_id={p()} WHERE id={p()}',(expense_id,expense_id,payment_id))
            for st,value in sources.items():
                if value:cur.execute(f'INSERT INTO cash_treasury_sources(payment_id,store,amount_cents) VALUES({p()},{p()},{p()})',(payment_id,st,value))
            validate_cash(cur,api['_rome_today']().isoformat());validate_loans(cur,api['_rome_today']().isoformat())
            logged(cur,user,beneficiary,f'Pagamento contanti #{payment_id} - {name}')
        return back()
    @app.post('/contanti-nei-negozi/rettifica')
    async def adjustment(request:Request):
        user,form=await form_for(request);key=submission(form);store=store_key(str(form.get('store','')));day=day_value(form.get('day',''))
        counted=cents(form.get('amount',''));reason=str(form.get('reason','')).strip()
        if not reason or len(reason)>1000:raise HTTPException(422,'Inserisci il motivo del conteggio/rettifica (massimo 1000 caratteri).')
        with connect() as conn:
            cur=conn.cursor();begin(cur)
            if cur.execute(f'SELECT id FROM cash_treasury_adjustments WHERE token={p()}',(key,)).fetchone():return back()
            require_base(cur,store,day);op,stream,_,_,_=events(cur);expected=balances(op,stream,day)[store];delta=counted-expected
            cur.execute(f'INSERT INTO cash_treasury_adjustments(day,store,delta_cents,notes,token,created_by,created_at) VALUES({",".join([p()]*7)})',(day,store,delta,reason,key,user['username'],now()))
            validate_cash(cur,api['_rome_today']().isoformat());logged(cur,user,store,'Rettifica contante contato del '+day)
        return back()
    @app.post('/contanti-nei-negozi/rimborso')
    async def repay(request:Request):
        user,form=await form_for(request);key=submission(form);day=day_value(form.get('day',''));source=store_key(str(form.get('source_store','')));target=store_key(str(form.get('target_store','')));value=cents(form.get('amount',''))
        if company(source)==company(target) or value<=0:raise HTTPException(422,'Il rimborso deve essere positivo e andare all’altra azienda.')
        with connect() as conn:
            cur=conn.cursor();begin(cur)
            if cur.execute(f'SELECT id FROM cash_treasury_repayments WHERE token={p()}',(key,)).fetchone():return back()
            require_base(cur,source,day);require_base(cur,target,day)
            cur.execute(f'INSERT INTO cash_treasury_repayments(day,source_store,target_store,amount_cents,token,created_by,created_at) VALUES({",".join([p()]*7)})',(day,source,target,value,key,user['username'],now()))
            validate_loans(cur,api['_rome_today']().isoformat());validate_cash(cur,api['_rome_today']().isoformat());logged(cur,user,source,'Rimborso prestito contanti del '+day)
        return back()
    @app.post('/contanti-nei-negozi/annulla-pagamento')
    async def cancel(request:Request):
        user,form=await form_for(request)
        try:ident=int(str(form.get('payment_id','')))
        except ValueError:raise HTTPException(422,'Pagamento non valido.')
        reason=str(form.get('reason','')).strip()
        if not reason or len(reason)>1000:raise HTTPException(422,'Inserisci il motivo dell’annullamento.')
        with connect() as conn:
            cur=conn.cursor();begin(cur)
            row=cur.execute(f'SELECT * FROM cash_treasury_payments WHERE id={p()}',(ident,)).fetchone()
            if not row:raise HTTPException(404,'Pagamento non trovato.')
            if row['cancelled_at']:return back()
            cur.execute(f'UPDATE cash_treasury_payments SET cancelled_at={p()},cancel_reason={p()},active_expense_id=NULL WHERE id={p()}',(now(),reason,ident))
            # A void is a correction of a nonexistent payment, not a new cash income.
            if row['expense_id']:
                if row['expense_created']:cur.execute(f'DELETE FROM cash_expenses WHERE id={p()}',(row['expense_id'],))
                else:cur.execute(f'UPDATE cash_expenses SET payment_method={p()},notes={p()} WHERE id={p()}',(row['original_method'],row['original_notes'],row['expense_id']))
            validate_loans(cur,api['_rome_today']().isoformat());validate_cash(cur,api['_rome_today']().isoformat());logged(cur,user,row['beneficiary_store'],f'Annullato pagamento contanti #{ident}')
        return back()

    @app.post('/contanti-nei-negozi/annulla-rimborso')
    async def cancel_repayment(request:Request):
        user,form=await form_for(request)
        try:ident=int(str(form.get('repayment_id','')))
        except ValueError:raise HTTPException(422,'Rimborso non valido.')
        reason=str(form.get('reason','')).strip()
        if not reason or len(reason)>1000:raise HTTPException(422,'Inserisci il motivo dell’annullamento.')
        with connect() as conn:
            cur=conn.cursor();begin(cur)
            row=cur.execute(f'SELECT * FROM cash_treasury_repayments WHERE id={p()}',(ident,)).fetchone()
            if not row:raise HTTPException(404,'Rimborso non trovato.')
            if row['cancelled_at']:return back()
            cur.execute(f'UPDATE cash_treasury_repayments SET cancelled_at={p()},cancel_reason={p()} WHERE id={p()}',(now(),reason,ident))
            validate_cash(cur,api['_rome_today']().isoformat());validate_loans(cur,api['_rome_today']().isoformat());logged(cur,user,row['source_store'],f'Annullato rimborso prestito #{ident}')
        return back()
