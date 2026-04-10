import os
from contextlib import contextmanager

# SQLite (solo fallback locale)
import sqlite3
from pathlib import Path

# Postgres (Supabase)
import psycopg
from psycopg.rows import dict_row


BASE_DIR = Path(__file__).resolve().parent


def _sqlite_dict_factory(cursor, row):
    return {col[0]: row[idx] for idx, col in enumerate(cursor.description)}
# Preferisci il DB storico nella root del progetto se presente, così non si perde
# tutto quando il file non è dentro /app ma un livello sopra.
_ROOT_SQLITE_PATH = BASE_DIR.parent / "spinza.db"
_APP_SQLITE_PATH = BASE_DIR / "spinza.db"
SQLITE_PATH = _ROOT_SQLITE_PATH if _ROOT_SQLITE_PATH.exists() and _ROOT_SQLITE_PATH.stat().st_size > 0 else _APP_SQLITE_PATH


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
    conn.row_factory = _sqlite_dict_factory
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
    ts_col = "TIMESTAMP" if pg else "TEXT"

    def _safe_exec(cur, sql: str):
        """Execute SQL safely during init.

        In Postgres a single SQL error aborts the current transaction; if we
        catch the exception without a rollback, every subsequent statement will
        fail with `InFailedSqlTransaction`. This helper ensures we always
        rollback on Postgres before continuing/raising.
        """
        try:
            cur.execute(sql)
        except Exception as e:
            if pg:
                try:
                    db.rollback()
                except Exception:
                    pass
            # Log the *real* failing statement to Render logs (very useful)
            print("[DB INIT] ERRORE SQL:", repr(e))
            print("[DB INIT] SQL FALLITA:\n", sql)
            raise

    with connect() as db:
        # IMPORTANT: in Postgres, DDL inside one long transaction is fragile.
        # With autocommit each statement is its own transaction, and a failure
        # won't poison the whole init phase.
        if pg:
            try:
                db.autocommit = True
            except Exception:
                pass

        cur = db.cursor()

        # USERS
        _safe_exec(cur, f"""
        CREATE TABLE IF NOT EXISTS users (
            id {id_col},
            store TEXT NOT NULL,
            username TEXT NOT NULL,
            role TEXT NOT NULL DEFAULT 'staff',
            pw_salt TEXT,
            pw_hash TEXT,
            legacy_sha256 TEXT,
            created_at {ts_default},
            last_seen {ts_default}
        )
        """)

        _safe_exec(cur, """
        CREATE UNIQUE INDEX IF NOT EXISTS ux_users_store_username
        ON users(store, username)
        """)

        # optional: unique admin username globale (partial index non sempre disponibile)
        try:
            _safe_exec(cur, """
            CREATE UNIQUE INDEX IF NOT EXISTS ux_users_admin_username
            ON users(username)
            WHERE role = 'admin'
            """)
        except Exception:
            # SQLite vecchie o DB particolari potrebbero non supportare indici parziali
            pass


        # ensure 'last_seen' column exists on older DBs
        try:
            _safe_exec(cur, "ALTER TABLE users ADD COLUMN last_seen TIMESTAMP" if pg else "ALTER TABLE users ADD COLUMN last_seen TEXT")
        except Exception:
            pass

        # PRODUCTS
        _safe_exec(cur, f"""
        CREATE TABLE IF NOT EXISTS products (
            id {id_col},
            store TEXT NOT NULL,
            category TEXT NOT NULL,
            name TEXT NOT NULL,
            area TEXT NOT NULL DEFAULT 'prodotti',
            unit TEXT NOT NULL DEFAULT '',
            location TEXT NOT NULL DEFAULT 'MAGAZZINO',
            qty {qty_col} NOT NULL DEFAULT 0,
            min_qty {qty_col} NOT NULL DEFAULT 0,
            missing_order_date TEXT,
            missing_delivery_date TEXT,
            missing_qty {qty_col} NOT NULL DEFAULT 0,
            updated_at {ts_default}
        )
        """)

        # NOTE: Supporto multi-posizione.
        # In passato l'indice unico era (store, category, name) e impediva di avere
        # lo stesso prodotto in più posizioni. Ora includiamo area + location.
        try:
            _safe_exec(cur, "DROP INDEX IF EXISTS ux_products_store_cat_name")
        except Exception:
            pass

        # ensure 'area' column exists on older DBs
        if using_postgres():
            # Postgres: evita errore (e transazione abortita) se la colonna esiste già
            _safe_exec(cur, "ALTER TABLE products ADD COLUMN IF NOT EXISTS area TEXT NOT NULL DEFAULT 'prodotti'")
            _safe_exec(cur, "ALTER TABLE products ADD COLUMN IF NOT EXISTS unit TEXT NOT NULL DEFAULT ''")
            _safe_exec(cur, "ALTER TABLE products ADD COLUMN IF NOT EXISTS location TEXT NOT NULL DEFAULT 'MAGAZZINO'")
            _safe_exec(cur, "ALTER TABLE products ADD COLUMN IF NOT EXISTS missing_order_date TEXT")
            _safe_exec(cur, "ALTER TABLE products ADD COLUMN IF NOT EXISTS missing_delivery_date TEXT")
            _safe_exec(cur, "ALTER TABLE products ADD COLUMN IF NOT EXISTS missing_qty DOUBLE PRECISION NOT NULL DEFAULT 0")
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
                cur.execute("ALTER TABLE products ADD COLUMN location TEXT NOT NULL DEFAULT 'MAGAZZINO'")
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

        # Crea l'indice multi-posizione solo DOPO essersi assicurati che le colonne esistano.
        try:
            _safe_exec(cur, """
            CREATE UNIQUE INDEX IF NOT EXISTS ux_products_store_cat_name_loc
            ON products(store, area, category, name, location)
            """)
        except Exception:
            pass

        # TRANSFERS (scambi tra negozi)
        _safe_exec(cur, f"""
        CREATE TABLE IF NOT EXISTS transfers (
            id {id_col},
            from_store TEXT NOT NULL,
            to_store TEXT NOT NULL,
            created_by TEXT NOT NULL,
            ts {ts_default}
        )
        """)

        _safe_exec(cur, f"""
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
        _safe_exec(cur, f"""
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
        _safe_exec(cur, f"""
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



        # CASH FLOW: incassi manuali giornalieri
        _safe_exec(cur, f"""
        CREATE TABLE IF NOT EXISTS cash_entries (
            id {id_col},
            store TEXT NOT NULL,
            flow_date {date_col} NOT NULL,
            payment_method TEXT NOT NULL DEFAULT '',
            amount {qty_col} NOT NULL DEFAULT 0,
            orders_count INTEGER NOT NULL DEFAULT 0,
            notes TEXT NOT NULL DEFAULT '',
            created_by TEXT NOT NULL,
            ts {ts_default}
        )
        """)

        # CASH FLOW: uscite manuali giornaliere
        _safe_exec(cur, f"""
        CREATE TABLE IF NOT EXISTS cash_expenses (
            id {id_col},
            store TEXT NOT NULL,
            flow_date {date_col} NOT NULL,
            category TEXT NOT NULL DEFAULT '',
            supplier TEXT NOT NULL DEFAULT '',
            payment_method TEXT NOT NULL DEFAULT '',
            amount {qty_col} NOT NULL DEFAULT 0,
            notes TEXT NOT NULL DEFAULT '',
            created_by TEXT NOT NULL,
            ts {ts_default}
        )
        """)

        # CASH FLOW: metodi di pagamento configurabili per gli incassi
        _safe_exec(cur, f"""
        CREATE TABLE IF NOT EXISTS cash_payment_methods (
            id {id_col},
            name TEXT NOT NULL UNIQUE,
            sort_order INTEGER NOT NULL DEFAULT 0,
            is_default INTEGER NOT NULL DEFAULT 0,
            created_by TEXT NOT NULL DEFAULT 'system',
            ts {ts_default}
        )
        """)

        default_payment_methods = [
            ('contanti', 10, 1),
            ('pos', 20, 1),
            ('deliveroo', 30, 1),
            ('glovo', 40, 1),
            ('just eat', 50, 1),
        ]
        for name, sort_order, is_default in default_payment_methods:
            try:
                _safe_exec(cur, f"INSERT INTO cash_payment_methods(name, sort_order, is_default, created_by) VALUES({ph},{ph},{ph},{ph})", (name, sort_order, is_default, 'system'))
            except Exception:
                pass

        # LOGISTICS: order queue + orders
        _safe_exec(cur, f"""
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

        _safe_exec(cur, f"""
        CREATE TABLE IF NOT EXISTS orders (
            id {id_col},
            store TEXT NOT NULL,
            supplier TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'in_corso',
            created_by TEXT NOT NULL,
            ts {ts_default},
            closed_at {ts_col}
        )
        """)

        _safe_exec(cur, f"""
        CREATE TABLE IF NOT EXISTS order_lines (
            id {id_col},
            order_id INTEGER NOT NULL,
            product_id INTEGER,
            category TEXT NOT NULL,
            name TEXT NOT NULL,
            qty {qty_col} NOT NULL,
            received_qty {qty_col} NOT NULL DEFAULT 0,
            is_missing INTEGER NOT NULL DEFAULT 0
        )
        """)
# INVOICES docs (archivio fatture)
        _safe_exec(cur, f"""
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
        _safe_exec(cur, f"""
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
        _safe_exec(cur, f"""
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

        _safe_exec(cur, """
        CREATE UNIQUE INDEX IF NOT EXISTS ux_invoice_imports_store_doc
        ON invoice_imports(store, invoice_doc_id)
        """)

        _safe_exec(cur, f"""
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
        _safe_exec(cur, f"""
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

        # NOVITÀ E AGGIORNAMENTI (bacheca)
        _safe_exec(cur, f"""
        CREATE TABLE IF NOT EXISTS updates (
            id {id_col},
            day {date_col} NOT NULL,
            message TEXT NOT NULL,
            created_by TEXT NOT NULL,
            ts {ts_default}
        )
        """)

        # --- Lightweight migrations (safe on both SQLite & Postgres) ---
        # Orders: mark transfers as "scambi" (treated like orders in progress)
        if pg:
            alters = [
                "ALTER TABLE orders ADD COLUMN IF NOT EXISTS kind TEXT DEFAULT 'ordine'",
                "ALTER TABLE orders ADD COLUMN IF NOT EXISTS from_store TEXT",
                "ALTER TABLE orders ADD COLUMN IF NOT EXISTS to_store TEXT",
                "ALTER TABLE orders ADD COLUMN IF NOT EXISTS transfer_id INTEGER",
                "ALTER TABLE orders ADD COLUMN IF NOT EXISTS closed_at TIMESTAMP",
                "ALTER TABLE order_lines ADD COLUMN IF NOT EXISTS area TEXT",
                "ALTER TABLE order_lines ADD COLUMN IF NOT EXISTS unit TEXT",
                "ALTER TABLE order_lines ADD COLUMN IF NOT EXISTS received_qty DOUBLE PRECISION NOT NULL DEFAULT 0",
                "ALTER TABLE order_lines ADD COLUMN IF NOT EXISTS is_missing INTEGER NOT NULL DEFAULT 0",
            ]
        else:
            alters = [
                "ALTER TABLE orders ADD COLUMN kind TEXT DEFAULT 'ordine'",
                "ALTER TABLE orders ADD COLUMN from_store TEXT",
                "ALTER TABLE orders ADD COLUMN to_store TEXT",
                "ALTER TABLE orders ADD COLUMN transfer_id INTEGER",
                "ALTER TABLE orders ADD COLUMN closed_at TEXT",
                "ALTER TABLE order_lines ADD COLUMN area TEXT",
                "ALTER TABLE order_lines ADD COLUMN unit TEXT",
                "ALTER TABLE order_lines ADD COLUMN received_qty REAL NOT NULL DEFAULT 0",
                "ALTER TABLE order_lines ADD COLUMN is_missing INTEGER NOT NULL DEFAULT 0",
            ]
        for stmt in alters:
            try:
                _safe_exec(cur, stmt)
            except Exception:
                pass

        # Cashflow compatibility migrations: evita errori sui database già esistenti
        if pg:
            cash_alters = [
                "ALTER TABLE cash_entries ADD COLUMN IF NOT EXISTS payment_method TEXT NOT NULL DEFAULT ''",
                "ALTER TABLE cash_entries ADD COLUMN IF NOT EXISTS amount DOUBLE PRECISION NOT NULL DEFAULT 0",
                "ALTER TABLE cash_entries ADD COLUMN IF NOT EXISTS orders_count INTEGER NOT NULL DEFAULT 0",
                "ALTER TABLE cash_entries ADD COLUMN IF NOT EXISTS notes TEXT NOT NULL DEFAULT ''",
                "ALTER TABLE cash_entries ADD COLUMN IF NOT EXISTS created_by TEXT NOT NULL DEFAULT 'system'",
                "ALTER TABLE cash_expenses ADD COLUMN IF NOT EXISTS category TEXT NOT NULL DEFAULT ''",
                "ALTER TABLE cash_expenses ADD COLUMN IF NOT EXISTS supplier TEXT NOT NULL DEFAULT ''",
                "ALTER TABLE cash_expenses ADD COLUMN IF NOT EXISTS payment_method TEXT NOT NULL DEFAULT ''",
                "ALTER TABLE cash_expenses ADD COLUMN IF NOT EXISTS amount DOUBLE PRECISION NOT NULL DEFAULT 0",
                "ALTER TABLE cash_expenses ADD COLUMN IF NOT EXISTS notes TEXT NOT NULL DEFAULT ''",
                "ALTER TABLE cash_expenses ADD COLUMN IF NOT EXISTS created_by TEXT NOT NULL DEFAULT 'system'",
            ]
        else:
            cash_alters = [
                "ALTER TABLE cash_entries ADD COLUMN payment_method TEXT NOT NULL DEFAULT ''",
                "ALTER TABLE cash_entries ADD COLUMN amount REAL NOT NULL DEFAULT 0",
                "ALTER TABLE cash_entries ADD COLUMN orders_count INTEGER NOT NULL DEFAULT 0",
                "ALTER TABLE cash_entries ADD COLUMN notes TEXT NOT NULL DEFAULT ''",
                "ALTER TABLE cash_entries ADD COLUMN created_by TEXT NOT NULL DEFAULT 'system'",
                "ALTER TABLE cash_expenses ADD COLUMN category TEXT NOT NULL DEFAULT ''",
                "ALTER TABLE cash_expenses ADD COLUMN supplier TEXT NOT NULL DEFAULT ''",
                "ALTER TABLE cash_expenses ADD COLUMN payment_method TEXT NOT NULL DEFAULT ''",
                "ALTER TABLE cash_expenses ADD COLUMN amount REAL NOT NULL DEFAULT 0",
                "ALTER TABLE cash_expenses ADD COLUMN notes TEXT NOT NULL DEFAULT ''",
                "ALTER TABLE cash_expenses ADD COLUMN created_by TEXT NOT NULL DEFAULT 'system'",
            ]
        for stmt in cash_alters:
            try:
                _safe_exec(cur, stmt)
            except Exception:
                pass
