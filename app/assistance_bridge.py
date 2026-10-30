"""Temporary HTTPS relay for explicitly consented app-page assistance."""
import json
import secrets
from fastapi.responses import PlainTextResponse


def schema(c):
    c.execute('''CREATE TABLE IF NOT EXISTS control_assist_bridge (
        session_id TEXT PRIMARY KEY, frame TEXT NOT NULL DEFAULT '',
        grant_token TEXT NOT NULL DEFAULT '',commands TEXT NOT NULL DEFAULT '[]',
        sequence INTEGER NOT NULL DEFAULT 0)''')


def read(c, rid, admin, ph):
    row = c.execute(f'SELECT * FROM control_assist_bridge WHERE session_id={ph}', (rid,)).fetchone()
    if not row:
        return None
    return dict(frame=json.loads(row['frame']) if admin and row['frame'] else None,
                grant=row['grant_token'], commands=[] if admin else json.loads(row['commands']))


def action(c, row, name, form, admin, ph):
    rid = row['id']
    bridge = c.execute(f'SELECT * FROM control_assist_bridge WHERE session_id={ph}', (rid,)).fetchone()
    if not bridge or row['state'] != 'active':
        return PlainTextResponse('Ponte non attivo', status_code=409)
    try:
        if name == 'bridge-frame':
            if admin:
                return PlainTextResponse('Solo il dipendente condivide la pagina', status_code=403)
            raw = str(form.get('frame', ''))
            if len(raw.encode('utf-8')) > 450000:
                raise ValueError()
            frame = json.loads(raw)
            if not isinstance(frame, dict) or not isinstance(frame.get('nodes'), list) or len(frame['nodes']) > 1400:
                raise ValueError()
            # Only JSON data, never HTML, scripts or URLs; client renders via textContent.
            if not isinstance(frame.get('id'), str) or len(frame['id']) > 100:
                raise ValueError()
            ack = int(form.get('ack', '0'))
            commands = [cmd for cmd in json.loads(bridge['commands']) if cmd['seq'] > ack]
            c.execute(f'UPDATE control_assist_bridge SET frame={ph},commands={ph} WHERE session_id={ph}',
                      (raw, json.dumps(commands), rid))
        elif name == 'bridge-command':
            if not admin or row['control_state'] != 'accepted':
                return PlainTextResponse('Serve il secondo consenso', status_code=403)
            raw = str(form.get('command', ''))
            if len(raw) > 6000:
                raise ValueError()
            cmd = json.loads(raw)
            if not isinstance(cmd, dict) or cmd.get('grant') != bridge['grant_token'] or not bridge['grant_token']:
                return PlainTextResponse('Consenso revocato o non valido', status_code=403)
            if cmd.get('type') not in ('click', 'text', 'key', 'scroll'):
                raise ValueError()
            commands = json.loads(bridge['commands'])
            if len(commands) >= 20:
                return PlainTextResponse('Attendi la consegna dei comandi precedenti', status_code=429)
            seq = bridge['sequence'] + 1
            commands.append(dict(seq=seq, command=cmd))
            c.execute(f'UPDATE control_assist_bridge SET commands={ph},sequence={ph} WHERE session_id={ph}',
                      (json.dumps(commands), seq, rid))
        else:
            return PlainTextResponse('Comando non valido', status_code=404)
    except (ValueError, TypeError, KeyError):
        return PlainTextResponse('Dati ponte non validi', status_code=400)
    return None


def grant(c, rid, ph, enabled):
    c.execute(f'UPDATE control_assist_bridge SET grant_token={ph},commands=\'[]\' WHERE session_id={ph}',
              (secrets.token_urlsafe(32) if enabled else '', rid))
