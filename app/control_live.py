"""Persistent live control across workers. Browser presence is distinct from committed events."""
import logging
import math
import time
import uuid
from datetime import datetime
from zoneinfo import ZoneInfo
from fastapi import Request
from fastapi.responses import JSONResponse, PlainTextResponse
from .db import connect, using_postgres

ROME=ZoneInfo('Europe/Rome')
EXCLUDED=('/static/','/admin/fasce-orarie','/admin/controllo-live','/inventario/attivita','/controllo/presenza','/controllo/assistenza')


def create_live_schema(c):
    from .control_assistance import schema
    schema(c)
    c.execute('''CREATE TABLE IF NOT EXISTS control_live_presence (
        token TEXT NOT NULL,user_id BIGINT NOT NULL,username TEXT NOT NULL,store TEXT NOT NULL,
        area TEXT NOT NULL,page TEXT NOT NULL,last_seen DOUBLE PRECISION NOT NULL,
        last_action DOUBLE PRECISION NOT NULL,state TEXT NOT NULL,PRIMARY KEY(token,user_id))''')
    c.execute('CREATE INDEX IF NOT EXISTS ix_control_presence_recent ON control_live_presence(last_seen,store,user_id)')
    c.execute('''CREATE TABLE IF NOT EXISTS control_live_operations (
        token TEXT PRIMARY KEY,user_id BIGINT NOT NULL,username TEXT NOT NULL,store TEXT NOT NULL,
        area TEXT NOT NULL,description TEXT NOT NULL,path TEXT NOT NULL,started DOUBLE PRECISION NOT NULL)''')
    c.execute('CREATE INDEX IF NOT EXISTS ix_control_pending_recent ON control_live_operations(started,store,user_id)')


def begin_operation(request,user,started,helpers):
    if not user or request.method not in ('POST','PUT','PATCH','DELETE') or request.url.path.startswith(EXCLUDED):return
    try:
        from .inventory_activity import init_activity_db
        from .control_journal import describe_request
        init_activity_db()
        token=str(uuid.uuid4());ph='%s' if using_postgres() else '?'
        store=getattr(request.state,'control_target_store',None) or (request.session.get('active_store') if user.get('role')=='admin' else user.get('store'))
        if store not in helpers['STORES']:store=user.get('store') or 'spinza'
        area=getattr(request.state,'control_target_area',None) or request.session.get('selected_area') or ''
        description=describe_request(request.method,request.url.path,200)[1]
        context=getattr(request.state,'control_target_label','')
        if context:description+=' · '+context
        with connect() as conn:
            conn.cursor().execute(f'INSERT INTO control_live_operations(token,user_id,username,store,area,description,path,started) VALUES({",".join([ph]*8)})',(token,int(user['id']),user['username'],store,area,description,request.url.path[:400],started))
        request.state.control_operation_token=token
    except Exception:logging.getLogger(__name__).exception('Inizio operazione live non disponibile')


def finish_operation(request,response,previous_user,started):
    token=getattr(request.state,'control_operation_token',None)
    if not token and request.url.path!='/logout':return
    try:
        ph='%s' if using_postgres() else '?'
        with connect() as conn:
            c=conn.cursor()
            if token:c.execute(f'DELETE FROM control_live_operations WHERE token={ph}',(token,))
            if request.url.path=='/logout' and previous_user:
                c.execute(f"UPDATE control_live_presence SET state='offline' WHERE user_id={ph} AND last_seen<={ph}",(int(previous_user['id']),time.time()))
    except Exception:logging.getLogger(__name__).exception('Chiusura operazione live non disponibile')


def read_live_events(c,store,request_cursor,log_cursor,initial,now):
    from .control_journal import log_epoch,log_value_label
    ph='%s' if using_postgres() else '?'
    scope=f' AND store={ph}' if store!='ALL' else ''
    scope_args=(store,) if scope else ()
    results=[];cursors={};more=False
    for source,cursor in [('request',request_cursor),('log',log_cursor)]:
        if source=='request':
            fields="id AS source_id,happened,user_id,username,store,area,kind,description,method,path,status,'' AS category,'' AS item,0 AS delta"
            table='control_activity_events';timefield='happened'
        else:
            fields=f"id AS source_id,{log_epoch()} AS happened,0 AS user_id,username,store,COALESCE(area,'') AS area,'log' AS kind,action AS description,'' AS method,'' AS path,200 AS status,category,name AS item,delta"
            table='logs';timefield=log_epoch()
        if initial:
            maximum=c.execute('SELECT MAX(id) AS n FROM '+table+' WHERE 1=1'+scope,scope_args).fetchone()
            maximum_id=int(maximum['n'] or 0)
            sql=f'SELECT {fields} FROM {table} WHERE {timefield}>={ph} AND id<={ph}'+scope+' ORDER BY id DESC LIMIT 100'
            rows=c.execute(sql,(now-86400,maximum_id)+scope_args).fetchall();rows=list(reversed(rows))
            cursors[source]=maximum_id
        else:
            rows=c.execute(f'SELECT {fields} FROM {table} WHERE id>{ph}'+scope+' ORDER BY id LIMIT 101',(cursor,)+scope_args).fetchall()
            if len(rows)>100:more=True
            rows=rows[:100];cursors[source]=max([cursor]+[int(r['source_id']) for r in rows])
            # Replay a bounded ID tail: late PostgreSQL commits and reconnects can be deduplicated by the client.
            tail=c.execute(f'SELECT {fields} FROM {table} WHERE id>{ph} AND id<={ph}'+scope+' ORDER BY id DESC LIMIT 100',(max(0,cursor-1000),cursor)+scope_args).fetchall()
            rows=list(tail)+list(rows)
        seen=set()
        for raw in rows:
            r=dict(raw);key=source+':'+str(r['source_id'])
            if key in seen:continue
            seen.add(key);r['key']=key;r['source']=source;r['happened']=float(r['happened'])
            r['time']=datetime.fromtimestamp(r['happened'],ROME).strftime('%H:%M:%S %Z')
            r['day']=datetime.fromtimestamp(r['happened'],ROME).date().isoformat()
            r['metric_label']=log_value_label(r) if source=='log' else ''
            results.append(r)
    results.sort(key=lambda r:(r['happened'],r['source'],r['source_id']))
    return results,cursors,more


def live_people(c,store,now):
    ph='%s' if using_postgres() else '?';scope=f' AND store={ph}' if store!='ALL' else '';args=(store,) if scope else ()
    users=c.execute('SELECT id,username,store,role FROM users').fetchall()
    users_by_id={int(u['id']):u for u in users}
    people={(int(u['id']),u['store']):{'user_id':int(u['id']),'username':u['username'],'store':u['store'],'role':u['role']} for u in users if store=='ALL' or store==u['store']}
    presences=c.execute(f'SELECT * FROM control_live_presence WHERE last_seen>{ph}'+scope+' ORDER BY last_seen DESC',(now-300,)+args).fetchall()
    operations=c.execute(f'SELECT * FROM control_live_operations WHERE started>{ph}'+scope+' ORDER BY started DESC',(now-600,)+args).fetchall()
    recent=c.execute(f'SELECT * FROM control_activity_events WHERE happened>{ph}'+scope+' ORDER BY happened DESC,id DESC LIMIT 1000',(now-300,)+args).fetchall()
    for r in list(presences)+list(operations)+list(recent):
        key=(int(r['user_id']),r['store'])
        u=users_by_id.get(key[0],{})
        people.setdefault(key,{'user_id':key[0],'username':r['username'],'store':r['store'],'role':u.get('role','staff')})
    for key,person in people.items():
        tabs=[r for r in presences if int(r['user_id'])==key[0] and r['store']==key[1]]
        pending=[dict(r) for r in operations if int(r['user_id'])==key[0] and r['store']==key[1]]
        receipts=[r for r in recent if int(r['user_id'])==key[0] and r['store']==key[1]]
        fresh=[r for r in tabs if now-float(r['last_seen'])<=35]
        rank=lambda r:(r['state']=='active',r['state']=='idle',r['state']=='away',float(r['last_seen']))
        chosen=max(fresh,key=rank) if fresh else (tabs[0] if tabs else None)
        last=receipts[0] if receipts else None
        state='offline';page='';last_seen=0;area=''
        if chosen:
            last_seen=float(chosen['last_seen']);page=chosen['page'];area=chosen['area']
            if now-last_seen<=35:state=chosen['state']
        elif last and now-float(last['happened'])<=15 and last['kind']!='logout':
            state='recent';last_seen=float(last['happened']);page=last['path'];area=last['area']
        if state=='offline' and last and last['kind']!='logout' and float(last['happened'])>last_seen and now-float(last['happened'])<=15:
            state='recent';last_seen=float(last['happened']);page=last['path'];area=last['area']
        if last and last['kind']=='logout' and (not chosen or float(last['happened'])>=float(chosen['last_seen'])):state='offline'
        last_action=next((r for r in receipts if r['kind'] in ('action','error')),None)
        person.update(state=state,page=page,area=area,last_seen=last_seen,tabs=len([r for r in fresh if r['state'] not in ('offline','away')]),pending=pending,last_operation=dict(last_action) if last_action else None)
    return sorted(people.values(),key=lambda r:(r['state']!='active',not bool(r['pending']),r['state']=='offline',r['username'].lower(),r['store'])),users


def register_live_control(app,helpers):
    from .control_assistance import register_assistance
    register_assistance(app,helpers)
    @app.post('/controllo/presenza')
    async def presence(request:Request):
        user=helpers['require_login'](request)
        if not user:return PlainTextResponse('Accesso richiesto',status_code=401)
        if request.headers.get('x-control-presence')!='1':return PlainTextResponse('Richiesta non valida',status_code=403)
        form=await request.form()
        try:
            token=str(uuid.UUID(str(form.get('token',''))));age=float(form.get('idle_seconds','0'))
            if not math.isfinite(age) or age<0:raise ValueError()
        except (ValueError,TypeError):return PlainTextResponse('Segnale non valido',status_code=400)
        store=str(form.get('store',''));area=str(form.get('area',''));page=str(form.get('page',''));mode=str(form.get('mode','visible'))
        if store not in helpers['STORES'] and not (store=='ALL' and user['role']=='admin'):return PlainTextResponse('Negozio non valido',status_code=403)
        if user['role']!='admin' and store!=user['store']:return PlainTextResponse('Accesso negato',status_code=403)
        if area not in ('','bibite','prodotti') or mode not in ('visible','away','offline') or not page.startswith('/') or page.startswith('//') or len(page)>300 or any(ord(ch)<32 or ch in '?#' for ch in page):return PlainTextResponse('Contesto non valido',status_code=400)
        from .inventory_activity import init_activity_db
        init_activity_db();now=time.time();ph='%s' if using_postgres() else '?'
        state=('active' if age<=60 else 'idle') if mode=='visible' else mode
        with connect() as conn:
            c=conn.cursor()
            c.execute(f'''INSERT INTO control_live_presence(token,user_id,username,store,area,page,last_seen,last_action,state) VALUES({','.join([ph]*9)})
            ON CONFLICT(token,user_id) DO UPDATE SET username=excluded.username,store=excluded.store,area=excluded.area,page=excluded.page,last_seen=excluded.last_seen,last_action=excluded.last_action,state=excluded.state''',(token,int(user['id']),user['username'],store,area,page,now,now-min(age,86400),state))
            c.execute(f'DELETE FROM control_live_presence WHERE last_seen<{ph}',(now-172800,))
            c.execute(f'DELETE FROM control_live_operations WHERE started<{ph}',(now-86400,))
        return ResponseNoContent()

    @app.get('/admin/controllo-live')
    def live_page(request:Request):
        user=helpers['require_login'](request)
        if not user or user['role']!='admin':return PlainTextResponse('Solo amministratore',status_code=403)
        result=helpers['render']('control_live.html',user=user,stores=helpers['STORES'],brand=request.session.get('active_store') or user['store'],active_store=request.session.get('active_store'),request=request)
        result.headers['Cache-Control']='no-store';return result

    @app.get('/admin/controllo-live/dati')
    def live_data(request:Request,store:str='ALL',request_cursor:int=0,log_cursor:int=0,initial:int=0):
        user=helpers['require_login'](request)
        if not user or user['role']!='admin':return PlainTextResponse('Solo amministratore',status_code=403)
        if store not in helpers['STORES'] and store!='ALL':return PlainTextResponse('Negozio non valido',status_code=400)
        if not 0<=request_cursor<=2**63-1 or not 0<=log_cursor<=2**63-1 or initial not in (0,1):return PlainTextResponse('Cursori non validi',status_code=400)
        from .inventory_activity import init_activity_db
        init_activity_db();now=time.time()
        with connect() as conn:
            c=conn.cursor();people,users=live_people(c,store,now)
            events,cursors,more=read_live_events(c,store,request_cursor,log_cursor,bool(initial),now)
        by_id={int(u['id']):u['role'] for u in users}
        names={}
        for u in users:names.setdefault(u['username'],[]).append(u['role'])
        for e in events:
            roles=names.get(e['username'],[])
            e['role']=by_id.get(int(e['user_id']),roles[0] if len(roles)==1 else 'unknown')
        return JSONResponse({'server_time':now,'time':datetime.fromtimestamp(now,ROME).strftime('%H:%M:%S %Z'),'people':people,'events':events,'cursor':cursors,'has_more':more,'refresh_seconds':3,'stores':helpers['STORES'],'day':datetime.fromtimestamp(now,ROME).date().isoformat()},headers={'Cache-Control':'no-store'})


def ResponseNoContent():
    from fastapi.responses import Response
    return Response(status_code=204,headers={'Cache-Control':'no-store'})

