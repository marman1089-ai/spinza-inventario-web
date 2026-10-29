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
import csv
import io
from .db import connect, using_postgres

ROME = ZoneInfo('Europe/Rome')

def _create_activity_db():
    pk = 'BIGSERIAL PRIMARY KEY' if using_postgres() else 'INTEGER PRIMARY KEY AUTOINCREMENT'
    with connect() as conn:
        c = conn.cursor()
        from .control_journal import create_control_schema
        create_control_schema(c)
        c.execute('''CREATE TABLE IF NOT EXISTS inventory_activity_sessions (
            token TEXT NOT NULL, user_id BIGINT NOT NULL, store TEXT NOT NULL,
            last_ping DOUBLE PRECISION NOT NULL, PRIMARY KEY(token,user_id,store))''')
        c.execute(f'''CREATE TABLE IF NOT EXISTS inventory_activity_intervals (
            id {pk}, user_id BIGINT NOT NULL, username TEXT NOT NULL,
            store TEXT NOT NULL, area TEXT NOT NULL, started DOUBLE PRECISION NOT NULL,
            ended DOUBLE PRECISION NOT NULL)''')
        c.execute('CREATE INDEX IF NOT EXISTS ix_inventory_activity_day ON inventory_activity_intervals(store,ended,started)')

def init_activity_db():
    key = (database._database_url() or str(database.SQLITE_PATH.resolve())) + ':control-v2'
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
    from .control_journal import register_control_detail
    register_control_detail(app, helpers)
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
        if store not in helpers['STORES'] or area not in ('prodotti', 'bibite', '') or (user['role'] != 'admin' and store != user['store']):
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
            if form.get('state') == 'paused':
                if old and 0 < now-float(old['last_ping']) <= 30:
                    c.execute(f'INSERT INTO inventory_activity_intervals(user_id,username,store,area,started,ended) VALUES({",".join([ph]*6)})', (uid,user['username'],store,area,float(old['last_ping']),now))
                c.execute(f'DELETE FROM inventory_activity_sessions WHERE token={ph} AND user_id={ph} AND store={ph}',(token,uid,store))
                return Response(status_code=204)
            if not old or now-float(old['last_ping']) > 30:
                c.execute(f'INSERT INTO inventory_activity_intervals(user_id,username,store,area,started,ended) VALUES({",".join([ph]*6)})',(uid,user['username'],store,area,now,now))
            if old and 0 < now-float(old['last_ping']) <= 30:
                c.execute(f'INSERT INTO inventory_activity_intervals(user_id,username,store,area,started,ended) VALUES({",".join([ph]*6)})', (uid,user['username'],store,area,float(old['last_ping']),now))
            c.execute(f'''INSERT INTO inventory_activity_sessions(token,user_id,store,last_ping) VALUES({ph},{ph},{ph},{ph})
                ON CONFLICT(token,user_id,store) DO UPDATE SET last_ping=excluded.last_ping''', (token,uid,store,now))
            c.execute(f'DELETE FROM inventory_activity_sessions WHERE last_ping < {ph}', (now-86400,))
        return Response(status_code=204)

    @app.get('/admin/fasce-orarie')
    def activity_report(request: Request, day: str = '', store: str = '', download: str = ''):
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
        from .inventory_analytics import build_reports, duration
        lo, hi = day_bounds(selected)
        monday = selected-timedelta(days=selected.weekday())
        history_lo = day_bounds(monday-timedelta(days=28))[0]
        now = activity_now()
        today = datetime.fromtimestamp(now,ROME).date()
        ph = '%s' if using_postgres() else '?'
        scope = f' AND store={ph}' if store != 'ALL' else ''
        args = (store,) if scope else ()
        with connect() as conn:
            c = conn.cursor()
            users = c.execute('SELECT id,username,store FROM users WHERE 1=1'+scope+' ORDER BY username,store', args).fetchall()
            events = c.execute(f'SELECT * FROM inventory_activity_intervals WHERE ended>={ph} AND started<{ph}'+scope+' ORDER BY started', (history_lo,hi)+args).fetchall()
            recent = c.execute(f'SELECT user_id,store,MAX(last_ping) AS last_ping FROM inventory_activity_sessions WHERE last_ping>={ph}'+scope+' GROUP BY user_id,store',(now-30,)+args).fetchall()
        people = {(int(u['id']),u['store']): {'name':u['username'],'store':u['store'],'intervals':[]} for u in users}
        for e in events:
            key = (int(e['user_id']),e['store'])
            row = people.setdefault(key, {'name':e['username'],'store':e['store'],'intervals':[]})
            row['intervals'].append((float(e['started']),float(e['ended'])))
        from .control_windows import add_control_windows
        from .control_journal import journal_query
        with connect() as conn:
            c = conn.cursor()
            journal = journal_query(c, lo=lo, hi=hi, store=store)
            coverage_row = c.execute('SELECT MIN(happened) AS first FROM control_activity_events').fetchone()
        active = {(int(r['user_id']),r['store']):float(r['last_ping']) for r in recent} if selected == today else {}
        for key,row in people.items():
            all_intervals = row.pop('intervals')
            row.update(build_reports(all_intervals,selected))
            merged = merge_intervals([(max(lo,a),min(hi,b)) for a,b in all_intervals if b>=lo and a<hi])
            row['measured_windows'] = merged
            row['_presence_last'] = active.get(key,0)
            row['active'] = key in active
            row['first'] = datetime.fromtimestamp(merged[0][0],ROME).strftime('%H:%M') if merged else ('Adesso' if row['active'] else '—')
            row['current_duration'] = duration(merged[-1][1]-merged[-1][0]) if row['active'] and merged else '0 min'
            row['current_start'] = datetime.fromtimestamp(merged[-1][0],ROME).strftime('%H:%M') if row['active'] and merged else '—'
            row['last'] = datetime.fromtimestamp(merged[-1][1],ROME).strftime('%H:%M') if merged else '—'
            row['sessions'] = [{'start':datetime.fromtimestamp(a,ROME).strftime('%H:%M:%S %Z'),'end':datetime.fromtimestamp(b,ROME).strftime('%H:%M:%S %Z'),'duration':duration(b-a)} for a,b in merged]
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
        add_control_windows(people, journal, lo, hi, selected, now)
        rows=sorted(people.values(),key=lambda row:(not row['active'],not bool(row['sessions']),-row['daily']['seconds'],row['name'].lower()))
        if download == 'csv':
            output=io.StringIO();writer=csv.writer(output,delimiter=';')
            writer.writerow(['Tipo','Dipendente','Negozio','Dal','Al','Minuti utilizzo','Confronto','Minuti riferimento','Variazione %'])
            def safe(value):
                value=str(value)
                return "'"+value if value.lstrip().startswith(('=','+','-','@')) else value
            for row in rows:
                for kind,items,label in [('Giorno',row['days'],'Media 7 giorni precedenti, inclusi giorni a zero'),('Settimana',row['weeks'],'Stessi giorni settimana precedente')]:
                    for item in items:
                        writer.writerow([kind,safe(row['name']),safe(helpers['STORES'].get(row['store'],row['store'])),item['from'],item['to'],round(item['seconds']/60,2),label,round(item['base']/60,2),item['delta'] if item['delta'] is not None else 'N/D: riferimento zero'])
            return Response('\ufeff'+output.getvalue(),media_type='text/csv; charset=utf-8',headers={'Content-Disposition':f'attachment; filename="utilizzo_inventario_{selected.isoformat()}.csv"'})
        return helpers['render']('inventory_activity.html',user=user,day=selected.isoformat(),store=store,stores=helpers['STORES'],people=rows,hours=range(25),dst=(hi-lo)!=86400,live=selected==today,
            journal_coverage=datetime.fromtimestamp(coverage_row['first'],ROME).strftime('%d/%m/%Y %H:%M') if coverage_row and coverage_row['first'] else '',access_count=sum(r['access_count'] for r in rows),action_count=sum(r['action_count'] for r in rows),error_count=sum(r['error_count'] for r in rows),active_count=sum(r['active'] for r in rows),total_duration=duration(sum(r['daily']['seconds'] for r in rows)),used_count=sum(bool(r['sessions']) for r in rows),updated=datetime.fromtimestamp(now,ROME).strftime('%H:%M:%S'),request=request)
