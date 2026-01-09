import os
import sqlite3
from pathlib import Path
from contextlib import contextmanager

# =========================
# CONFIG
# =========================
# Su Render usa disco persistente /var/data (consigliato)
DEFAULT_DB_PATH = "/var/data/spinza.db"
DB_PATH = Path(os.getenv("DB_PATH", DEFAULT_DB_PATH))

# =========================
# CONNECTION
# =========================
@contextmanager
def connect():
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(DB_PATH), check_same_thread=False)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()

# =========================
# ENSURE DB EXISTS
# =========================
def ensure_db_exists():
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    if not DB_PATH.exists():
        DB_PATH.touch()

# =========================
# INIT DB
# =========================
def init_db():
    ensure_db_exists()

    with connect() as db:
        cur = db.cursor()

        # ---- USERS ----
        cur.execute("""
        CREATE TABLE IF NOT EXISTS users (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            store TEXT NOT NULL,
            username TEXT NOT NULL,
            role TEXT NOT NULL DEFAULT 'staff',

            pw_salt TEXT,
            pw_hash TEXT,
            legacy_sha256 TEXT,

            created_at TEXT DEFAULT (datetime('now'))
        )
        """)

        cur.execute("""
        CREATE UNIQUE INDEX IF NOT EXISTS ux_users_store_username
        ON users(store, username)
        """)

        # ---- PRODUCTS ----
        cur.execute("""
        CREATE TABLE IF NOT EXISTS products (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            store TEXT NOT NULL,
            category TEXT NOT NULL,
            name TEXT NOT NULL,
            qty REAL NOT NULL DEFAULT 0,
            min_qty REAL NOT NULL DEFAULT 0,
            updated_at TEXT DEFAULT (datetime('now'))
        )
        """)

        cur.execute("""
        CREATE UNIQUE INDEX IF NOT EXISTS ux_products_store_cat_name
        ON products(store, category, name)
        """)

        # ---- LOGS ----
        cur.execute("""
        CREATE TABLE IF NOT EXISTS logs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ts TEXT DEFAULT (datetime('now')),
            store TEXT NOT NULL,
            username TEXT NOT NULL,
            action TEXT NOT NULL,
            category TEXT NOT NULL,
            name TEXT NOT NULL,
            delta REAL NOT NULL
        )
        """)
