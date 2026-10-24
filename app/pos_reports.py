"""Typed parsers and reviewed import of the four supplied cash-register reports."""
import csv
import hashlib
import io
import json
import re
from datetime import date,datetime,timedelta
from decimal import Decimal
from fastapi import Request,UploadFile,File,Form,HTTPException
from fastapi.responses import RedirectResponse,Response
from .db import connect,using_postgres
from .control import ph,lock,suffix,number,iso,timestamp,csvsafe,money,today

REPORTS={'paid':'Pagato al giorno','methods':'METODO DI PAGAMENTO GIORNALIERO',
         'vat':'TAVOLA IVA PER GIORNO / FILIALE','products':'Report fatturato settimanale'}
CATEGORIES=['Panini','Bevande','Contorni','Salse','Extra','Modifiche','Da classificare']

def parse_date(value):
    for fmt in ('%Y-%m-%d','%d-%m-%y','%d/%m/%Y','%d-%m-%Y'):
        try:return datetime.strptime(value.strip(),fmt).date().isoformat()
        except ValueError:pass
    raise HTTPException(422,'Data CSV non riconosciuta: '+str(value))

def category(name):
    n=name.strip().upper()
    n=re.sub(r'\s*\(?DELIVERY\)?\s*','',n).strip()
    if n.startswith('-'):return 'Modifiche'
    if n in ['JUICY LUCY','OG SMASH','REDHEAD','SIDECHICK','SMOKED BONE']:return 'Panini'
    if any(s in n for s in ['COCA COLA','ACQUA','FANTA','SPRITE','BEER']):return 'Bevande'
    if n in ['CHICKEN POPS','CLASSIC FIRES','SWEET POTATOES','WILD HERB']:return 'Contorni'
    if n in ['1000 ISLAND','BBQ','HONEY MUSTARD','KETCHUP','MAYO']:return 'Salse'
    if n.startswith('+') or any(s in n for s in ['BACON','FORMAGGIO','BUN','JALAPENO','FRITTE','MAIONESE']):return 'Extra'
    return 'Da classificare'

def parse_csv(raw,kind,start='',end=''):
    if kind not in REPORTS:raise HTTPException(422,'Tipo report non valido.')
    if len(raw)>10*1024*1024:raise HTTPException(422,'CSV troppo grande (massimo 10 MB).')
    try:text=raw.decode('utf-8-sig')
    except UnicodeDecodeError:text=raw.decode('cp1252')
    delim=';' if text.splitlines()[0].count(';')>text.splitlines()[0].count(',') else ','
    grid=list(csv.reader(io.StringIO(text),delimiter=delim))
    if not grid:raise HTTPException(422,'CSV vuoto.')
    warnings=[];rows=[];total_line=None
    if kind=='products':
        header=next((i for i,r in enumerate(grid[:8]) if len(r)>=6 and r[0].strip().casefold()=='name' and r[1].strip().upper()=='ID'),None)
        if header is None:raise HTTPException(422,'Il file non è il Report fatturato settimanale atteso.')
        first,last=iso(start),iso(end)
        if first>last:raise HTTPException(422,'Periodo non valido.')
        seen=set()
        for r in grid[header+1:]:
            if not r or not any(c.strip() for c in r):continue
            if len(r)<6:raise HTTPException(422,'Riga prodotto incompleta.')
            if r[0].strip().casefold()=='total':
                total_line=[number(v) for v in r[2:6]];continue
            pid=r[1].strip()
            if not pid or pid in seen:raise HTTPException(422,'ID prodotto mancante o ripetuto nel file.')
            seen.add(pid)
            rows.append(dict(product_code=pid,name=r[0].strip(),quantity=number(r[2]),before_discount=number(r[3]),
                net=number(r[4]),gross=number(r[5]),category=category(r[0])))
        if not rows:raise HTTPException(422,'Nessun prodotto nel CSV.')
        if total_line:
            for i,key in enumerate(['quantity','before_discount','net','gross']):
                actual=sum(Decimal(str(r[key])) for r in rows)
                if abs(actual-Decimal(str(total_line[i])))>Decimal('0.02'):
                    warnings.append('Il totale dichiarato di '+key+' non coincide con la somma delle righe: controllare.')
        else:warnings.append('Riga totale assente: totali calcolati dalle righe prodotto.')
    else:
        headers=[c.strip() for c in grid[0]]
        required={'paid':['DATO','PAGATO'],'methods':['DATO','SERVO','PAGATO'],
            'vat':['DATO','FILIALE','TIPO DI IVA','TURNOVER INCL.','TURNOVER EXCL.','IMPORTO IVA']}[kind]
        if not all(c in headers for c in required):raise HTTPException(422,'Colonne non compatibili con '+REPORTS[kind]+'.')
        seen=set()
        for line in grid[1:]:
            if not line or not any(v.strip() for v in line):continue
            if len(line)!=len(headers):raise HTTPException(422,'Riga CSV incompleta.')
            r=dict(zip(headers,line));d=parse_date(r['DATO'])
            if kind=='paid':
                row=dict(date=d,gross=number(r['PAGATO']));key=(d,)
            elif kind=='methods':
                original=r['SERVO'].strip()
                method={'contanti':'Contanti','carta':'POS','pagamento non riscosso':'Delivery'}.get(original.casefold(),original)
                if method not in ['Contanti','POS','Delivery']:warnings.append('Metodo non previsto conservato: '+original)
                row=dict(date=d,method=method,original_method=original,gross=number(r['PAGATO']));key=(d,original.casefold())
            else:
                row=dict(date=d,branch=r['FILIALE'].strip(),vat_code=r['TIPO DI IVA'].strip(),gross=number(r['TURNOVER INCL.']),net=number(r['TURNOVER EXCL.']),vat=number(r['IMPORTO IVA']))
                key=(d,row['branch'],row['vat_code'])
                if abs(Decimal(str(row['gross']))-Decimal(str(row['net']))-Decimal(str(row['vat'])))>Decimal('0.02'):
                    warnings.append('Lordo, imponibile e IVA non quadrano il '+d+'.')
            if key in seen:raise HTTPException(422,'Riga duplicata nello stesso file: '+d)
            seen.add(key);rows.append(row)
        if not rows:raise HTTPException(422,'CSV senza dati.')
        first,last=min(r['date'] for r in rows),max(r['date'] for r in rows)
        if kind=='vat' and len({r['branch'] for r in rows})>1:
            raise HTTPException(422,'Il CSV contiene più filiali. Esporta un file per filiale e scegli il negozio corretto.')
    return dict(rows=rows,start=first,end=last,warnings=list(dict.fromkeys(warnings)),totals=dict(
        gross=str(sum(Decimal(str(r['gross'])) for r in rows)),
        net=str(sum(Decimal(str(r.get('net',0))) for r in rows)),
        quantity=str(sum(Decimal(str(r.get('quantity',0))) for r in rows))))


def init_pos():
    ident='SERIAL PRIMARY KEY' if using_postgres() else 'INTEGER PRIMARY KEY AUTOINCREMENT'
    blob='BYTEA' if using_postgres() else 'BLOB'
    with connect() as conn:
        cur=conn.cursor()
        cur.execute(f'''CREATE TABLE IF NOT EXISTS pos_import_batches (
            id {ident},store TEXT NOT NULL,kind TEXT NOT NULL,start_date TEXT NOT NULL,end_date TEXT NOT NULL,
            filename TEXT NOT NULL,digest TEXT NOT NULL,raw_csv {blob} NOT NULL,parsed_json TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'draft',created_by TEXT NOT NULL,created_at TEXT NOT NULL)''')
        cur.execute('CREATE TABLE IF NOT EXISTS pos_import_locks (store TEXT PRIMARY KEY)')
        cur.execute('CREATE INDEX IF NOT EXISTS ix_pos_batch_scope ON pos_import_batches(store,kind,status,start_date,end_date)')
        cur.execute('''CREATE TABLE IF NOT EXISTS pos_product_settings (
            store TEXT NOT NULL,product_code TEXT NOT NULL,category TEXT NOT NULL,base_name TEXT NOT NULL,
            PRIMARY KEY(store,product_code))''')
        cur.execute(f'''CREATE TABLE IF NOT EXISTS pos_recipes (
            id {ident},store TEXT NOT NULL,product_code TEXT NOT NULL,inventory_product_id INTEGER NOT NULL,
            qty_per_sale DOUBLE PRECISION NOT NULL,UNIQUE(store,product_code,inventory_product_id))''')


def install(app,api):
    stores=api['STORES'];render=api['render']
    def auth(request):
        user=api['require_login'](request)
        if not user:raise HTTPException(401,'Accedi al gestionale.')
        if user['role']!='admin':raise HTTPException(403,'Solo amministratore.')
        return user,api['_effective_store'](request,user)
    def context(user,store,**kw):return dict(user=user,brand=store,active_store=store,stores=stores,reports=REPORTS,categories=CATEGORIES,**kw)
    def batch(cur,bid,store):
        row=cur.execute(f'SELECT * FROM pos_import_batches WHERE id={ph()}',(bid,)).fetchone()
        if not row or (store!='ALL' and row['store']!=store):raise HTTPException(404,'Importazione non disponibile.')
        return row
    def overlaps(cur,row):
        return cur.execute(f"SELECT id,start_date,end_date,filename FROM pos_import_batches WHERE store={ph()} AND kind={ph()} AND status='active' AND start_date<={ph()} AND end_date>={ph()} ORDER BY id",
            (row['store'],row['kind'],row['end_date'],row['start_date'])).fetchall()
    @app.on_event('startup')
    def startup_pos():init_pos()

    @app.get('/admin/cassa')
    def index(request: Request):
        user,store=auth(request)
        with connect() as conn:
            cur=conn.cursor();sql='SELECT * FROM pos_import_batches WHERE 1=1';params=[]
            if store!='ALL':sql+=f' AND store={ph()}';params=[store]
            batches=cur.execute(sql+' ORDER BY id DESC LIMIT 80',tuple(params)).fetchall()
        return render('pos_imports.html',**context(user,store,batches=batches))

    @app.post('/admin/cassa/importa')
    async def upload(request: Request,kind: str=Form(...),store: str=Form(...),start: str=Form(''),end: str=Form(''),file:UploadFile=File(...)):
        user,scope=auth(request)
        if store not in stores or (scope!='ALL' and store!=scope):raise HTTPException(403,'Seleziona un negozio consentito.')
        raw=await file.read(10*1024*1024+1);parsed=parse_csv(raw,kind,start,end);digest=hashlib.sha256(raw).hexdigest()
        with connect() as conn:
            cur=conn.cursor()
            existing=cur.execute(f"SELECT id FROM pos_import_batches WHERE store={ph()} AND kind={ph()} AND digest={ph()} AND start_date={ph()} AND end_date={ph()} AND status IN ('draft','active') ORDER BY id DESC LIMIT 1",
                (store,kind,digest,parsed['start'],parsed['end'])).fetchone()
            if existing:return RedirectResponse(f'/admin/cassa/importazione/{existing["id"]}',303)
            cur.execute(f'INSERT INTO pos_import_batches(store,kind,start_date,end_date,filename,digest,raw_csv,parsed_json,created_by,created_at) VALUES({",".join([ph()]*10)}) RETURNING id',
                (store,kind,parsed['start'],parsed['end'],file.filename or 'report.csv',digest,raw,json.dumps(parsed,ensure_ascii=False),user['username'],timestamp()))
            bid=cur.fetchone()['id']
        return RedirectResponse(f'/admin/cassa/importazione/{bid}',303)

    @app.get('/admin/cassa/importazione/{bid}')
    def preview(request: Request,bid:int):
        user,store=auth(request)
        with connect() as conn:
            cur=conn.cursor();row=batch(cur,bid,store);conflicts=overlaps(cur,row)
        return render('pos_preview.html',**context(user,store,batch=row,parsed=json.loads(row['parsed_json']),conflicts=conflicts))

    @app.post('/admin/cassa/importazione/{bid}/conferma')
    async def confirm(request:Request,bid:int):
        user,store=auth(request);form=await request.form()
        with connect() as conn:
            lock(conn);cur=conn.cursor();row=batch(cur,bid,store)
            # All imports of a branch serialize on its users row in PostgreSQL, including the no-conflict case.
            if using_postgres():
                cur.execute(f'INSERT INTO pos_import_locks(store) VALUES({ph()}) ON CONFLICT(store) DO NOTHING',(row['store'],))
                cur.execute(f'SELECT store FROM pos_import_locks WHERE store={ph()} FOR UPDATE',(row['store'],)).fetchone()
            cur.execute(f'SELECT id FROM pos_import_batches WHERE id={ph()}'+suffix(),(bid,)).fetchone()
            row=batch(cur,bid,store)
            if row['status']=='active':return RedirectResponse('/admin/cassa',303)
            if row['status']!='draft':raise HTTPException(409,'Importazione già sostituita.')
            conflicts=overlaps(cur,row)
            if any(r['start_date']!=row['start_date'] or r['end_date']!=row['end_date'] for r in conflicts):
                raise HTTPException(409,'Periodo parzialmente sovrapposto: esporta un intervallo non sovrapposto oppure lo stesso intervallo del report da sostituire.')
            if conflicts and form.get('replace')!='yes':raise HTTPException(409,'Conferma la sostituzione del report dello stesso periodo.')
            for old in conflicts:
                cur.execute(f"UPDATE pos_import_batches SET status='replaced' WHERE id={ph()}",(old['id'],))
            cur.execute(f"UPDATE pos_import_batches SET status='active' WHERE id={ph()}",(bid,))
            api['_log'](cur,store=row['store'],username=user['username'],action='IMPORT_CASSA',category=row['kind'],name=f'{REPORTS[row["kind"]]} | {row["start_date"]} → {row["end_date"]} | importazione #{bid}',delta=0)
        return RedirectResponse('/admin/cassa/report?start='+row['start_date']+'&end='+row['end_date'],303)

    @app.post('/admin/cassa/prodotto')
    def setting(request:Request,store:str=Form(...),product_code:str=Form(...),category:str=Form(...),base_name:str=Form(...)):
        user,scope=auth(request)
        if store not in stores or (scope!='ALL' and store!=scope) or category not in CATEGORIES:raise HTTPException(403,'Negozio o categoria non consentiti.')
        with connect() as conn:
            conn.cursor().execute(f'INSERT INTO pos_product_settings(store,product_code,category,base_name) VALUES({",".join([ph()]*4)}) ON CONFLICT(store,product_code) DO UPDATE SET category=excluded.category,base_name=excluded.base_name',
                (store,product_code,category,base_name.strip()))
        return RedirectResponse('/admin/cassa/report',303)

    @app.post('/admin/cassa/ricetta')
    def recipe(request:Request,store:str=Form(...),product_code:str=Form(...),inventory_product_id:int=Form(...),qty_per_sale:str=Form(...)):
        user,scope=auth(request)
        if store not in stores or (scope!='ALL' and store!=scope):raise HTTPException(403,'Negozio non consentito.')
        qty=number(qty_per_sale)
        # Negative doses are allowed for explicit removals such as -Cipolla, not for ordinary products.
        with connect() as conn:
            cur=conn.cursor()
            product=cur.execute(f'SELECT id FROM products WHERE id={ph()} AND store={ph()}',(inventory_product_id,store)).fetchone()
            if not product:raise HTTPException(422,'Prodotto inventario non valido.')
            if qty==0:
                cur.execute(f'DELETE FROM pos_recipes WHERE store={ph()} AND product_code={ph()} AND inventory_product_id={ph()}',(store,product_code,inventory_product_id))
            else:
                cur.execute(f'INSERT INTO pos_recipes(store,product_code,inventory_product_id,qty_per_sale) VALUES({",".join([ph()]*4)}) ON CONFLICT(store,product_code,inventory_product_id) DO UPDATE SET qty_per_sale=excluded.qty_per_sale',
                    (store,product_code,inventory_product_id,qty))
        return RedirectResponse('/admin/cassa/report',303)

    @app.get('/admin/cassa/report')
    def report(request:Request,start:str='',end:str='',month:str='',download:str=''):
        user,store=auth(request)
        if start or end:
            first=iso(start or today().replace(day=1).isoformat());last=iso(end or today().isoformat())
        else:
            try:anchor=date.fromisoformat((month or today().strftime('%Y-%m'))+'-01')
            except ValueError:raise HTTPException(422,'Mese non valido.')
            first=anchor.isoformat();last=((anchor.replace(day=28)+timedelta(days=4)).replace(day=1)-timedelta(days=1)).isoformat()
        if first>last:raise HTTPException(422,'Periodo non valido.')
        with connect() as conn:
            cur=conn.cursor();sql=f"SELECT * FROM pos_import_batches WHERE status='active' AND start_date<={ph()} AND end_date>={ph()}";params=[last,first]
            if store!='ALL':sql+=f' AND store={ph()}';params.append(store)
            batches=cur.execute(sql+' ORDER BY store,start_date,kind',tuple(params)).fetchall()
            setting_rows=cur.execute('SELECT * FROM pos_product_settings').fetchall()
            settings={(r['store'],r['product_code']):r for r in setting_rows}
            recipes=cur.execute('SELECT r.*,p.name,p.unit,p.location,p.area,p.qty FROM pos_recipes r JOIN products p ON p.id=r.inventory_product_id AND p.store=r.store').fetchall()
            stock_sql='SELECT id,store,name,location,unit,qty,area FROM products WHERE 1=1';stock_params=[]
            if store!='ALL':stock_sql+=f' AND store={ph()}';stock_params=[store]
            stock=cur.execute(stock_sql+' ORDER BY store,area,name,location',tuple(stock_params)).fetchall()
        daily={};products={};crossing=[];warnings=[]
        for b in batches:
            data=json.loads(b['parsed_json'])
            warnings.extend(data['warnings'])
            if b['kind']=='products' and (b['start_date']<first or b['end_date']>last):
                crossing.append(b);continue
            for row in data['rows']:
                if b['kind']=='products':
                    key=(b['store'],row['product_code']);setting=settings.get(key,{})
                    item=products.setdefault(key,dict(store=b['store'],product_code=row['product_code'],name=row['name'],
                        base_name=setting.get('base_name') or re.sub(r'\s*\(?Delivery\)?\s*','',row['name'],flags=re.I).strip(),
                        category=setting.get('category') or row['category'],quantity=Decimal(0),gross=Decimal(0),net=Decimal(0),before_discount=Decimal(0)))
                    for field in ['quantity','gross','net','before_discount']:item[field]+=Decimal(str(row[field]))
                elif first<=row['date']<=last:
                    key=(b['store'],row['date'])
                    day=daily.setdefault(key,dict(store=b['store'],date=row['date'],paid=None,methods=None,vat_gross=None,net=Decimal(0),vat=Decimal(0),method_detail={},vat_detail=[]))
                    amount=Decimal(str(row['gross']))
                    if b['kind']=='paid':day['paid']=amount
                    elif b['kind']=='methods':
                        day['methods']=(day['methods'] or Decimal(0))+amount
                        day['method_detail'][row['method']]=day['method_detail'].get(row['method'],Decimal(0))+amount
                    else:
                        day['vat_gross']=(day['vat_gross'] or Decimal(0))+amount
                        day['net']+=Decimal(str(row['net']));day['vat']+=Decimal(str(row['vat']));day['vat_detail'].append(row)
        for day in daily.values():
            values=[day[k] for k in ['paid','methods','vat_gross'] if day[k] is not None]
            day['matches']=bool(len(values)>1 and max(values)-min(values)<=Decimal('0.02'))
            day['incomplete']=len(values)<3
        daily=sorted(daily.values(),key=lambda d:(d['date'],d['store']))
        products=sorted(products.values(),key=lambda r:(r['store'],r['category'],r['name']))
        totals={k:sum((d[k] for d in daily if d[k] is not None),Decimal(0)) if any(d[k] is not None for d in daily) else None for k in ['paid','methods','vat_gross']}
        method_totals={}
        for day in daily:
            for method,value in day['method_detail'].items():method_totals[method]=method_totals.get(method,Decimal(0))+value
        gross=sum((r['gross'] for r in products),Decimal(0)) if products else None
        sandwiches=sum((r['quantity'] for r in products if r['category']=='Panini'),Decimal(0))
        # Compare products only when both sources cover exactly the requested days for each branch.
        reconciliation=[]
        for st in sorted({b['store'] for b in batches}):
            paid_days={d['date'] for d in daily if d['store']==st and d['paid'] is not None}
            product_days=set()
            for b in batches:
                if b['store']==st and b['kind']=='products' and b not in crossing:
                    d=date.fromisoformat(b['start_date']);finish=date.fromisoformat(b['end_date'])
                    while d<=finish:product_days.add(d.isoformat());d+=timedelta(days=1)
            comparable=bool(paid_days and paid_days==product_days)
            paid_total=sum((d['paid'] for d in daily if d['store']==st and d['paid'] is not None),Decimal(0))
            product_total=sum((r['gross'] for r in products if r['store']==st),Decimal(0))
            reconciliation.append(dict(store=st,comparable=comparable,paid=paid_total,products=product_total,difference=paid_total-product_total))
        recipe_map={}
        for r in recipes:recipe_map.setdefault((r['store'],r['product_code']),[]).append(r)
        consumption={};unmapped=[]
        for item in products:
            links=recipe_map.get((item['store'],item['product_code']),[])
            if not links:unmapped.append(item)
            for link in links:
                key=(link['store'],link['inventory_product_id'])
                c=consumption.setdefault(key,dict(store=link['store'],product_id=link['inventory_product_id'],name=link['name'],
                    unit=link['unit'],location=link['location'],current=link['qty'],expected_consumption=Decimal(0)))
                c['expected_consumption']+=item['quantity']*Decimal(str(link['qty_per_sale']))
        text=[f'REPORT CASSA — {stores.get(store,"Tutti i negozi")}',f'Dal {first} al {last}','']
        for key,label in [('paid','Pagato al giorno'),('methods','Metodi di pagamento'),('vat_gross','Lordo tavola IVA')]:
            text.append(label+': '+(money(totals[key])+' EUR' if totals[key] is not None else 'report assente'))
        for method,val in method_totals.items():text.append(f'  {method}: {money(val)} EUR')
        text+=['',f'Panini venduti classificati: {sandwiches}',f'Fatturato prodotti dopo sconti: {money(gross)} EUR','']
        for r in products:text.append(f'{stores.get(r["store"])} | {r["name"]} | {r["category"]} | quantità {r["quantity"]} | {money(r["gross"])} EUR')
        for r in reconciliation:
            text.append(f'{stores.get(r["store"])}: '+(f'differenza pagato − prodotti {money(r["difference"])} EUR' if r['comparable'] else 'periodi incompleti o differenti: confronto non disponibile'))
        text.append('I totali dei quattro report non vanno sommati tra loro. Delivery conserva la voce originale Pagamento non riscosso e non certifica un accredito ricevuto.')
        body='\n'.join(text)
        if download=='txt':return Response(body.encode('utf-8-sig'),media_type='text/plain; charset=utf-8',headers={'Content-Disposition':f'attachment; filename="cassa-{first}-{last}.txt"'})
        if download=='csv':
            out=io.StringIO(newline='');writer=csv.writer(out,delimiter=';');writer.writerow(['Sezione','Negozio','Dal','Al','Voce','Quantità','Lordo','Imponibile','IVA'])
            for day in daily:
                for key,label in [('paid','Pagato al giorno'),('methods','Metodi totale'),('vat_gross','Tavola IVA')]:
                    if day[key] is not None:writer.writerow([csvsafe(v) for v in [label,stores.get(day['store']),day['date'],day['date'],'','',money(day[key]),money(day['net']) if key=='vat_gross' else '',money(day['vat']) if key=='vat_gross' else '']])
                for method,val in day['method_detail'].items():writer.writerow([csvsafe(v) for v in ['Metodo',stores.get(day['store']),day['date'],day['date'],method,'',money(val),'','']])
            for r in products:writer.writerow([csvsafe(v) for v in ['Prodotti',stores.get(r['store']),first,last,r['name'],r['quantity'],money(r['gross']),money(r['net']),'']])
            return Response(out.getvalue().encode('utf-8-sig'),media_type='text/csv; charset=utf-8',headers={'Content-Disposition':f'attachment; filename="cassa-{first}-{last}.csv"'})
        return render('pos_report.html',**context(user,store,start=first,end=last,daily=daily,products=products,totals=totals,
            method_totals=method_totals,product_gross=gross,sandwiches=sandwiches,crossing=crossing,reconciliation=reconciliation,
            consumption=list(consumption.values()),unmapped=unmapped,stock=stock,recipes=recipes,report_text=body,warnings=warnings))
