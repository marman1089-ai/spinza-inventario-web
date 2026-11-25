"""Read-only comparison of reports, digitized till closures and counted cash."""
import json
from collections import defaultdict
from datetime import date, timedelta
from urllib.parse import urlencode
from fastapi import Request, HTTPException
from .db import connect
from .cash_treasury import p, amount, ensure_schema
from .cash_collection import collection_windows,CollectionIndex


def comparisons(reports, documents, receipts, windows=None):
    collections=CollectionIndex(collection_windows(reports,receipts) if windows is None else windows)
    groups = defaultdict(lambda: dict(reports=[], documents=[], receipt=None))
    for row in reports:
        groups[(row['store'], str(row['closure_date']))]['reports'].append(dict(row))
    for row in documents:
        day = date.fromisoformat(str(row['closure_date'])) - timedelta(days=1)
        groups[(row['store'], day.isoformat())]['documents'].append(dict(row))
    for row in receipts:
        day = date.fromisoformat(str(row['day'])) - timedelta(days=1)
        groups[(row['store'], day.isoformat())]['receipt'] = dict(row)
    result = []
    for (store, day), group in sorted(groups.items(), key=lambda item: item[0][1], reverse=True):
        rs, ds = group['reports'], group['documents']
        channels = defaultdict(int)
        spent = 0
        for report in rs:
            for line in json.loads(report['lines_json']):
                channels[line['name']] += int(line['cents'])
            spent += sum(int(x['cents']) for x in json.loads(report['expenses_json'] or '[]') if x.get('mode') != 'cash_fund')
        # Several photos on the same day may be duplicates or separate shifts: never silently sum them.
        details = json.loads(ds[0]['details_json'] or '{}') if len(ds) == 1 and ds[0]['digital_id'] is not None else {}
        metrics = []
        for label, channel, field in [('Contanti incassati', 'Cash', 'cash'), ('POS / carte', 'POS', 'card')]:
            reported = channels.get(channel) if rs else None
            actual = amount(details[field]) if details.get(field) is not None else None
            metrics.append(dict(label=label, reported=reported, actual=actual, difference=actual-reported if actual is not None and reported is not None else None))
        collection=collections.for_report(store,day)
        expected = collection['declared'] if collection else (channels.get('Cash',0)-spent if rs and 'Cash' in channels else None)
        found = collection['amount_cents'] if collection else None
        cash_difference = collection['difference'] if collection else None
        complete = all(m['difference'] is not None for m in metrics) and cash_difference is not None
        different = any(m['difference'] not in (None, 0) for m in metrics) or cash_difference not in (None, 0)
        status = 'Differenze' if different else ('Coincide' if complete else 'Da completare')
        result.append(dict(group, store=store, day=day, next_day=(date.fromisoformat(day)+timedelta(days=1)).isoformat(), metrics=metrics,
                           spent=collection['spent'] if collection else (spent if rs else None), collection=collection,collection_day=collection['day'] if collection else (date.fromisoformat(day)+timedelta(days=1)).isoformat(),expected=expected, found=found, cash_difference=cash_difference, status=status,
                           report_url='/report-chiusura?'+urlencode(dict(store=store, day=day))))
    return result


def install(app, api):
    @app.get('/gestionale/controllo-dati')
    def index(request: Request, store: str = '', start: str = '', end: str = ''):
        user = api['require_login'](request)
        if not user or not api['is_admin'](request):
            raise HTTPException(403, 'Sezione riservata agli admin.')
        scope = api['_effective_store'](request, user)
        store = store or scope
        if store != 'ALL' and store not in api['STORES']:
            raise HTTPException(422, 'Negozio non valido.')
        if scope != 'ALL' and store != scope:
            raise HTTPException(403, 'Seleziona il negozio nel menu principale.')
        today = api['_rome_today']()
        start = start or today.replace(day=1).isoformat()
        end = end or today.isoformat()
        try:
            first, last = date.fromisoformat(start), date.fromisoformat(end)
            if first > last or first.year < 2 or last.year > 9998 or (last-first).days > 366:
                raise ValueError()
        except ValueError:
            raise HTTPException(422, 'Seleziona un periodo valido fino a 367 giorni.')
        extra = '' if store == 'ALL' else f' AND r.store={p()}'
        args = () if store == 'ALL' else (store,)
        with connect() as conn:
            cur = conn.cursor()
            ensure_schema(cur)
            reports = cur.execute(f'''SELECT r.*,e.lines_json AS expenses_json FROM closure_reports r
                LEFT JOIN closure_report_expenses e ON e.report_id=r.id
                WHERE 1=1{extra}''',args).fetchall()
            lo, hi = (first+timedelta(days=1)).isoformat(), (last+timedelta(days=1)).isoformat()
            documents = cur.execute(f'''SELECT r.id,r.store,r.closure_date,d.doc_id AS digital_id,t.details_json
                FROM closures r LEFT JOIN digital_documents d ON d.kind='chiusure' AND d.doc_id=r.id AND d.store=r.store
                LEFT JOIN document_details t ON t.kind='chiusure' AND t.doc_id=d.doc_id
                WHERE r.closure_date BETWEEN {p()} AND {p()}{extra}''',(lo,hi,*args)).fetchall()
            all_receipts=cur.execute(f'SELECT r.* FROM cash_store_receipts r WHERE 1=1{extra}',args).fetchall()
            receipts=[r for r in all_receipts if lo<=str(r['day'])[:10]<=hi]
        windows=collection_windows(reports,all_receipts)
        rows = comparisons([r for r in reports if start<=str(r['closure_date'])[:10]<=end],documents,receipts,windows)
        return api['render']('reconciliation.html', request=request,user=user,brand=scope, stores=api['STORES'],store=store,start=start,end=end,rows=rows,
                             counts={s:sum(r['status']==s for r in rows) for s in ('Coincide','Differenze','Da completare')})
