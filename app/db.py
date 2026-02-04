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
    """Create required tables for both Postgres (Render/Supabase) and SQLite (local fallback)."""
    ensure_db_exists()

    pg = using_postgres()

    id_col = "SERIAL PRIMARY KEY" if pg else "INTEGER PRIMARY KEY AUTOINCREMENT"
    qty_col = "DOUBLE PRECISION" if pg else "REAL"
    ts_default = "TIMESTAMP DEFAULT now()" if pg else "TEXT DEFAULT (datetime('now'))"
    date_col = "DATE" if pg else "TEXT"
    blob_col = "BYTEA" if pg else "BLOB"

    with connect() as db:
        cur = db.cursor()

        # USERS
        cur.execute(f"""
        CREATE TABLE IF NOT EXISTS users (
            id {id_col},
            store TEXT NOT NULL,
            username TEXT NOT NULL,
            role TEXT NOT NULL DEFAULT 'staff',
            pw_salt TEXT,
            pw_hash TEXT,
            legacy_sha256 TEXT,
            created_at {ts_default}
        )
        """)

        cur.execute("""
        CREATE UNIQUE INDEX IF NOT EXISTS ux_users_store_username
        ON users(store, username)
        """)

        # optional: unique admin username globale (partial index non sempre disponibile)
        try:
            cur.execute("""
            CREATE UNIQUE INDEX IF NOT EXISTS ux_users_admin_username
            ON users(username)
            WHERE role = 'admin'
            """)
        except Exception:
            # SQLite vecchie o DB particolari potrebbero non supportare indici parziali
            pass

        # PRODUCTS
        cur.execute(f"""
        CREATE TABLE IF NOT EXISTS products (
            id {id_col},
            store TEXT NOT NULL,
            category TEXT NOT NULL,
            name TEXT NOT NULL,
            area TEXT NOT NULL DEFAULT 'prodotti',
            unit TEXT NOT NULL DEFAULT '',
            qty {qty_col} NOT NULL DEFAULT 0,
            min_qty {qty_col} NOT NULL DEFAULT 0,
            missing_order_date TEXT,
            missing_delivery_date TEXT,
            missing_qty {qty_col} NOT NULL DEFAULT 0,
            updated_at {ts_default}
        )
        """)

        cur.execute("""
        CREATE UNIQUE INDEX IF NOT EXISTS ux_products_store_cat_name
        ON products(store, category, name)
        """)

        # ensure 'area' column exists on older DBs
        if using_postgres():
            # Postgres: evita errore (e transazione abortita) se la colonna esiste già
            cur.execute("ALTER TABLE products ADD COLUMN IF NOT EXISTS area TEXT NOT NULL DEFAULT 'prodotti'")
            cur.execute("ALTER TABLE products ADD COLUMN IF NOT EXISTS unit TEXT NOT NULL DEFAULT ''")
            cur.execute("ALTER TABLE products ADD COLUMN IF NOT EXISTS missing_order_date TEXT")
            cur.execute("ALTER TABLE products ADD COLUMN IF NOT EXISTS missing_delivery_date TEXT")
            cur.execute("ALTER TABLE products ADD COLUMN IF NOT EXISTS missing_qty DOUBLE PRECISION NOT NULL DEFAULT 0")
        else:
            # SQLite: IF NOT EXISTS non è garantito su versioni vecchie, quindi try/except
            try:
                cur.execute("ALTER TABLE products ADD COLUMN area TEXT NOT NULL DEFAULT 'prodotti'")
            except Exception:
                pass
            try:
                cur.execute("ALTER TABLE products ADD COLUMN unit TEXT NOT NULL DEFAULT ''")
            except Exception:
                pass
            try:
                cur.execute("ALTER TABLE products ADD COLUMN missing_order_date TEXT")
            except Exception:
                pass
            try:
                cur.execute("ALTER TABLE products ADD COLUMN missing_delivery_date TEXT")
            except Exception:
                pass
            try:
                cur.execute("ALTER TABLE products ADD COLUMN missing_qty REAL NOT NULL DEFAULT 0")
            except Exception:
                pass

        # TRANSFERS (scambi tra negozi)
        cur.execute(f"""
        CREATE TABLE IF NOT EXISTS transfers (
            id {id_col},
            from_store TEXT NOT NULL,
            to_store TEXT NOT NULL,
            created_by TEXT NOT NULL,
            ts {ts_default}
        )
        """)

        cur.execute(f"""
        CREATE TABLE IF NOT EXISTS transfer_lines (
            id {id_col},
            transfer_id INTEGER NOT NULL,
            from_store TEXT NOT NULL,
            to_store TEXT NOT NULL,
            category TEXT NOT NULL,
            name TEXT NOT NULL,
            area TEXT NOT NULL,
            qty {qty_col} NOT NULL,
            unit TEXT NOT NULL DEFAULT ''
        )
        """)
        # CLOSURES (foto chiusure)
        cur.execute(f"""
        CREATE TABLE IF NOT EXISTS closures (
            id {id_col},
            store TEXT NOT NULL,
            closure_date {date_col} NOT NULL,
            uploaded_by TEXT NOT NULL,
            ts {ts_default},
            filename TEXT,
            content_type TEXT,
            data {blob_col}
        )
        """)

        
        # SECONDARY EXPENSES (spese secondarie - foto scontrini/spese)
        cur.execute(f"""
        CREATE TABLE IF NOT EXISTS secondary_expenses (
            id {id_col},
            store TEXT NOT NULL,
            expense_date {date_col} NOT NULL,
            uploaded_by TEXT NOT NULL,
            ts {ts_default},
            filename TEXT,
            content_type TEXT,
            data {blob_col}
        )
        """)

        # LOGISTICS: order queue + orders
        cur.execute(f"""
        CREATE TABLE IF NOT EXISTS order_queue (
            id {id_col},
            store TEXT NOT NULL,
            product_id INTEGER NOT NULL,
            category TEXT NOT NULL,
            name TEXT NOT NULL,
            qty_to_order {qty_col} NOT NULL DEFAULT 1,
            added_by TEXT NOT NULL,
            ts {ts_default}
        )
        """)

        cur.execute(f"""
        CREATE TABLE IF NOT EXISTS orders (
            id {id_col},
            store TEXT NOT NULL,
            supplier TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'in_corso',
            created_by TEXT NOT NULL,
            ts {ts_default}
        )
        """)

        cur.execute(f"""
        CREATE TABLE IF NOT EXISTS order_lines (
            id {id_col},
            order_id INTEGER NOT NULL,
            product_id INTEGER,
            category TEXT NOT NULL,
            name TEXT NOT NULL,
            qty {qty_col} NOT NULL
        )
        """)
# INVOICES docs (archivio fatture)
        cur.execute(f"""
        CREATE TABLE IF NOT EXISTS invoices_docs (
            id {id_col},
            store TEXT NOT NULL,
            supplier TEXT NOT NULL,
            doc_date {date_col} NOT NULL,
            uploaded_by TEXT NOT NULL,
            ts {ts_default},
            filename TEXT,
            content_type TEXT,
            data {blob_col}
        )
        """)

        # Invoice import drafts (foto caricata prima del ricontrollo)
        cur.execute(f"""
        CREATE TABLE IF NOT EXISTS invoice_import_drafts (
            id {id_col},
            store TEXT NOT NULL,
            supplier TEXT NOT NULL,
            doc_date {date_col} NOT NULL,
            uploaded_by TEXT NOT NULL,
            ts {ts_default},
            filename TEXT,
            content_type TEXT,
            data {blob_col}
        )
        """)

        # Invoice imports (confermati)
        cur.execute(f"""
        CREATE TABLE IF NOT EXISTS invoice_imports (
            id {id_col},
            store TEXT NOT NULL,
            invoice_doc_id INTEGER NOT NULL,
            supplier TEXT NOT NULL,
            doc_date {date_col} NOT NULL,
            area TEXT NOT NULL,
            created_by TEXT NOT NULL,
            ts {ts_default}
        )
        """)

        cur.execute("""
        CREATE UNIQUE INDEX IF NOT EXISTS ux_invoice_imports_store_doc
        ON invoice_imports(store, invoice_doc_id)
        """)

        cur.execute(f"""
        CREATE TABLE IF NOT EXISTS invoice_import_lines (
            id {id_col},
            import_id INTEGER NOT NULL,
            raw_name TEXT NOT NULL,
            category TEXT NOT NULL,
            qty {qty_col} NOT NULL,
            unit TEXT,
            product_id INTEGER,
            product_name TEXT
        )
        """)

        # LOGS
        cur.execute(f"""
        CREATE TABLE IF NOT EXISTS logs (
            id {id_col},
            ts {ts_default},
            store TEXT NOT NULL,
            username TEXT NOT NULL,
            action TEXT NOT NULL,
            category TEXT NOT NULL,
            name TEXT NOT NULL,
            delta {qty_col} NOT NULL
        )
        """)
