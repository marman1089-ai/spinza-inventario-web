import os
import sqlite3
from pathlib import Path
from contextlib import contextmanager

# =========================
# CONFIG (Render FREE friendly)
# =========================
# Su Render FREE non puoi usare /var/data (dischi persistenti non disponibili).
# Mettiamo il DB nella cartella "app/" (scrivibile a runtime).
BASE_DIR = Path(__file__).resolve().parent

# Puoi anche impostare una ENV "DB_PATH" su Render se vuoi cambiare posizione.
# Default: app/spinza.db
DB_PATH = Path(os.getenv("DB_PATH", str(BASE_DIR / "spinza.db")))

# =========================
# CONNECTION
# =========================
@contextmanager
def connect():
    # assicura che la cartella esista (di solito sì)
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
# INIT DB (schema compatibile con main.py)
# =========================
def init_db():
    ensure_db_exists()

    with connect() as db:
        cur = db.cursor()

        # ---- USERS ----
        # Compatibile con:
        # - make_password / verify_password
        # - colonne: pw_salt, pw_hash, legacy_sha256
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
