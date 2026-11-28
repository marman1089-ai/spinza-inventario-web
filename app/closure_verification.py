"""Admin-only, read-only verification of daily reports against digital till closures."""
import json
import unicodedata
from collections import defaultdict
from datetime import date, timedelta
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from urllib.parse import urlencode
from fastapi import HTTPException, Request
from .db import connect
from .cash_treasury import ORDER, LABELS as STORE_LABELS, p

ROOT='/gestionale/verifica-chiusure'
LABELS={'cash':'Contanti','card':'POS / carte','uncollected':'Non riscosso · Glovo + Deliveroo'}
ALIASES={'cash':'cash','contanti':'cash','contante':'cash','pos':'card','carta':'card','carte':'card',
         'pos carte':'card','ticket':'tickets','tickets':'tickets','buoni':'tickets','buoni pasto':'tickets',
         'non riscosso':'uncollected','non riscossi':'uncollected','glovo':'glovo','deliveroo':'deliveroo'}


def channel(name):
    value=''.join(c for c in unicodedata.normalize('NFKD',str(name or '').lower()) if not unicodedata.combining(c))
    value=' '.join(''.join(c if c.isalnum() else ' ' for c in value).split())
    return ALIASES.get(value)


def unpack(value, fallback):
    try:
        result=json.loads(value or '')
        return result if isinstance(result,type(fallback)) else fallback
    except (ValueError,TypeError):return fallback


def cents(value):
    if value is None or isinstance(value,bool):return None
    try:
        number=Decimal(str(value))
        return int((number*100).quantize(Decimal('1'),rounding=ROUND_HALF_UP)) if number.is_finite() else None
    except (InvalidOperation,ValueError,TypeError):return None


def daily_comparisons(reports, documents, first, last, stores):
    by_report=defaultdict(list);by_document=defaultdict(list)
    for row in reports:by_report[row['store'],str(row['closure_date'])[:10]].append(dict(row))
    for row in documents:
        day=date.fromisoformat(str(row['closure_date'])[:10])-timedelta(days=1)
        by_document[row['store'],day.isoformat()].append(dict(row))
    sections=[]
    for store,label in stores.items():
        rows=[];day=last
        while day>=first:
            ds=day.isoformat();rs=by_report[store,ds];docs=by_document[store,ds]
            reported=defaultdict(int)
            for r in rs:
                for line in unpack(r.get('lines_json'),[]):
                    key=channel(line.get('name'))
                    if key is not None and isinstance(line.get('cents'),int):reported[key]+=line['cents']
            delivery_keys=[key for key in ('glovo','deliveroo') if key in reported]
            conflicting_delivery=False
            if delivery_keys:
                delivery_total=sum(reported[key] for key in delivery_keys)
                conflicting_delivery='uncollected' in reported and reported['uncollected']!=delivery_total
                reported['uncollected']=None if conflicting_delivery else delivery_total
            details=unpack(docs[0].get('details_json'),{}) if len(docs)==1 and docs[0].get('digital_id') is not None else {}
            metrics=[];issues=[]
            if conflicting_delivery:issues.append('Non riscosso esplicito diverso da Glovo + Deliveroo: controllare il report')
            if not rs and docs:issues.append('Manca il report della chiusura')
            if rs and not docs:issues.append('Manca la chiusura della cassa')
            if len(docs)>1:issues.append(f'{len(docs)} documenti: distinguere copie e turni prima del confronto')
            if len(docs)==1 and docs[0].get('digital_id') is None:
                state=docs[0].get('job_state')
                issues.append('Lettura digitale in corso' if state in ('pending','working') else 'Chiusura presente solo come foto o lettura non disponibile')
            for key in LABELS:
                actual=cents(details.get(key));value=reported.get(key)
                required=True
                difference=value-actual if value is not None and actual is not None else None
                metric=dict(key=key,label=LABELS[key],reported=value,actual=actual,difference=difference,required=required)
                metrics.append(metric)
                if rs and value is None:issues.append(f'{LABELS[key]} non indicati nel report')
                if len(docs)==1 and docs[0].get('digital_id') is not None and required and actual is None:issues.append(f'{LABELS[key]} non leggibili nella scheda digitale')
            missing=sum(-m['difference'] for m in metrics if m['difference'] is not None and m['difference']<0)
            excess=sum(m['difference'] for m in metrics if m['difference'] is not None and m['difference']>0)
            if not rs and not docs:status='Nessun dato'
            elif missing or excess:status='Differenze'
            elif issues:status='Da completare'
            else:status='Coincide'
            rows.append(dict(day=ds,till_day=(day+timedelta(days=1)).isoformat(),store=store,reports=rs,documents=docs,
                             metrics=metrics,issues=issues,status=status,missing=missing,excess=excess,
                             report_url='/report-chiusura?'+urlencode(dict(store=store,day=ds))))
            day-=timedelta(days=1)
        counts={s:sum(r['status']==s for r in rows) for s in ('Coincide','Differenze','Da completare','Nessun dato')}
        sections.append(dict(store=store,label=label,rows=rows,counts=counts,
                             missing=sum(r['missing'] for r in rows),excess=sum(r['excess'] for r in rows)))
    return sections


def install(app, api):
    @app.get(ROOT)
    def index(request:Request,store:str='ALL',start:str='',end:str='',status:str='tutti'):
        user=api['require_login'](request)
        if not user or not api['is_admin'](request):raise HTTPException(403,'Sezione riservata agli admin.')
        stores={key:STORE_LABELS[key] for key in ORDER if key in api['STORES']}
        if store!='ALL' and store not in stores:raise HTTPException(422,'Negozio non valido.')
        if status not in ('tutti','differenze','incompleti','coincidenti','senza-dati'):raise HTTPException(422,'Filtro non valido.')
        today=api['_rome_today']()
        try:
            first=date.fromisoformat(start) if start else today.replace(day=1)
            last=date.fromisoformat(end) if end else today
            if first>last or first.year<2 or last.year>9998 or (last-first).days>366:raise ValueError()
        except ValueError:raise HTTPException(422,'Seleziona un periodo valido fino a 367 giorni.')
        if store!='ALL':stores={store:stores[store]}
        marks=','.join(p() for _ in stores);keys=tuple(stores)
        with connect() as conn:
            cur=conn.cursor()
            reports=cur.execute(f'SELECT id,store,closure_date,lines_json FROM closure_reports WHERE store IN ({marks}) AND closure_date BETWEEN {p()} AND {p()} ORDER BY id',(*keys,first.isoformat(),last.isoformat())).fetchall()
            documents=cur.execute(f'''SELECT c.id,c.store,c.closure_date,d.doc_id AS digital_id,t.details_json,j.state AS job_state
                FROM closures c LEFT JOIN digital_documents d ON d.kind='chiusure' AND d.doc_id=c.id AND d.store=c.store
                LEFT JOIN document_details t ON t.kind='chiusure' AND t.doc_id=c.id
                LEFT JOIN document_ai_jobs j ON j.kind='chiusure' AND j.doc_id=c.id AND j.store=c.store
                WHERE c.store IN ({marks}) AND c.closure_date BETWEEN {p()} AND {p()} ORDER BY c.id''',
                (*keys,(first+timedelta(days=1)).isoformat(),(last+timedelta(days=1)).isoformat())).fetchall()
        sections=daily_comparisons(reports,documents,first,last,stores)
        counts={s:sum(section['counts'][s] for section in sections) for s in ('Coincide','Differenze','Da completare','Nessun dato')}
        chosen={'differenze':'Differenze','incompleti':'Da completare','coincidenti':'Coincide','senza-dati':'Nessun dato'}.get(status)
        for section in sections:
            if chosen:section['rows']=[row for row in section['rows'] if row['status']==chosen]
        return api['render']('closure_verification.html',request=request,user=user,brand=api['_effective_store'](request,user),
                             sections=sections,counts=counts,start=first.isoformat(),end=last.isoformat(),status=status,store=store,root=ROOT)
