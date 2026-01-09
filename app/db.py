import os
from contextlib import contextmanager

# SQLite (solo fallback locale)
import sqlite3
from pathlib import Path

# Postgres (Supabase)
import psycopg
from psycopg.rows import dict_row


BASE_DIR = Path(__file__).resolve().parent
SQLITE_PATH = BASE_DIR / "spinza.db"


def _database_url() -> str | None:
    url = os.environ.get("DATABASE_URL")
    if not url:
        return None
    return url.strip()


def using_postgres() -> bool:
    url = _database_url()
    return bool(url and url.startswith(("postgres://", "postgresql://")))


@contextmanager
def connect():
    """
    - Se c'è DATABASE_URL => Postgres (Supabase)
    - Altrimenti => SQLite locale (solo dev)
    """
    url = _database_url()

    # --- POSTGRES ---
    if url:
        # Se Render ti dà "postgres://", psycopg preferisce "postgresql://"
        if url.startswith("postgres://"):
            url = "postgresql://" + url[len("postgres://") :]

        # IMPORTANTISSIMO per Supabase: SSL
        # (Supabase richiede SSL, quindi enforce)
        conn = psycopg.connect(url, row_factory=dict_row, sslmode="require")
        try:
            yield conn
            conn.commit()
        finally:
            conn.close()
        return

    # --- SQLITE fallback ---
    SQLITE_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(SQLITE_PATH, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def ensure_db_exists():
    # In Postgres non serve creare file
    if using_postgres():
        return
    if not SQLITE_PATH.exists():
        SQLITE_PATH.touch()


def init_db():
    """
    Crea tabelle sia su Postgres che su SQLite.
    Schema compatibile con il tuo main.py (pw_salt/pw_hash/legacy_sha256 ecc.)
    """
    ensure_db_exists()

    with connect() as db:
        cur = db.cursor()

        # USERS
        cur.execute("""
        CREATE TABLE IF NOT EXISTS users (
            id SERIAL PRIMARY KEY,
            store TEXT NOT NULL,
            username TEXT NOT NULL,
            role TEXT NOT NULL DEFAULT 'staff',
            pw_salt TEXT,
            pw_hash TEXT,
            legacy_sha256 TEXT,
            created_at TIMESTAMP DEFAULT now()
        )
        """)

        # unique staff per store
        cur.execute("""
        CREATE UNIQUE INDEX IF NOT EXISTS ux_users_store_username
        ON users(store, username)
        """)

        # optional: unique admin username globale
        # (se vuoi admin globale davvero)
        cur.execute("""
        CREATE UNIQUE INDEX IF NOT EXISTS ux_users_admin_username
        ON users(username)
        WHERE role = 'admin'
        """)

        # PRODUCTS
        cur.execute("""
        CREATE TABLE IF NOT EXISTS products (
            id SERIAL PRIMARY KEY,
            store TEXT NOT NULL,
            category TEXT NOT NULL,
            name TEXT NOT NULL,
            qty DOUBLE PRECISION NOT NULL DEFAULT 0,
            min_qty DOUBLE PRECISION NOT NULL DEFAULT 0,
            updated_at TIMESTAMP DEFAULT now()
        )
        """)

        cur.execute("""
        CREATE UNIQUE INDEX IF NOT EXISTS ux_products_store_cat_name
        ON products(store, category, name)
        """)

        # LOGS
        cur.execute("""
        CREATE TABLE IF NOT EXISTS logs (
            id SERIAL PRIMARY KEY,
            ts TIMESTAMP DEFAULT now(),
            store TEXT NOT NULL,
            username TEXT NOT NULL,
            action TEXT NOT NULL,
            category TEXT NOT NULL,
            name TEXT NOT NULL,
            delta DOUBLE PRECISION NOT NULL
        )
        """)
