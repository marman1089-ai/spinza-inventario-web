"""Physical cash by store, expense funding and inter-company advances (integer cents)."""
import json
import re
import secrets
from calendar import monthrange
from collections import defaultdict
from datetime import date,datetime,timedelta
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
    cur.execute(f'''CREATE TABLE IF NOT EXISTS cash_treasury_reconciliations (
        id {ident}, store TEXT NOT NULL, day TEXT NOT NULL, expected_cents BIGINT NOT NULL,
        counted_cents BIGINT NOT NULL, cash_cents BIGINT NOT NULL, previous_cents BIGINT,
        notes TEXT NOT NULL, counted_by TEXT NOT NULL, counted_at TEXT NOT NULL, token TEXT NOT NULL UNIQUE)''')
    cur.execute('CREATE INDEX IF NOT EXISTS ix_treasury_reconciliation ON cash_treasury_reconciliations(store,day,id)')
    cur.execute('''CREATE TABLE IF NOT EXISTS cash_daily_openings (
        store TEXT PRIMARY KEY,start_date TEXT NOT NULL,amount_cents BIGINT NOT NULL)''')
    cur.execute('''CREATE TABLE IF NOT EXISTS cash_daily_counts (
        store TEXT NOT NULL,day TEXT NOT NULL,amount_cents BIGINT NOT NULL,
        counted_by TEXT NOT NULL,counted_at TEXT NOT NULL,PRIMARY KEY(store,day))''')
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
            add(row['store'],row['flow_date'],amount(row['amount']),'report_income' if str(row['notes'] or '').startswith('Report chiusura #') else 'income',row['notes'] or 'Incasso contanti',row['created_by'],row['id'],row['ts'])
    linked={r['expense_id'] for r in cur.execute("SELECT expense_id FROM cash_treasury_payments WHERE cancelled_at='' AND expense_id IS NOT NULL").fetchall()}
    unknown=[]
    for row in cur.execute('SELECT e.id,e.store,e.flow_date,e.payment_method,e.category,e.amount,e.notes,e.created_by,e.ts FROM cash_expenses e JOIN cash_treasury_openings b ON b.store=e.store AND CAST(e.flow_date AS TEXT)>=b.start_date').fetchall():
        if row['id'] in linked:continue
        method=str(row['payment_method'] or '').strip().casefold()
        closure=(row['category']=='Spese chiusura' and str(row['notes'] or '').startswith('Report chiusura #'))
        if method in CASH_METHODS or closure:
            add(row['store'],row['flow_date'],-amount(row['amount']),'report_expense' if closure else 'expense',row['notes'] or row['category'],row['created_by'],row['id'],row['ts'])
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

def reconciliation_rows(cur, day):
    return [dict(row) for row in cur.execute(
        f'SELECT * FROM cash_treasury_reconciliations WHERE day<={p()} ORDER BY day DESC,id DESC',(day,)
    ).fetchall()]


def reconciliation_balance(opening, stream, rows, store, day):
    if not opening or opening['start_date']>day:
        return None, 0
    previous=next((row for row in rows if row['store']==store and row['day']<day),None)
    base=previous['counted_cents'] if previous else opening['amount_cents']
    relevant=[row for row in stream if row['store']==store and row['day']<=day
              and row['day']>=opening['start_date'] and (not previous or row['day']>previous['day'])]
    cash=sum(row['delta'] for row in relevant if row['kind'] in ('report_income','report_expense'))
    admin_delta=sum(row['delta'] for row in relevant if row['kind'] in ('payment','repayment','adjustment'))
    return base+cash+admin_delta,cash


def confirmed_cash(cur, day, scope):
    rows=reconciliation_rows(cur,day)
    stores=ORDER if scope=='ALL' else (scope,)
    latest=[next((row for row in rows if row['store']==store),None) for store in stores]
    return [dict(label=LABELS[store],row=row) for store,row in zip(stores,latest)]


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
    def back():return RedirectResponse('/contanti-nei-negozi/tesoreria',303)
    @app.on_event('startup')
    def startup():
        with connect() as conn:ensure_schema(conn.cursor())
    @app.get('/contanti-nei-negozi')
    def daily_counts(request: Request, asof: str = ''):
        user = admin(request)
        today = api['_rome_today']().isoformat()
        day = day_value(asof) if asof else today
        count_date = date.fromisoformat(day)
        if count_date == date.min:
            raise HTTPException(422, 'Seleziona una data con un giorno precedente valido.')
        report_day = (count_date - timedelta(days=1)).isoformat()
        month_start = max(count_date.replace(day=1), date.min + timedelta(days=1))
        month = day[:7]
        month_end = count_date.replace(day=monthrange(count_date.year, count_date.month)[1]).isoformat()
        with connect() as conn:
            cur = conn.cursor()
            openings = {row['store']: row for row in cur.execute(
                'SELECT store,start_date,amount_cents FROM cash_daily_openings'
            ).fetchall()}
            first_day = min([month_start.isoformat(), *(row['start_date'] for row in openings.values())])
            balances_by_store = {row['store']: row['balance_cents'] for row in cur.execute(f'''
                SELECT o.store,o.amount_cents + COALESCE(SUM(c.amount_cents),0) AS balance_cents
                FROM cash_daily_openings o
                LEFT JOIN cash_daily_counts c
                  ON c.store=o.store AND c.day>=o.start_date AND c.day<={p()}
                WHERE o.start_date<={p()}
                GROUP BY o.store,o.amount_cents
            ''', (day, day)).fetchall()}
            counts = cur.execute(f'''
                SELECT store,day,amount_cents,counted_by FROM cash_daily_counts
                WHERE day>={p()} AND day<={p()} ORDER BY day DESC,store
            ''', (month_start.isoformat(), month_end)).fetchall()
            reports = cur.execute(f'''
                SELECT r.store,r.closure_date,r.created_by,r.lines_json,e.lines_json AS expenses_json
                FROM closure_reports r
                LEFT JOIN closure_report_expenses e ON e.report_id=r.id
                WHERE r.closure_date>={p()} AND r.closure_date<{p()}
            ''', ((date.fromisoformat(first_day) - timedelta(days=1)).isoformat(), month_end)).fetchall()
        declared = defaultdict(int)
        authors = defaultdict(set)
        for report in reports:
            key = (report['closure_date'], report['store'])
            cash = sum(line['cents'] for line in json.loads(report['lines_json']) if line['name'] == 'Cash')
            expenses = json.loads(report['expenses_json'] or '[]')
            spent = sum(expense['cents'] for expense in expenses if expense.get('mode') != 'cash_fund')
            declared[key] += cash - spent
            authors[key].add(report['created_by'])
        counts = [row for row in counts if row['store'] in openings
                  and row['day'] >= openings[row['store']]['start_date']]
        selected_counts = {row['store']: row['amount_cents'] for row in counts if row['day'] == day}
        cards = []
        for store in ORDER:
            opening = openings.get(store)
            balance = balances_by_store.get(store)
            expected = None
            if balance is not None:
                first_report = (date.fromisoformat(opening['start_date']) - timedelta(days=1)).isoformat()
                expected = opening['amount_cents'] + sum(
                    value for (closure_day, key), value in declared.items()
                    if key == store and first_report <= closure_day <= report_day
                )
            cards.append(dict(store=store, label=LABELS[store], company=company(store),
                              counted=selected_counts.get(store), declared=declared.get((report_day, store)),
                              opening=opening, balance=balance, expected=expected))
        history = []
        for row in counts:
            previous_day = (date.fromisoformat(row['day']) - timedelta(days=1)).isoformat()
            key = (previous_day, row['store'])
            expected = declared.get(key)
            if row['store'] in LABELS:
                history.append(dict(
                    day=row['day'], report_day=previous_day, store=row['store'], counted=row['amount_cents'],
                    declared=expected, difference=row['amount_cents']-expected if expected is not None else None,
                    authors=', '.join(sorted(authors[key])), counted_by=row['counted_by'],
                ))
        summaries = []
        for key, label in [*COMPANIES.items(), (None, 'Totale generale')]:
            members = [card for card in cards if key is None or card['company'] == key]
            recorded = [card['counted'] for card in members if card['counted'] is not None]
            complete_balance = all(card['balance'] is not None for card in members)
            summaries.append(dict(label=label, counted=sum(recorded) if recorded else None,
                                  balance=sum(card['balance'] for card in members) if complete_balance else None,
                                  expected=sum(card['expected'] for card in members) if complete_balance else None,
                                  recorded=len(recorded), stores=len(members)))
        return api['render']('cash_daily_counts.html', request=request, user=user,
                             cards=cards, summaries=summaries, companies=COMPANIES, labels=LABELS,
                             history=history, month=month,
                             asof=day, report_day=report_day, today=today, csrf=token(request))

    @app.get('/contanti-nei-negozi/tesoreria')
    def index(request:Request,asof:str=''):
        user=admin(request)
        today=api['_rome_today']().isoformat()
        cutoff=day_value(asof) if asof else today
        with connect() as conn:
            cur=conn.cursor()
            openings,stream,payments,_,_=events(cur)
            records=reconciliation_rows(cur,cutoff)
        cards=[]
        for store in ORDER:
            opening=openings.get(store)
            expected,cash=reconciliation_balance(opening,stream,records,store,cutoff)
            record=next((row for row in records if row['store']==store and row['day']==cutoff),None)
            first_count=not any(row['store']==store and row['day']<cutoff for row in records)
            if record and first_count:
                expected,cash=record['expected_cents'],record['cash_cents']
            cards.append(dict(first_count=first_count,store=store,label=LABELS[store],company=COMPANIES[company(store)],
                              opening=opening,expected=expected,cash=cash,record=record,
                              count_token=secrets.token_urlsafe(32),expense_token=secrets.token_urlsafe(32)))
        summaries=[]
        for label,members in [(name,[c for c in cards if c['company']==name]) for name in COMPANIES.values()]+[('Totale generale',cards)]:
            expected=sum(c['expected'] for c in members) if all(c['expected'] is not None for c in members) else None
            counted=sum(c['record']['counted_cents'] for c in members) if all(c['record'] for c in members) else None
            summaries.append(dict(label=label,expected=expected,counted=counted,
                                  difference=counted-expected if counted is not None and expected is not None else None))
        return api['render']('cash_treasury.html',request=request,user=user,brand=api['_current_store_scope'](request,user),
                             stores=api['STORES'],labels=LABELS,cards=cards,summaries=summaries,
                             reconciliations=records,payments=payments,today=today,asof=cutoff,csrf=token(request))
    @app.post('/contanti-nei-negozi/riconcilia')
    async def reconcile(request:Request):
        user,form=await form_for(request)
        store=store_key(form.get('store'))
        day=day_value(form.get('day'))
        key=submission(form)
        value=cents(form.get('amount',''))
        notes=str(form.get('notes','')).strip()
        if len(notes)>1000:
            raise HTTPException(422,'Nota troppo lunga.')
        with connect() as conn:
            cur=conn.cursor()
            begin(cur)
            require_base(cur,store,day)
            if cur.execute(f'SELECT id FROM cash_treasury_reconciliations WHERE token={p()}',(key,)).fetchone():
                return RedirectResponse('/contanti-nei-negozi/tesoreria?'+urlencode(dict(asof=day)),303)
            if cur.execute(f'SELECT id FROM cash_treasury_reconciliations WHERE store={p()} AND day>{p()}',(store,day)).fetchone():
                raise HTTPException(409,'Esiste un conteggio successivo per questo negozio. Aggiorna il conteggio più recente.')
            op,stream,_,_,_=events(cur)
            rows=reconciliation_rows(cur,day)
            expected,cash=reconciliation_balance(op[store],stream,rows,store,day)
            first_count=not any(row['store']==store and row['day']<day for row in rows)
            if first_count:
                expected,cash=value,0
            elif str(form.get('expected_cents',expected))!=str(expected):
                raise HTTPException(409,'Il saldo atteso è cambiato. Ricarica la pagina prima di confermare.')
            previous=next((row['counted_cents'] for row in rows if row['store']==store and row['day']==day),None)
            cur.execute(f'''INSERT INTO cash_treasury_reconciliations
                (store,day,expected_cents,counted_cents,cash_cents,previous_cents,notes,counted_by,counted_at,token)
                VALUES({','.join([p()]*10)})''',(store,day,expected,value,cash,previous,notes,user['username'],now(),key))
            logged(cur,user,store,f'Riconciliazione contanti del {day}')
        return RedirectResponse('/contanti-nei-negozi/tesoreria?'+urlencode(dict(asof=day)),303)

    @app.post('/contanti-nei-negozi/elimina-conteggio')
    async def delete_reconciliation(request:Request):
        user,form=await form_for(request)
        try:
            ident=int(str(form.get('id','')))
        except ValueError:
            raise HTTPException(422,'Conteggio non valido.')
        with connect() as conn:
            cur=conn.cursor()
            begin(cur)
            row=cur.execute(f'DELETE FROM cash_treasury_reconciliations WHERE id={p()} RETURNING store,day',(ident,)).fetchone()
            if not row:
                return back()
            logged(cur,user,row['store'],f'Eliminato conteggio #{ident} del {row["day"]}')
        return RedirectResponse('/contanti-nei-negozi/tesoreria?'+urlencode(dict(asof=row['day'])),303)

    @app.post('/contanti-nei-negozi/elimina-saldo-iniziale')
    async def delete_opening(request:Request):
        user,form=await form_for(request)
        store=store_key(form.get('store'))
        with connect() as conn:
            cur=conn.cursor()
            begin(cur)
            if cur.execute(f'SELECT id FROM cash_treasury_reconciliations WHERE store={p()}',(store,)).fetchone():
                raise HTTPException(409,'Elimina prima i conteggi del negozio.')
            cur.execute(f'DELETE FROM cash_treasury_openings WHERE store={p()}',(store,))
            logged(cur,user,store,'Eliminato saldo iniziale contanti')
        return back()

    @app.post('/contanti-nei-negozi/partenza')
    async def save_daily_opening(request: Request):
        user, form = await form_for(request)
        store = store_key(form.get('store'))
        day = day_value(form.get('day'))
        if day == date.min.isoformat():
            raise HTTPException(422, 'Seleziona una data con un giorno precedente valido.')
        value = cents(form.get('amount', ''))
        with connect() as conn:
            cur = conn.cursor()
            cur.execute(f'''INSERT INTO cash_daily_openings(store,start_date,amount_cents)
                VALUES({p()},{p()},{p()}) ON CONFLICT(store) DO UPDATE SET
                start_date=excluded.start_date,amount_cents=excluded.amount_cents''', (store,day,value))
            logged(cur,user,store,f'Saldo iniziale conteggi dal {day}')
        return RedirectResponse('/contanti-nei-negozi?'+urlencode(dict(asof=day)),303)

    @app.post('/contanti-nei-negozi/azzera-partenza')
    async def clear_daily_opening(request: Request):
        user, form = await form_for(request)
        store = store_key(form.get('store'))
        with connect() as conn:
            cur = conn.cursor()
            removed = cur.execute(
                f'DELETE FROM cash_daily_openings WHERE store={p()} RETURNING store', (store,)
            ).fetchone()
            if removed:
                logged(cur, user, store, 'Rimosso saldo iniziale dei conteggi')
        return RedirectResponse('/contanti-nei-negozi', 303)

    @app.post('/contanti-nei-negozi/conteggio')
    async def save_count(request:Request):
        user,form=await form_for(request)
        store=store_key(form.get('store'))
        day=day_value(form.get('day'))
        if day == date.min.isoformat():
            raise HTTPException(422, 'Seleziona una data con un giorno precedente valido.')
        raw=str(form.get('amount','')).strip()
        if not raw:
            raise HTTPException(422,'Inserisci il contante effettivamente contato.')
        value=cents(raw)
        with connect() as conn:
            cur=conn.cursor()
            opening = cur.execute(f'SELECT start_date FROM cash_daily_openings WHERE store={p()}', (store,)).fetchone()
            if not opening or day < opening['start_date']:
                raise HTTPException(422, 'Imposta prima il saldo iniziale del negozio per questa data.')
            cur.execute(f'''INSERT INTO cash_daily_counts(store,day,amount_cents,counted_by,counted_at)
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
            busy=cur.execute(f"SELECT s.payment_id FROM cash_treasury_sources s JOIN cash_treasury_payments t ON t.id=s.payment_id WHERE s.store={p()} AND t.cancelled_at=''",(store,)).fetchone()
            busy=busy or cur.execute(f'SELECT id FROM cash_treasury_adjustments WHERE store={p()}',(store,)).fetchone()
            busy=busy or cur.execute(f'SELECT id FROM cash_treasury_repayments WHERE source_store={p()} OR target_store={p()}',(store,store)).fetchone()
            busy=busy or cur.execute(f'SELECT id FROM cash_treasury_reconciliations WHERE store={p()}',(store,)).fetchone()
            if busy and cur.execute(f'SELECT store FROM cash_treasury_openings WHERE store={p()}',(store,)).fetchone():raise HTTPException(409,'Il saldo iniziale ha già movimenti collegati. Per correggere il contante usa una rettifica, mantenendo lo storico.')
            cur.execute(f'''INSERT INTO cash_treasury_openings(store,start_date,amount_cents,created_by,created_at) VALUES({','.join([p()]*5)})
                ON CONFLICT(store) DO UPDATE SET start_date=excluded.start_date,amount_cents=excluded.amount_cents,created_by=excluded.created_by,created_at=excluded.created_at''',(store,day,value,user['username'],now()))
            logged(cur,user,store,f'Saldo iniziale contanti del {day}: {value/100:.2f} €')
        return back()
    @app.post('/contanti-nei-negozi/paga')
    async def pay(request:Request):
        user,form=await form_for(request);key=submission(form);day=day_value(form.get('day',''))
        name=' '.join(str(form.get('name','')).split());target=str(form.get('company',''));beneficiary=store_key(str(form.get('beneficiary_store','')))
        if not name or len(name)>200:raise HTTPException(422,'Inserisci il nome della spesa (massimo 200 caratteri).')
        if target not in COMPANIES or company(beneficiary)!=target:raise HTTPException(422,'Il negozio beneficiario non appartiene all’azienda scelta.')
        total=cents(form.get('amount',''));sources={st:cents(str(form.get('from_'+st,'')).strip() or '0') for st in ORDER}
        single_store=form.get('single_store')=='1'
        if single_store:
            sources={st:total if st==beneficiary else 0 for st in ORDER}
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
            if not single_store:
                validate_cash(cur,api['_rome_today']().isoformat())
            validate_loans(cur,api['_rome_today']().isoformat())
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
            validate_loans(cur,api['_rome_today']().isoformat());logged(cur,user,row['beneficiary_store'],f'Annullato pagamento contanti #{ident}')
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
