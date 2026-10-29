"""Versioned startup schema guard. No user data or schema is removed."""
import os
from .db import connect, using_postgres

SCHEMA_VERSION = '2026-10-29-log-area-v2'

def needs_schema_sync():
    if os.getenv('FORCE_SCHEMA_SYNC_ON_START') == '1' or not using_postgres():
        return True
    try:
        with connect() as db:
            row = db.cursor().execute(
                "SELECT version FROM app_schema_bootstrap WHERE id=1"
            ).fetchone()
        return not row or row['version'] != SCHEMA_VERSION
    except Exception:
        return True  # First deployment / older database: do the safe full initialization.

def mark_schema_synced():
    if not using_postgres():
        return
    with connect() as db:
        cur = db.cursor()
        cur.execute('CREATE TABLE IF NOT EXISTS app_schema_bootstrap (id INTEGER PRIMARY KEY, version TEXT NOT NULL)')
        cur.execute('INSERT INTO app_schema_bootstrap(id,version) VALUES(1,%s) ON CONFLICT(id) DO UPDATE SET version=EXCLUDED.version', (SCHEMA_VERSION,))
