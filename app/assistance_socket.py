"""Live page relay. One server worker, no frame writes to the database."""
import asyncio
import json
import time
from urllib.parse import urlsplit
from fastapi import WebSocket, WebSocketDisconnect
from starlette.concurrency import run_in_threadpool
from .db import connect, using_postgres

ROOMS = {}
MAX_FRAME = 1500000


def session_state(rid, uid):
    ph = '%s' if using_postgres() else '?'
    with connect() as conn:
        c = conn.cursor()
        row = c.execute(f'''SELECT s.*, b.grant_token FROM control_assist_sessions s
            JOIN control_assist_bridge b ON b.session_id=s.id WHERE s.id={ph}''', (rid,)).fetchone()
        user = c.execute(f'SELECT role FROM users WHERE id={ph}', (uid,)).fetchone()
    if not row or not user or uid not in (int(row['admin_id']), int(row['target_id'])):
        return None
    admin = uid == int(row['admin_id'])
    if admin and user['role'] != 'admin':
        return None
    if row['state'] != 'active' or min(row['admin_ping'], row['target_ping']) < time.time()-45 or row['created'] < time.time()-3600:
        return None
    return row, admin


class Peer:
    def __init__(self, ws):
        self.ws = ws
        self.latest = None
        self.controls = asyncio.Queue(maxsize=32)
        self.wake = asyncio.Event()

    async def write(self):
        while True:
            await self.wake.wait()
            self.wake.clear()
            while not self.controls.empty():
                await self.ws.send_json(self.controls.get_nowait())
            frame, self.latest = self.latest, None
            if frame:
                await self.ws.send_text(frame)

    def frame(self, raw):
        self.latest = raw  # Drop obsolete frames instead of building latency.
        self.wake.set()

    def message(self, data):
        if not self.controls.full():
            self.controls.put_nowait(data)
            self.wake.set()


def register_socket(app):
    @app.websocket('/controllo/assistenza/{rid}/live')
    async def live(ws: WebSocket, rid: str):
        origin = urlsplit(ws.headers.get('origin', ''))
        if origin.netloc != ws.headers.get('host') or origin.scheme not in ('http', 'https'):
            await ws.close(code=1008)
            return
        try:
            uid = int((ws.session.get('user') or {}).get('id', 0))
        except (ValueError, TypeError):
            uid = 0
        state = await run_in_threadpool(session_state, rid, uid)
        if not state:
            await ws.close(code=1008)
            return
        row, admin = state
        await ws.accept()
        role, other = ('admin', 'target') if admin else ('target', 'admin')
        room = ROOMS.setdefault(rid, {})
        if role in room:
            await ws.close(code=1008)
            return
        peer = Peer(ws)
        room[role] = peer
        writer = asyncio.create_task(peer.write())

        async def watch():
            while True:
                try:
                    fresh = await run_in_threadpool(session_state, rid, uid)
                except Exception:
                    await ws.close(code=1011)
                    return
                if not fresh:
                    await ws.close(code=1008)
                    return
                peer.message(dict(type='consent', state=fresh[0]['control_state'], grant=fresh[0]['grant_token'], updated=fresh[0]['updated']))
                await asyncio.sleep(1)

        watchdog = asyncio.create_task(watch())
        peer.message(dict(type='ready', paired=other in room))
        if other in room:
            room[other].message(dict(type='ready', paired=True))
        last_frame = 0
        try:
            while True:
                raw = await ws.receive_text()
                if len(raw.encode('utf-8')) > MAX_FRAME:
                    await ws.close(code=1009)
                    break
                try:
                    msg = json.loads(raw)
                    if not isinstance(msg, dict):
                        continue
                except (ValueError, TypeError):
                    continue
                if not admin and msg.get('type') == 'frame':
                    now = time.monotonic()
                    if now-last_frame < .08:  # Bound abuse; target cadence is 5 Hz.
                        continue
                    frame = msg.get('frame')
                    if not isinstance(frame, dict) or not isinstance(frame.get('html'), str):
                        continue
                    last_frame = now
                    if other in room:
                        room[other].frame(raw)
                elif admin and msg.get('type') == 'command':
                    fresh = await run_in_threadpool(session_state, rid, uid)
                    cmd = msg.get('command')
                    if not fresh or fresh[0]['control_state'] != 'accepted' or not isinstance(cmd, dict):
                        continue
                    token = fresh[0]['grant_token']
                    if not token or cmd.get('grant') != token or cmd.get('type') not in ('click','text','key','scroll') or len(raw)>6000:
                        continue
                    if other in room:
                        room[other].message(msg)
                elif msg.get('type') == 'ping':
                    peer.message(dict(type='pong', paired=other in room))
        except (WebSocketDisconnect, RuntimeError):
            pass
        finally:
            writer.cancel()
            watchdog.cancel()
            if room.get(role) is peer:
                room.pop(role, None)
            if other in room:
                room[other].message(dict(type='ready', paired=False))
            if not room:
                ROOMS.pop(rid, None)
            await asyncio.gather(writer, watchdog, return_exceptions=True)
