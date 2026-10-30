"""Consent-based screen sharing and independently approved control of the shared app tab."""
import json, os, time, uuid
from fastapi import Request
from fastapi.responses import JSONResponse, PlainTextResponse
from .db import connect,using_postgres


def schema(c):
    pk='BIGSERIAL PRIMARY KEY' if using_postgres() else 'INTEGER PRIMARY KEY AUTOINCREMENT'
    c.execute('''CREATE TABLE IF NOT EXISTS control_assist_sessions (
        id TEXT PRIMARY KEY,admin_id BIGINT NOT NULL,target_id BIGINT NOT NULL,
        admin_name TEXT NOT NULL,target_name TEXT NOT NULL,store TEXT NOT NULL,
        created DOUBLE PRECISION NOT NULL,updated DOUBLE PRECISION NOT NULL,
        admin_ping DOUBLE PRECISION NOT NULL,target_ping DOUBLE PRECISION NOT NULL,
        state TEXT NOT NULL,control_state TEXT NOT NULL DEFAULT 'none',
        control_capable INTEGER NOT NULL DEFAULT 0,offer TEXT NOT NULL DEFAULT '',answer TEXT NOT NULL DEFAULT '')''')
    c.execute('CREATE INDEX IF NOT EXISTS ix_control_assist_target ON control_assist_sessions(target_id,state,created)')
    c.execute(f'''CREATE TABLE IF NOT EXISTS control_assist_signals (
        id {pk},session_id TEXT NOT NULL,sender_id BIGINT NOT NULL,payload TEXT NOT NULL)''')
    c.execute('CREATE INDEX IF NOT EXISTS ix_control_assist_signals ON control_assist_signals(session_id,id)')
    from .assistance_bridge import schema as bridge_schema
    bridge_schema(c)


def ice_config():
    default=[{'urls':'stun:stun.l.google.com:19302'}]
    try:
        value=json.loads(os.getenv('CONTROL_ICE_SERVERS',json.dumps(default)))
        if not isinstance(value,list):return default
        result=[]
        for item in value[:10]:
            urls=item.get('urls',[]);urls=[urls] if isinstance(urls,str) else urls
            if not urls or not all(isinstance(u,str) and u.startswith(('stun:','stuns:','turn:','turns:')) for u in urls):continue
            result.append({k:item[k] for k in ('urls','username','credential') if k in item})
        return result
    except (ValueError,TypeError,AttributeError):return default


def expire(c,now):
    ph='%s' if using_postgres() else '?'
    c.execute(f"UPDATE control_assist_sessions SET state='expired',control_state='revoked',offer='',answer='' WHERE (state='pending' AND created<{ph}) OR (state IN ('accepted','active') AND (admin_ping<{ph} OR target_ping<{ph} OR created<{ph}))",(now-120,now-45,now-45,now-3600))
    c.execute("DELETE FROM control_assist_signals WHERE session_id IN (SELECT id FROM control_assist_sessions WHERE state NOT IN ('pending','accepted','active'))")
    c.execute("DELETE FROM control_assist_bridge WHERE session_id IN (SELECT id FROM control_assist_sessions WHERE state NOT IN ('pending','accepted','active'))")


def audit(c,user,row,description):
    ph='%s' if using_postgres() else '?'
    c.execute(f'''INSERT INTO control_activity_events(user_id,username,store,area,happened,kind,description,method,path,status) VALUES({','.join([ph]*10)})''',(int(user['id']),user['username'],row['store'],'',time.time(),'assist',description,'POST','/controllo/assistenza/'+row['id'],200))


def public(row):
    return {k:row[k] for k in ('id','admin_id','target_id','admin_name','target_name','state','control_state','control_capable','created','updated')}


def register_assistance(app,helpers):
    from .assistance_socket import register_socket
    register_socket(app)
    def authenticated(request):return helpers['require_login'](request)
    def init():
        from .inventory_activity import init_activity_db
        init_activity_db()
    def result(data):return JSONResponse(data,headers={'Cache-Control':'no-store'})

    @app.get('/controllo/assistenza/inviti')
    def invitations(request:Request):
        user=authenticated(request)
        if not user:return PlainTextResponse('Accesso richiesto',status_code=401)
        init();ph='%s' if using_postgres() else '?'
        with connect() as conn:
            c=conn.cursor();expire(c,time.time())
            rows=c.execute(f"SELECT * FROM control_assist_sessions WHERE target_id={ph} AND state='pending' ORDER BY created LIMIT 5",(int(user['id']),)).fetchall()
        return result({'requests':[public(r) for r in rows]})

    @app.post('/controllo/assistenza/richiedi')
    async def create(request:Request):
        user=authenticated(request)
        if not user or user.get('role')!='admin':return PlainTextResponse('Solo amministratore',status_code=403)
        if request.headers.get('x-control-screen')!='1':return PlainTextResponse('Richiesta non valida',status_code=403)
        form=await request.form()
        try:target=int(form.get('target_id','0'))
        except ValueError:return PlainTextResponse('Persona non valida',status_code=400)
        init();ph='%s' if using_postgres() else '?';now=time.time()
        with connect() as conn:
            c=conn.cursor()
            if not using_postgres():c.execute('BEGIN IMMEDIATE')
            else:c.execute('SELECT pg_advisory_xact_lock(%s)',(827491,))
            expire(c,now)
            employee=c.execute(f"SELECT id,username,store,role FROM users WHERE id={ph}",(target,)).fetchone()
            if not employee or employee['role']=='admin':return PlainTextResponse('Scegli un dipendente',status_code=400)
            existing=c.execute(f"SELECT id FROM control_assist_sessions WHERE (target_id={ph} OR admin_id={ph}) AND state IN ('pending','accepted','active')",(target,int(user['id']))).fetchone()
            if existing:return PlainTextResponse('Esiste già una richiesta o condivisione attiva. Chiudila prima di proseguire.',status_code=409)
            rid=str(uuid.uuid4())
            c.execute(f"INSERT INTO control_assist_sessions(id,admin_id,target_id,admin_name,target_name,store,created,updated,admin_ping,target_ping,state) VALUES({','.join([ph]*11)})",(rid,int(user['id']),target,user['username'],employee['username'],employee['store'],now,now,now,now,'pending'))
            row=c.execute(f'SELECT * FROM control_assist_sessions WHERE id={ph}',(rid,)).fetchone();audit(c,user,row,'Richiesta condivisione schermo a '+employee['username'])
        return result({'session':public(row),'iceServers':ice_config()})

    @app.get('/controllo/assistenza/{rid}')
    def channel(request:Request,rid:str,cursor:int=0):
        user=authenticated(request)
        if not user:return PlainTextResponse('Accesso richiesto',status_code=401)
        if cursor<0 or cursor>2**63-1:return PlainTextResponse('Cursore non valido',status_code=400)
        init();ph='%s' if using_postgres() else '?'
        with connect() as conn:
            c=conn.cursor();expire(c,time.time())
            row=c.execute(f'SELECT * FROM control_assist_sessions WHERE id={ph}',(rid,)).fetchone()
            if not row or int(user['id']) not in (int(row['admin_id']),int(row['target_id'])):return PlainTextResponse('Sessione non accessibile',status_code=403)
            signals=c.execute(f'SELECT id,payload FROM control_assist_signals WHERE session_id={ph} AND sender_id<>{ph} AND id>{ph} ORDER BY id LIMIT 200',(rid,int(user['id']),cursor)).fetchall()
            from .assistance_bridge import read as read_bridge
            bridge=read_bridge(c,rid,int(user['id'])==int(row['admin_id']),ph)
        return result({'bridge':bridge,'session':public(row),'offer':row['offer'],'answer':row['answer'],'signals':[{'id':r['id'],'candidate':json.loads(r['payload'])} for r in signals],'iceServers':ice_config()})

    @app.post('/controllo/assistenza/{rid}/{action}')
    async def command(request:Request,rid:str,action:str):
        user=authenticated(request)
        if not user:return PlainTextResponse('Accesso richiesto',status_code=401)
        if request.headers.get('x-control-screen')!='1':return PlainTextResponse('Richiesta non valida',status_code=403)
        init();ph='%s' if using_postgres() else '?';form=await request.form();now=time.time()
        with connect() as conn:
            c=conn.cursor()
            if not using_postgres():c.execute('BEGIN IMMEDIATE')
            expire(c,now)
            row=c.execute(f'SELECT * FROM control_assist_sessions WHERE id={ph}'+(' FOR UPDATE' if using_postgres() else ''),(rid,)).fetchone()
            if not row or int(user['id']) not in (int(row['admin_id']),int(row['target_id'])):return PlainTextResponse('Sessione non accessibile',status_code=403)
            admin=int(user['id'])==int(row['admin_id']);target=not admin
            active=row['state'] in ('pending','accepted','active')
            if not active:return PlainTextResponse('Sessione conclusa',status_code=409)
            description=None
            if action in ('bridge-frame','bridge-command'):
                from .assistance_bridge import action as bridge_action
                error=bridge_action(c,row,action,form,admin,ph)
                if error is not None:return error
            elif action=='accept':
                if not target or row['state']!='pending':return PlainTextResponse('Accettazione non consentita',status_code=403)
                bridge_mode=form.get('transport')=='bridge'
                capable=1 if bridge_mode or form.get('capable')=='1' else 0
                c.execute(f"UPDATE control_assist_sessions SET state='accepted',control_capable={ph},admin_ping={ph},target_ping={ph} WHERE id={ph}",(capable,now,now,rid));description='Condivisione schermo accettata da '+user['username']
                if bridge_mode:
                    description='Condivisione pagina gestionale accettata da '+user['username']
                    c.execute(f'INSERT INTO control_assist_bridge(session_id) VALUES({ph})',(rid,))
                    c.execute(f"UPDATE control_assist_sessions SET state='active' WHERE id={ph}",(rid,))
            elif action in ('decline','stop'):
                if action=='decline' and not target:return PlainTextResponse('Solo il destinatario può rifiutare',status_code=403)
                c.execute(f'DELETE FROM control_assist_bridge WHERE session_id={ph}',(rid,))
                state='declined' if action=='decline' else 'stopped'
                c.execute(f"UPDATE control_assist_sessions SET state={ph},control_state='revoked',offer='',answer='' WHERE id={ph}",(state,rid));c.execute(f'DELETE FROM control_assist_signals WHERE session_id={ph}',(rid,));description=('Condivisione rifiutata da ' if action=='decline' else 'Condivisione interrotta da ')+user['username']
            elif action=='offer':
                if not admin or row['offer']:return PlainTextResponse('Offerta non consentita',status_code=403)
                sdp=str(form.get('sdp',''))
                if not sdp.startswith('v=0') or len(sdp)>150000:return PlainTextResponse('SDP non valido',status_code=400)
                c.execute(f'UPDATE control_assist_sessions SET offer={ph} WHERE id={ph}',(sdp,rid))
            elif action=='answer':
                if not target or row['state']!='accepted' or not row['offer'] or row['answer']:return PlainTextResponse('Risposta non consentita',status_code=403)
                sdp=str(form.get('sdp',''))
                if not sdp.startswith('v=0') or len(sdp)>150000:return PlainTextResponse('SDP non valido',status_code=400)
                c.execute(f"UPDATE control_assist_sessions SET answer={ph},state='active' WHERE id={ph}",(sdp,rid))
            elif action=='candidate':
                if target and row['state']=='pending':return PlainTextResponse('Accetta prima la condivisione',status_code=403)
                try:
                    raw=str(form.get('candidate',''));value=json.loads(raw)
                    if len(raw)>10000 or not isinstance(value,dict) or not isinstance(value.get('candidate'),str):raise ValueError()
                    value={k:value[k] for k in ('candidate','sdpMid','sdpMLineIndex','usernameFragment') if k in value}
                except (ValueError,TypeError):return PlainTextResponse('Candidato non valido',status_code=400)
                count=c.execute(f'SELECT COUNT(*) AS n FROM control_assist_signals WHERE session_id={ph}',(rid,)).fetchone()['n']
                if count>=400:return PlainTextResponse('Troppi candidati',status_code=429)
                c.execute(f'INSERT INTO control_assist_signals(session_id,sender_id,payload) VALUES({ph},{ph},{ph})',(rid,int(user['id']),json.dumps(value)))
            elif action=='pulse':
                column='admin_ping' if admin else 'target_ping'
                c.execute(f'UPDATE control_assist_sessions SET {column}={ph} WHERE id={ph}',(now,rid))
            elif action=='request-control':
                if not admin or row['state']!='active' or not row['control_capable'] or row['control_state'] in ('requested','accepted'):return PlainTextResponse('Controllo non disponibile: occorre condividere la scheda del gestionale verificata e una nuova accettazione.',status_code=403)
                c.execute(f"UPDATE control_assist_sessions SET control_state='requested' WHERE id={ph}",(rid,));description='Richiesta separata controllo mouse e tastiera a '+row['target_name']
            elif action=='allow-control':
                if not target or row['state']!='active' or not row['control_capable'] or row['control_state']!='requested':return PlainTextResponse('Consenso separato richiesto',status_code=403)
                from .assistance_bridge import grant
                grant(c,rid,ph,True)
                c.execute(f"UPDATE control_assist_sessions SET control_state='accepted' WHERE id={ph}",(rid,));description='Controllo della scheda gestionale accettato da '+user['username']
            elif action=='revoke-control':
                if not target and row['control_state']!='accepted':return PlainTextResponse('Operazione non consentita',status_code=403)
                from .assistance_bridge import grant
                grant(c,rid,ph,False)
                c.execute(f"UPDATE control_assist_sessions SET control_state='revoked'"+(",control_capable=0" if target and form.get('disable')=='1' else '')+f" WHERE id={ph}",(rid,));description='Controllo remoto revocato da '+user['username']
            else:return PlainTextResponse('Comando non valido',status_code=404)
            c.execute(f'UPDATE control_assist_sessions SET updated={ph} WHERE id={ph}',(now,rid))
            if description:audit(c,user,row,description)
            updated=c.execute(f'SELECT * FROM control_assist_sessions WHERE id={ph}',(rid,)).fetchone()
            from .assistance_bridge import read as read_bridge
            bridge=read_bridge(c,rid,admin,ph)
        return result({'session':public(updated),'bridge':bridge})
