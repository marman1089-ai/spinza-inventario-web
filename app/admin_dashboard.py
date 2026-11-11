"""Admin overview and a durable, local-only document catalogue cache."""
import base64
import io
import json
import logging
import threading
import zipfile
from datetime import date, datetime, timedelta, timezone
from pathlib import PurePosixPath
from urllib.parse import urlencode, quote
from xml.etree import ElementTree
from zoneinfo import ZoneInfo
from fastapi import Request, HTTPException
from fastapi.responses import JSONResponse, Response
from .db import connect, using_postgres
from .document_review import pending_documents

SOURCES={
    'archivio':('archive_files','ts','created_by','File archiviati'),
    'fatture':('invoices_docs','doc_date','uploaded_by','Fatture'),
    'chiusure':('closures','closure_date','uploaded_by','Foto chiusure'),
    'spese-secondarie':('secondary_expenses','expense_date','uploaded_by','Spese secondarie'),
}
_wake=threading.Event()
_stop=threading.Event()
_thread=None
_store_labels={}
_logger=logging.getLogger(__name__)
MAX_TEXT=200000

def ph(): return '%s' if using_postgres() else '?'

def ensure_index(cur):
    cur.execute('''CREATE TABLE IF NOT EXISTS admin_file_index (
        kind TEXT NOT NULL,file_id INTEGER NOT NULL,source_stamp TEXT NOT NULL,
        full_text TEXT NOT NULL,thumbnail TEXT NOT NULL,state TEXT NOT NULL,
        info TEXT NOT NULL,PRIMARY KEY(kind,file_id))''')

def stamp(row):
    return json.dumps([row.get('filename'),str(row.get('ts')),row.get('size_bytes')],ensure_ascii=False)

def source_query(kind,preview=True):
    table,day,author,label=SOURCES[kind]
    extra='f.folder_id' if kind=='archivio' else 'NULL AS folder_id'
    supplier='f.supplier' if kind=='fatture' else "'' AS supplier"
    cached="substr(i.full_text,1,8000) AS excerpt,i.thumbnail" if preview else "'' AS excerpt,'' AS thumbnail"
    return f'''SELECT '{kind}' AS kind,f.id,f.store,f.filename,f.content_type,
        CAST(f.{day} AS TEXT) AS doc_date,CAST(f.ts AS TEXT) AS ts,
        f.{author} AS author,length(f.data) AS size_bytes,{extra},{supplier},
        i.source_stamp,i.state,i.info,{cached},
        CASE WHEN r.doc_id IS NOT NULL AND r.reviewed_at IS NULL THEN 1 ELSE 0 END AS pending_review
        FROM {table} f LEFT JOIN admin_file_index i ON i.kind='{kind}' AND i.file_id=f.id
        LEFT JOIN admin_document_reviews r ON r.kind='{kind}' AND r.doc_id=f.id'''

def catalogue(cur,scope,query='',kind='',page=0,day=None,today=None):
    today=today or datetime.now(ZoneInfo('Europe/Rome')).date()
    first=day or today-timedelta(days=1)
    last=day or today
    kinds=[kind] if kind in SOURCES else list(SOURCES)
    searches=[];params=[];needle='%'+query.casefold().replace('!','!!').replace('%','!%').replace('_','!_')+'%'
    for key in kinds:
        clauses=[]
        if scope!='ALL': clauses.append(f'f.store={ph()}');params.append(scope)
        if key=='archivio':
            rome=ZoneInfo('Europe/Rome')
            start=datetime.combine(first,datetime.min.time(),rome).astimezone(timezone.utc)
            end=datetime.combine(last+timedelta(days=1),datetime.min.time(),rome).astimezone(timezone.utc)
            timestamp='f.ts' if using_postgres() else 'datetime(f.ts)'
            clauses.extend([f'{timestamp}>={ph()}',f'{timestamp}<{ph()}'])
            params.extend([start.strftime('%Y-%m-%d %H:%M:%S'),end.strftime('%Y-%m-%d %H:%M:%S')])
        else:
            field=SOURCES[key][1]
            clauses.extend([f'f.{field}>={ph()}',f'f.{field}<={ph()}'])
            params.extend([first.isoformat(),last.isoformat()])
        if query:
            clauses.append(f"(LOWER(COALESCE(f.filename,'')) LIKE {ph()} ESCAPE '!' OR LOWER(COALESCE(i.full_text,'')) LIKE {ph()} ESCAPE '!'" + (f" OR LOWER(f.supplier) LIKE {ph()} ESCAPE '!'" if key=='fatture' else '')+')')
            params.extend([needle,needle]+([needle] if key=='fatture' else []))
        searches.append(source_query(key)+(' WHERE '+' AND '.join(clauses) if clauses else ''))
    union=' UNION ALL '.join(searches)
    total=cur.execute('SELECT COUNT(*) AS n FROM ('+union+') catalog',tuple(params)).fetchone()['n']
    page=max(0,min(page,max(0,(total-1)//40)))
    rows=cur.execute('SELECT * FROM ('+union+') catalog ORDER BY ts DESC,kind,id DESC LIMIT 40 OFFSET '+str(page*40),tuple(params)).fetchall()
    folders={r['id']:r for r in cur.execute('SELECT id,store,name,parent_id FROM archive_folders').fetchall()}
    result=[]
    for raw in rows:
        row=dict(raw);row['label']=SOURCES[row['kind']][3]
        row['store_label']=_store_labels.get(row['store'],'Archivio generale' if row['store']=='ALL' else row['store'])
        parts=[];seen=set();folder=folders.get(row['folder_id'])
        while folder and folder['id'] not in seen and folder['store']==row['store']:
            seen.add(folder['id']);parts.append(folder['name']);folder=folders.get(folder['parent_id'])
        row['folder_path']=' / '.join(reversed(parts)) if parts else ('Radice' if row['kind']=='archivio' else row['label'])
        if row['source_stamp']!=stamp(row):
            row.update(state='pending',excerpt='',thumbnail='',info='Anteprima in preparazione')
        row['url']=f"/admin/indice-file/{row['kind']}/{row['id']}/originale"
        result.append(row)
    return dict(rows=result,total=total,page=page,pages=max(1,(total+39)//40))

def thumbnail_image(raw):
    from PIL import Image,ImageOps
    with Image.open(io.BytesIO(raw)) as img:
        img=ImageOps.exif_transpose(img).convert('RGB');img.thumbnail((640,640))
        out=io.BytesIO();img.save(out,format='JPEG',quality=65)
        return 'data:image/jpeg;base64,'+base64.b64encode(out.getvalue()).decode('ascii')

def extract(raw,filename,mime):
    """Never execute uploaded files or send them to an external service."""
    if not raw:return '', '', 'unavailable','Nessun originale disponibile'
    ext=PurePosixPath(filename.lower()).suffix
    text='';thumb='';info='';state='ready'
    if ext=='.pdf' or mime=='application/pdf':
        try:
            import pymupdf as fitz
            with fitz.open(stream=raw,filetype='pdf') as doc:
                if doc.needs_pass: return '', '', 'unavailable','PDF protetto da password'
                text='\n'.join(doc[i].get_text() for i in range(min(len(doc),200)))
                if len(doc):
                    page=doc[0];scale=min(1.0,640/max(page.rect.width,page.rect.height))
                    pix=page.get_pixmap(matrix=fitz.Matrix(scale,scale),alpha=False)
                    thumb='data:image/jpeg;base64,'+base64.b64encode(pix.tobytes('jpeg')).decode('ascii')
                info=f'{len(doc)} pagine · anteprima prima pagina'
                if len(doc)>200: info+=' · testo limitato alle prime 200 pagine';state='partial'
        except ImportError:
            from pypdf import PdfReader
            doc=PdfReader(io.BytesIO(raw))
            text='\n'.join(page.extract_text() or '' for page in doc.pages[:200])
            info=f'{len(doc.pages)} pagine · anteprima immagine non disponibile'
        if not text.strip():
            state='visual';info+=' · scansione: testo non disponibile, consulta l’immagine o l’originale'
    elif mime.startswith('image/') or ext in ('.jpg','.jpeg','.png','.webp','.tif','.tiff','.bmp'):
        thumb=thumbnail_image(raw);state='visual';info='Anteprima immagine · testo non estratto'
    elif mime.startswith('text/') or ext in ('.txt','.md','.csv','.log','.json','.xml','.html'):
        try:text=raw.decode('utf-8-sig')
        except UnicodeDecodeError:text=raw.decode('cp1252',errors='replace')
        info='Testo del file'
    elif ext in ('.docx','.xlsx','.pptx','.odt','.ods','.zip'):
        with zipfile.ZipFile(io.BytesIO(raw)) as archive:
            entries=archive.infolist()
            if ext=='.zip':
                text='\n'.join(item.filename for item in entries[:5000]);info=f'Elenco di {len(entries)} file contenuti';state='partial' if len(entries)>5000 else 'ready'
            else:
                fragments=[];budget=0
                for item in entries:
                    name=item.filename
                    wanted=(name=='word/document.xml' or name.startswith('word/header') or name.startswith('word/footer') or name.startswith('xl/sharedStrings') or name.startswith('xl/worksheets/sheet') or name.startswith('ppt/slides/slide') or name=='content.xml') and name.endswith('.xml')
                    if not wanted:continue
                    budget+=item.file_size
                    if budget>20*1024*1024:state='partial';break
                    node=ElementTree.fromstring(archive.read(item))
                    fragments.append(name+'\n'+' '.join(node.itertext()))
                text='\n'.join(fragments);info='Testo estratto dal documento · formule e impaginazione nell’originale'
    else:
        return '', '', 'unavailable','Formato non indicizzabile: nome, percorso e originale disponibili'
    if len(text)>MAX_TEXT:
        text=text[:MAX_TEXT];state='partial';info+=' · testo limitato a 200.000 caratteri'
    return text,thumb,state,info

def index_one(kind,file_id):
    if kind not in SOURCES: return False
    with connect() as conn:
        cur=conn.cursor();ensure_index(cur)
        row=cur.execute(source_query(kind)+f' WHERE f.id={ph()}',(file_id,)).fetchone()
        if not row:return False
        if row['source_stamp']==stamp(row):return False
        source=cur.execute(f'SELECT data FROM {SOURCES[kind][0]} WHERE id={ph()}',(file_id,)).fetchone()
        if not source:return False
        raw=bytes(source['data'] or b'')
    try:
        if len(raw)>30*1024*1024: result=('', '', 'unavailable','File oltre 30 MB: consulta l’originale')
        else:result=extract(raw,row['filename'] or '',row['content_type'] or '')
    except Exception:
        result=('', '', 'error','Anteprima non riuscita: il file originale resta disponibile')
    with connect() as conn:
        cur=conn.cursor()
        # Deleted or replaced during extraction must not publish a stale preview.
        current=cur.execute(source_query(kind)+f' WHERE f.id={ph()}',(file_id,)).fetchone()
        if not current or stamp(current)!=stamp(row):return False
        cur.execute(f'''INSERT INTO admin_file_index(kind,file_id,source_stamp,full_text,thumbnail,state,info)
            VALUES({','.join([ph()]*7)}) ON CONFLICT(kind,file_id) DO UPDATE SET
            source_stamp=excluded.source_stamp,full_text=excluded.full_text,thumbnail=excluded.thumbnail,
            state=excluded.state,info=excluded.info''',(kind,file_id,stamp(row),*result))
    return True

def refresh_index():
    with connect() as conn:
        cur=conn.cursor();ensure_index(cur)
        jobs=[]
        for kind in SOURCES:
            for row in cur.execute(source_query(kind,preview=False)).fetchall():
                if row['source_stamp']!=stamp(row):jobs.append((str(row.get('ts') or ''),kind,row['id']))
            cur.execute(f'DELETE FROM admin_file_index WHERE kind={ph()} AND file_id NOT IN (SELECT id FROM {SOURCES[kind][0]})',(kind,))
    for _,kind,file_id in sorted(jobs,reverse=True):
        if _stop.is_set():break
        index_one(kind,file_id)

def worker():
    while not _stop.is_set():
        _wake.wait(30);_wake.clear()
        if _stop.is_set():break
        try:refresh_index()
        except Exception:_logger.exception('Aggiornamento indice admin non riuscito')

def dashboard(cur,scope,stores,today):
    ensure_index(cur)
    days=[today-timedelta(days=1),today]
    selected=list(stores) if scope=='ALL' else [scope]
    board=[]
    where=f'closure_date>={ph()} AND closure_date<={ph()}'
    args=[days[0].isoformat(),days[-1].isoformat()]
    if scope!='ALL':where+=f' AND store={ph()}';args.append(scope)
    pending_ids={row['report_id'] for row in cur.execute('SELECT report_id FROM closure_report_reviews WHERE reviewed_at IS NULL').fetchall()}
    report_groups={}
    for raw in cur.execute(f'SELECT id,store,closure_date,total_cents,created_by,created_at FROM closure_reports WHERE {where} ORDER BY id',tuple(args)).fetchall():
        row=dict(raw);row['pending_review']=row['id'] in pending_ids;report_groups.setdefault((row['store'],str(row['closure_date'])),[]).append(row)
    documents_waiting=pending_documents(cur,scope)
    pending_files={(row['kind'],row['id']) for row in documents_waiting}
    document_groups={}
    for kind,(table,column,author,label) in SOURCES.items():
        if kind=='archivio':continue
        where=f'{column}>={ph()} AND {column}<={ph()}'
        args=[days[0].isoformat(),days[-1].isoformat()]
        if scope!='ALL':where+=f' AND store={ph()}';args.append(scope)
        for raw in cur.execute(f'SELECT id,store,CAST({column} AS TEXT) AS doc_date,filename,{author} AS author FROM {table} WHERE {where} ORDER BY id',tuple(args)).fetchall():
            row=dict(raw);row['pending_review']=(kind,row['id']) in pending_files;row['url']=f"/admin/indice-file/{kind}/{row['id']}/originale"
            document_groups.setdefault((row['store'],row['doc_date'],kind),[]).append(row)
    for store in selected:
        item=dict(store=store,label=stores[store],days=[])
        for day in days:
            key=day.isoformat()
            kinds=[dict(kind=kind,label=spec[3],files=document_groups.get((store,key,kind),[])) for kind,spec in SOURCES.items() if kind!='archivio']
            item['days'].append(dict(date=key,label='Ieri' if day!=today else 'Oggi',reports=report_groups.get((store,key),[]),kinds=kinds,report_url='/report-chiusura?'+urlencode(dict(store=store,day=key))))
        board.append(item)
    totals=[]
    for kind,(table,_,_,label) in SOURCES.items():
        clause=f' WHERE store={ph()}' if scope!='ALL' else '';args=(scope,) if scope!='ALL' else ()
        row=cur.execute(f'SELECT COUNT(*) AS n,COALESCE(SUM(length(data)),0) AS size FROM {table}'+clause,args).fetchone()
        totals.append(dict(kind=kind,label=label,count=row['n'],size=row['size']))
    scope_clause='' if scope=='ALL' else f' AND r.store={ph()}'
    scope_args=() if scope=='ALL' else (scope,)
    waiting=cur.execute('SELECT COUNT(*) AS n FROM closure_report_reviews v JOIN closure_reports r ON r.id=v.report_id WHERE v.reviewed_at IS NULL'+scope_clause,scope_args).fetchone()['n']
    from .control_journal import create_control_schema
    create_control_schema(cur)
    recent_access=[]
    access_scope='' if scope=='ALL' else f' AND store={ph()}'
    for row in cur.execute("SELECT username,store,happened FROM control_activity_events WHERE kind='access' AND method='POST' AND path IN ('/login','/admin-login') AND status>=300 AND status<400"+access_scope+" ORDER BY happened DESC,id DESC LIMIT 12",scope_args).fetchall():
        recent_access.append(dict(row,local_time=datetime.fromtimestamp(row['happened'],ZoneInfo('Europe/Rome')).strftime('%d/%m/%Y %H:%M')))
    _wake.set()
    from .cash_treasury import confirmed_cash
    cash=confirmed_cash(cur,today.isoformat(),scope)
    return dict(cash=cash,waiting=waiting+len(documents_waiting),recent_access=recent_access,board=board,totals=totals,file_count=sum(t['count'] for t in totals),catalogue=catalogue(cur,scope,today=today),scope=scope,now=datetime.now(ZoneInfo('Europe/Rome')).strftime('%d/%m/%Y %H:%M'))

def install(app,api):
    global _store_labels
    _store_labels=dict(api['STORES'])
    def admin(request):
        user=api['require_login'](request)
        if not user or not api['is_admin'](request): raise HTTPException(403,'Pannello riservato agli admin.')
        return api['_current_store_scope'](request,user)
    @app.on_event('startup')
    def start_index():
        global _thread
        with connect() as conn:ensure_index(conn.cursor())
        _stop.clear();_wake.set()
        if _thread is None or not _thread.is_alive():
            _thread=threading.Thread(target=worker,name='admin-file-index',daemon=True);_thread.start()
    @app.on_event('shutdown')
    def stop_index():
        _stop.set();_wake.set()
        if _thread:_thread.join(timeout=2)
    @app.get('/admin/indice-file')
    def search(request:Request,q:str='',kind:str='',page:int=0,day:str=''):
        scope=admin(request)
        try:
            selected_day=date.fromisoformat(day) if day else None
            if selected_day and not 1900<=selected_day.year<=9998:
                raise ValueError()
        except ValueError:
            raise HTTPException(422,'Data non valida.')
        with connect() as conn:
            result=catalogue(conn.cursor(),scope,q[:200].strip(),kind,page,selected_day,api['_rome_today']())
        _wake.set()
        return JSONResponse(result,headers={'Cache-Control':'no-store'})
    @app.get('/admin/indice-file/{kind}/{file_id}/testo')
    def full_text(request:Request,kind:str,file_id:int):
        scope=admin(request)
        if kind not in SOURCES:raise HTTPException(404,'File non trovato.')
        with connect() as conn:
            row=conn.cursor().execute(source_query(kind)+f' WHERE f.id={ph()}'+(f' AND f.store={ph()}' if scope!='ALL' else ''),(file_id,scope) if scope!='ALL' else (file_id,)).fetchone()
            if not row:raise HTTPException(404,'File non trovato.')
            cache=conn.cursor().execute(f'SELECT full_text FROM admin_file_index WHERE kind={ph()} AND file_id={ph()}',(kind,file_id)).fetchone()
        return JSONResponse(dict(text=cache['full_text'] if cache and row['source_stamp']==stamp(row) else '',info=row['info']),headers={'Cache-Control':'no-store'})
    @app.get('/admin/indice-file/{kind}/{file_id}/originale')
    def original(request:Request,kind:str,file_id:int):
        scope=admin(request)
        if kind not in SOURCES:raise HTTPException(404,'File non trovato.')
        with connect() as conn:
            row=conn.cursor().execute(f'SELECT store,filename,content_type,data FROM {SOURCES[kind][0]} WHERE id={ph()}'+(f' AND store={ph()}' if scope!='ALL' else ''),(file_id,scope) if scope!='ALL' else (file_id,)).fetchone()
        if not row:raise HTTPException(404,'File non trovato.')
        mime=(row['content_type'] or 'application/octet-stream').split(';')[0]
        inline=mime in ('application/pdf','image/jpeg','image/png','image/webp','text/plain')
        return Response(bytes(row['data'] or b''),media_type=mime,headers={'Cache-Control':'no-store','X-Content-Type-Options':'nosniff','Content-Disposition':('inline' if inline else 'attachment')+"; filename*=UTF-8''"+quote(row['filename'] or 'file')})
