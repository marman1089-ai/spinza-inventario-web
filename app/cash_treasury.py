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
    cur.execute('''CREATE TABLE IF NOT EXISTS cash_store_receipts (
        store TEXT NOT NULL, day TEXT NOT NULL, amount_cents BIGINT NOT NULL,
        counted_by TEXT NOT NULL, PRIMARY KEY(store,day))''')
    cur.execute('''CREATE TABLE IF NOT EXISTS cash_company_spending (
        token TEXT PRIMARY KEY, day TEXT NOT NULL, description TEXT NOT NULL,
        reburger_cents BIGINT NOT NULL, sc_cents BIGINT NOT NULL, recorded_by TEXT NOT NULL)''')
    cur.execute('''CREATE TABLE IF NOT EXISTS cash_treasury_openings (
        store TEXT PRIMARY KEY,start_date TEXT NOT NULL,amount_cents BIGINT NOT NULL,
        created_by TEXT NOT NULL,created_at TEXT NOT NULL)''')
    cur.execute(f'''CREATE TABLE IF NOT EXISTS cash_treasury_reconciliations (
        id {ident}, store TEXT NOT NULL, day TEXT NOT NULL, expected_cents BIGINT NOT NULL,
        counted_cents BIGINT NOT NULL, cash_cents BIGINT NOT NULL, previous_cents BIGINT,
        notes TEXT NOT NULL, counted_by TEXT NOT NULL, counted_at TEXT NOT NULL, token TEXT NOT NULL UNIQUE)''')
    cur.execute('CREATE INDEX IF NOT EXISTS ix_treasury_reconciliation ON cash_treasury_reconciliations(store,day,id)')
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


def install(app, api):
    root = '/contanti-nei-negozi'

    def admin(request):
        user = api['require_login'](request)
        if not user or not api['is_admin'](request):
            raise HTTPException(403, 'Sezione riservata agli admin.')
        return user

    def csrf(request):
        return request.session.setdefault('treasury_csrf', secrets.token_urlsafe(32))

    async def read_form(request):
        user = admin(request)
        form = await request.form()
        if not secrets.compare_digest(str(form.get('csrf', '')), csrf(request)):
            raise HTTPException(403, 'Sessione scaduta: ricarica la pagina.')
        return user, form

    def selected_day(value):
        try:
            day = date.fromisoformat(value) if value else api['_rome_today']()
        except (TypeError, ValueError):
            raise HTTPException(422, 'Data non valida.')
        if day == date.min or day > api['_rome_today']():
            raise HTTPException(422, 'Seleziona una data valida fino a oggi.')
        return day

    def show(request, user, view, **context):
        return api['render']('cash_daily_counts.html', request=request, user=user, view=view,
                             root=root, csrf=csrf(request), labels=LABELS, stores_order=ORDER,
                             companies=COMPANIES, today=api['_rome_today']().isoformat(), **context)

    @app.on_event('startup')
    def startup():
        with connect() as conn:
            ensure_schema(conn.cursor())

    @app.get(root)
    def index(request: Request):
        return show(request, admin(request), 'home')

    @app.get(root+'/incassi')
    def receipts(request: Request, day: str = ''):
        user = admin(request)
        chosen = selected_day(day)
        first = chosen.replace(day=1)
        last = chosen.replace(day=monthrange(chosen.year, chosen.month)[1])
        previous = (chosen - timedelta(days=1)).isoformat()
        with connect() as conn:
            cur = conn.cursor()
            records = cur.execute(f"""
                SELECT * FROM cash_store_receipts WHERE day BETWEEN {p()} AND {p()}
                ORDER BY day DESC,store
            """, (first.isoformat(), last.isoformat())).fetchall()
            reports = cur.execute(f"""
                SELECT r.store,r.closure_date,r.created_by,r.lines_json,e.lines_json AS expenses_json
                FROM closure_reports r
                LEFT JOIN closure_report_expenses e ON e.report_id=r.id
                WHERE r.closure_date BETWEEN {p()} AND {p()}
            """, ((max(first,date.min+timedelta(days=1))-timedelta(days=1)).isoformat(),
                  (last-timedelta(days=1)).isoformat())).fetchall()
        declared = defaultdict(int)
        authors = defaultdict(set)
        for report in reports:
            key = (report['store'], str(report['closure_date']))
            cash = sum(line['cents'] for line in json.loads(report['lines_json']) if line['name']=='Cash')
            spent = sum(line['cents'] for line in json.loads(report['expenses_json'] or '[]')
                        if line.get('mode') != 'cash_fund')
            declared[key] += cash-spent
            authors[key].add(report['created_by'])
        history = []
        for row in records:
            report_day = (date.fromisoformat(row['day'])-timedelta(days=1)).isoformat()
            key = (row['store'], report_day)
            expected = declared.get(key)
            history.append(dict(row, report_day=report_day, declared=expected,
                                authors=', '.join(sorted(authors[key])),
                                difference=row['amount_cents']-expected if expected is not None else None))
        current = {row['store']: row for row in history if row['day']==chosen.isoformat()}
        return show(request,user,'receipts',day=chosen.isoformat(),previous=previous,current=current,
                    declared={store:declared.get((store,previous)) for store in ORDER},history=history)

    @app.post(root+'/incassi')
    async def save_receipts(request: Request):
        user, form = await read_form(request)
        day = selected_day(form.get('day')).isoformat()
        amounts = [(store,cents(form[store])) for store in ORDER if str(form.get(store,'')).strip()]
        if not amounts:
            raise HTTPException(422, 'Inserisci almeno un importo.')
        with connect() as conn:
            cur = conn.cursor()
            for store, value in amounts:
                cur.execute(f"""
                    INSERT INTO cash_store_receipts(store,day,amount_cents,counted_by)
                    VALUES({p()},{p()},{p()},{p()}) ON CONFLICT(store,day) DO UPDATE
                    SET amount_cents=excluded.amount_cents,counted_by=excluded.counted_by
                """, (store,day,value,user['username']))
        return RedirectResponse(root+'/incassi?'+urlencode(dict(day=day)),303)

    @app.get(root+'/spese')
    def spending(request: Request, day: str = ''):
        user = admin(request)
        chosen = selected_day(day)
        last = chosen.replace(day=monthrange(chosen.year,chosen.month)[1])
        with connect() as conn:
            rows = conn.cursor().execute(f"""
                SELECT * FROM cash_company_spending WHERE day BETWEEN {p()} AND {p()}
                ORDER BY day DESC,description,token
            """, (chosen.replace(day=1).isoformat(),last.isoformat())).fetchall()
        return show(request,user,'spending',day=chosen.isoformat(),expenses=rows,submission=secrets.token_urlsafe(32))

    @app.post(root+'/spese')
    async def save_spending(request: Request):
        user, form = await read_form(request)
        day = selected_day(form.get('day')).isoformat()
        value = cents(form.get('amount',''))
        share = cents(form.get('reburger_percent',''))
        description = str(form.get('description','')).strip()
        token = str(form.get('submission',''))
        if not value or share > 10000 or not description or len(description)>250:
            raise HTTPException(422, 'Controlla descrizione, importo e percentuale (da 0 a 100).')
        if not re.fullmatch(r'[A-Za-z0-9_-]{43}',token):
            raise HTTPException(422, 'Ricarica il modulo della spesa.')
        reburger = (value*share+5000)//10000
        with connect() as conn:
            conn.cursor().execute(f"""
                INSERT INTO cash_company_spending(token,day,description,reburger_cents,sc_cents,recorded_by)
                VALUES({p()},{p()},{p()},{p()},{p()},{p()}) ON CONFLICT(token) DO NOTHING
            """, (token,day,description,reburger,value-reburger,user['username']))
        return RedirectResponse(root+'/spese?'+urlencode(dict(day=day)),303)

    @app.get(root+'/bilanci')
    def cash_balances(request: Request):
        user = admin(request)
        today = api['_rome_today']().isoformat()
        with connect() as conn:
            cur = conn.cursor()
            totals = cur.execute(f"""
                SELECT store,SUM(amount_cents) AS income FROM cash_store_receipts WHERE day<={p()} GROUP BY store
            """, (today,)).fetchall()
            expenses = cur.execute(f"""
                SELECT COALESCE(SUM(reburger_cents),0) AS reburger,COALESCE(SUM(sc_cents),0) AS sc
                FROM cash_company_spending WHERE day<={p()}
            """, (today,)).fetchone()
        income_by_store = {row['store']:row['income'] for row in totals}
        rows = []
        for key,label in COMPANIES.items():
            income = sum(income_by_store.get(store,0) for store in ORDER if company(store)==key)
            rows.append(dict(label=label,income=income,spent=expenses[key],balance=income-expenses[key]))
        rows.append(dict(label='Totale generale',**{key:sum(row[key] for row in rows) for key in ('income','spent','balance')}))
        return show(request,user,'balances',balances=rows,income_by_store=income_by_store)
