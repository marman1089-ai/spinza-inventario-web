"""Inventory activity: server-timed samples, persistent and scoped to authenticated users."""
from time import time as activity_now
import uuid
from threading import Lock
from . import db as database

_schema_keys = set()
_schema_lock = Lock()
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
from fastapi import Request
from fastapi.responses import PlainTextResponse, Response
from .db import connect, using_postgres

ROME = ZoneInfo('Europe/Rome')

def _create_activity_db():
    pk = 'BIGSERIAL PRIMARY KEY' if using_postgres() else 'INTEGER PRIMARY KEY AUTOINCREMENT'
    with connect() as conn:
        c = conn.cursor()
        c.execute('''CREATE TABLE IF NOT EXISTS inventory_activity_sessions (
            token TEXT NOT NULL, user_id BIGINT NOT NULL, store TEXT NOT NULL,
            last_ping DOUBLE PRECISION NOT NULL, PRIMARY KEY(token,user_id,store))''')
        c.execute(f'''CREATE TABLE IF NOT EXISTS inventory_activity_intervals (
            id {pk}, user_id BIGINT NOT NULL, username TEXT NOT NULL,
            store TEXT NOT NULL, area TEXT NOT NULL, started DOUBLE PRECISION NOT NULL,
            ended DOUBLE PRECISION NOT NULL)''')
        c.execute('CREATE INDEX IF NOT EXISTS ix_inventory_activity_day ON inventory_activity_intervals(store,ended,started)')

def init_activity_db():
    key = database._database_url() or str(database.SQLITE_PATH.resolve())
    with _schema_lock:
        if key not in _schema_keys:
            _create_activity_db()
            _schema_keys.add(key)


def day_bounds(day):
    # Local midnight boundaries give 23/25 elapsed hours on DST transition days.
    start = datetime.combine(day, datetime.min.time(), ROME)
    end = datetime.combine(day + timedelta(days=1), datetime.min.time(), ROME)
    return start.timestamp(), end.timestamp()

def merge_intervals(intervals):
    result = []
    for start, end in sorted(intervals):
        if result and start <= result[-1][1]:
            result[-1][1] = max(end, result[-1][1])
        else:
            result.append([start, end])
    return result

def register_inventory_activity(app, helpers):
    @app.on_event('startup')
    def migrate_activity():
        init_activity_db()

    @app.post('/inventario/attivita')
    async def activity(request: Request):
        user = helpers['require_login'](request)
        if not user:
            return PlainTextResponse('Accesso richiesto', status_code=401)
        # Custom header prevents cross-site form submissions; no client timestamps trusted.
        if request.headers.get('x-inventory-activity') != '1':
            return PlainTextResponse('Richiesta non valida', status_code=403)
        form = await request.form()
        try:
            token = str(uuid.UUID(str(form.get('token', ''))))
        except ValueError:
            return PlainTextResponse('Sessione non valida', status_code=400)
        store = str(form.get('store', ''))
        area = str(form.get('area', ''))
        if store not in helpers['STORES'] or area not in ('prodotti', 'bibite') or (user['role'] != 'admin' and store != user['store']):
            return PlainTextResponse('Accesso negato', status_code=403)
        init_activity_db()
        now = activity_now()
        ph = '%s' if using_postgres() else '?'
        uid = int(user['id'])
        with connect() as conn:
            c = conn.cursor()
            if using_postgres():
                c.execute('SELECT pg_advisory_xact_lock(hashtext(%s))', ('activity:'+token+':'+str(uid)+':'+store,))
            else:
                c.execute('BEGIN IMMEDIATE')
            old = c.execute(f'SELECT last_ping FROM inventory_activity_sessions WHERE token={ph} AND user_id={ph} AND store={ph}', (token,uid,store)).fetchone()
            if old and 0 < now-float(old['last_ping']) <= 30:
                c.execute(f'INSERT INTO inventory_activity_intervals(user_id,username,store,area,started,ended) VALUES({",".join([ph]*6)})', (uid,user['username'],store,area,float(old['last_ping']),now))
            c.execute(f'''INSERT INTO inventory_activity_sessions(token,user_id,store,last_ping) VALUES({ph},{ph},{ph},{ph})
                ON CONFLICT(token,user_id,store) DO UPDATE SET last_ping=excluded.last_ping''', (token,uid,store,now))
            c.execute(f'DELETE FROM inventory_activity_sessions WHERE last_ping < {ph}', (now-86400,))
        return Response(status_code=204)

    @app.get('/admin/fasce-orarie')
    def activity_report(request: Request, day: str = '', store: str = ''):
        user = helpers['require_login'](request)
        if not user or user['role'] != 'admin':
            return PlainTextResponse('Accesso riservato agli amministratori', status_code=403)
        try:
            selected = datetime.strptime(day, '%Y-%m-%d').date() if day else datetime.now(ROME).date()
        except ValueError:
            return PlainTextResponse('Data non valida', status_code=400)
        store = store or request.session.get('active_store') or 'ALL'
        if store != 'ALL' and store not in helpers['STORES']:
            return PlainTextResponse('Negozio non valido', status_code=400)
        init_activity_db()
        lo, hi = day_bounds(selected)
        ph = '%s' if using_postgres() else '?'
        scope = f' AND store={ph}' if store != 'ALL' else ''
        args = (store,) if scope else ()
        with connect() as conn:
            c = conn.cursor()
            users = c.execute('SELECT id,username,store FROM users WHERE 1=1'+scope+' ORDER BY username,store', args).fetchall()
            events = c.execute(f'SELECT * FROM inventory_activity_intervals WHERE ended>{ph} AND started<{ph}'+scope+' ORDER BY started', (lo,hi)+args).fetchall()
        people = {(int(u['id']),u['store']): {'name':u['username'],'store':u['store'],'intervals':[]} for u in users}
        for e in events:
            key = (int(e['user_id']),e['store'])
            row = people.setdefault(key, {'name':e['username'],'store':e['store'],'intervals':[]})
            row['intervals'].append((max(lo,float(e['started'])),min(hi,float(e['ended']))))
        for row in people.values():
            merged = merge_intervals(row.pop('intervals'))
            row['minutes'] = round(sum(b-a for a,b in merged)/60,1)
            row['bars'] = []
            for a,b in merged:
                # Coalesce samples, splitting at timezone offset changes.
                cursor = a
                chunks = []
                while cursor < b:
                    stop = min(b, (int(cursor)//60+1)*60)
                    offset = datetime.fromtimestamp(cursor,ROME).utcoffset()
                    if chunks and chunks[-1][2] == offset:
                        chunks[-1][1] = stop
                    else:
                        chunks.append([cursor,stop,offset])
                    cursor = stop
                for start,stop,offset in chunks:
                    left = datetime.fromtimestamp(start,ROME)
                    # Label the closing boundary in the same offset as the segment.
                    right = datetime.fromtimestamp(stop,ROME)
                    minute = left.hour*60+left.minute+left.second/60
                    row['bars'].append({'left':minute/1440*100,'width':(stop-start)/86400*100,
                        'label':left.strftime('%H:%M:%S %Z')+' – '+right.strftime('%H:%M:%S %Z')})
        return helpers['render']('inventory_activity.html',user=user,day=selected.isoformat(),store=store,stores=helpers['STORES'],people=list(people.values()),hours=range(25),dst=(hi-lo)!=86400)
