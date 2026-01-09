"""Database layer (SQLite for local dev, PostgreSQL for production).

This app was originally built on a local SQLite file (spinza.db). That works on a
PC, but on hosts like Render the container filesystem is **ephemeral**: files
written at runtime can disappear after a restart/redeploy.

Solution
- If `DATABASE_URL` (or `POSTGRES_URL` / `POSTGRESQL_URL`) is set, the app uses
  PostgreSQL and stores everything there (persistent).
- Otherwise it falls back to SQLite (`spinza.db`) for local development.

Compatibility
The rest of the codebase was written with SQLite-style SQL:
- `?` placeholders
- `datetime('now')`
- `INSERT OR IGNORE`
This module provides a small wrapper so that the same SQL works on PostgreSQL
without rewriting all routes.
"""

from __future__ import annotations

import os
import re
import sqlite3
from pathlib import Path
from typing import Any, Iterable, Optional, Sequence, Tuple, Union

try:
    import psycopg2
    from psycopg2.extras import RealDictCursor
except Exception:  # pragma: no cover
    psycopg2 = None
    RealDictCursor = None


# -------------------------
# Configuration
# -------------------------

DB_PATH = Path(os.environ.get("SPINZA_DB_PATH", "spinza.db"))
SEED_DB_PATH = Path(os.environ.get("SEED_DB_PATH", Path(__file__).parent / "seed" / "spinza_seed.db"))


def _get_database_url() -> Optional[str]:
    return (
        os.environ.get("DATABASE_URL")
        or os.environ.get("POSTGRES_URL")
        or os.environ.get("POSTGRESQL_URL")
        or os.environ.get("PGDATABASE_URL")
    )


def using_postgres() -> bool:
    url = _get_database_url()
    return bool(url and url.strip())


def _normalize_pg_url(url: str) -> str:
    """Make Render/Heroku style URLs work with psycopg2 and add sslmode if needed."""
    url = url.strip()

    # Some providers use postgres://, psycopg2 accepts it but we normalize anyway.
    if url.startswith("postgres://"):
        url = "postgresql://" + url[len("postgres://") :]

    # If sslmode not specified and it's not clearly local, add sslmode=require.
    # This is safe for most managed DBs (Render/Supabase/Neon/etc.).
    if "sslmode=" not in url and not re.search(r"(@localhost|@127\.0\.0\.1|@0\.0\.0\.0)", url):
        joiner = "&" if "?" in url else "?"
        url = f"{url}{joiner}sslmode=require"

    return url


# -------------------------
# SQL translation helpers
# -------------------------

_QMARK_RE = re.compile(r"\?")
_DATETIME_NOW_RE = re.compile(r"datetime\(\s*'now'\s*\)", re.IGNORECASE)
_INSERT_OR_IGNORE_RE = re.compile(r"^\s*INSERT\s+OR\s+IGNORE\s+INTO\s+", re.IGNORECASE)


def _translate_sql_for_postgres(sql: str) -> str:
    """Translate a subset of SQLite SQL into PostgreSQL compatible SQL."""
    s = sql

    # Placeholders: ? -> %s
    s = _QMARK_RE.sub("%s", s)

    # sqlite datetime('now') -> NOW()
    s = _DATETIME_NOW_RE.sub("NOW()", s)

    # INSERT OR IGNORE -> INSERT ... ON CONFLICT DO NOTHING
    # We append ON CONFLICT DO NOTHING only if it's not already present.
    if _INSERT_OR_IGNORE_RE.search(s):
        s = _INSERT_OR_IGNORE_RE.sub("INSERT INTO ", s)
        if "ON CONFLICT" not in s.upper():
            s = s.rstrip().rstrip(";") + " ON CONFLICT DO NOTHING"

    return s


# -------------------------
# Cursor / connection wrappers
# -------------------------

class DBCursor:
    def __init__(self, cur, backend: str):
        self._cur = cur
        self._backend = backend  # "sqlite" or "postgres"

    def execute(self, sql: str, params: Sequence[Any] | None = None):
        if params is None:
            params = ()
        if self._backend == "postgres":
            sql = _translate_sql_for_postgres(sql)
        return self._cur.execute(sql, tuple(params))

    def executemany(self, sql: str, seq_of_params: Iterable[Sequence[Any]]):
        if self._backend == "postgres":
            sql = _translate_sql_for_postgres(sql)
        return self._cur.executemany(sql, [tuple(p) for p in seq_of_params])

    def fetchone(self):
        return self._cur.fetchone()

    def fetchall(self):
        return self._cur.fetchall()

    def __getattr__(self, name: str):
        return getattr(self._cur, name)


class DBConn:
    def __init__(self, conn, backend: str):
        self._conn = conn
        self.backend = backend

    def cursor(self):
        if self.backend == "sqlite":
            return DBCursor(self._conn.cursor(), "sqlite")
        return DBCursor(self._conn.cursor(cursor_factory=RealDictCursor), "postgres")

    def commit(self):
        return self._conn.commit()

    def close(self):
        return self._conn.close()

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        try:
            if exc is None:
                self.commit()
        finally:
            self.close()


# -------------------------
# Public API
# -------------------------

def ensure_db_exists():
    """SQLite only: ensure the local DB file exists (optionally seeded)."""
    if using_postgres():
        return

    if DB_PATH.exists():
        return

    # If a seed DB exists (pre-populated with demo data), copy it.
    if SEED_DB_PATH.exists():
        DB_PATH.write_bytes(SEED_DB_PATH.read_bytes())
    else:
        # Touch file; tables will be created by init_db().
        DB_PATH.touch()


def connect() -> DBConn:
    """Return a DB connection wrapper."""
    if using_postgres():
        url = _normalize_pg_url(_get_database_url() or "")
        conn = psycopg2.connect(url)
        # For web apps, autocommit avoids idle transactions holding locks.
        conn.autocommit = False
        return DBConn(conn, "postgres")

    conn = sqlite3.connect(str(DB_PATH))
    conn.row_factory = sqlite3.Row
    # For better behavior with multiple threads (uvicorn), allow sharing.
    conn.execute("PRAGMA foreign_keys=ON")
    return DBConn(conn, "sqlite")


def init_db():
    """Create required tables (idempotent) on the selected backend."""
    with connect() as db:
        cur = db.cursor()

        if db.backend == "sqlite":
            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS users(
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    store TEXT NOT NULL,
                    username TEXT NOT NULL,
                    role TEXT NOT NULL DEFAULT 'staff',
                    pw_salt TEXT,
                    pw_hash TEXT,
                    legacy_sha256 TEXT,
                    created_at TEXT DEFAULT (datetime('now'))
                )
                """
            )

            # ---- SQLite migration: older DBs may miss the `store` column ----
            try:
                info = cur.execute("PRAGMA table_info(users)").fetchall()
                cols = {r["name"] for r in info}
                if "store" not in cols:
                    cur.execute("ALTER TABLE users ADD COLUMN store TEXT NOT NULL DEFAULT 'spinza'")
                if "role" not in cols:
                    cur.execute("ALTER TABLE users ADD COLUMN role TEXT NOT NULL DEFAULT 'staff'")
                if "pw_salt" not in cols:
                    cur.execute("ALTER TABLE users ADD COLUMN pw_salt TEXT")
                if "pw_hash" not in cols:
                    cur.execute("ALTER TABLE users ADD COLUMN pw_hash TEXT")
                if "legacy_sha256" not in cols:
                    cur.execute("ALTER TABLE users ADD COLUMN legacy_sha256 TEXT")
            except Exception:
                pass

            cur.execute("CREATE UNIQUE INDEX IF NOT EXISTS ux_users_store_username ON users(store, username)")

            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS products(
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    store TEXT NOT NULL,
                    category TEXT NOT NULL,
                    name TEXT NOT NULL,
                    qty REAL NOT NULL DEFAULT 0,
                    min_qty REAL NOT NULL DEFAULT 0,
                    created_at TEXT DEFAULT (datetime('now')),
                    updated_at TEXT DEFAULT (datetime('now'))
                )
                """
            )

            # SQLite migration for older `products` table
            try:
                info = cur.execute("PRAGMA table_info(products)").fetchall()
                cols = {r["name"] for r in info}
                if "store" not in cols:
                    cur.execute("ALTER TABLE products ADD COLUMN store TEXT NOT NULL DEFAULT 'spinza'")
                if "min_qty" not in cols:
                    cur.execute("ALTER TABLE products ADD COLUMN min_qty REAL NOT NULL DEFAULT 0")
                if "qty" not in cols:
                    cur.execute("ALTER TABLE products ADD COLUMN qty REAL NOT NULL DEFAULT 0")
                if "category" not in cols:
                    cur.execute("ALTER TABLE products ADD COLUMN category TEXT NOT NULL DEFAULT ''")
                if "name" not in cols:
                    cur.execute("ALTER TABLE products ADD COLUMN name TEXT NOT NULL DEFAULT ''")
            except Exception:
                pass

            cur.execute(
                "CREATE UNIQUE INDEX IF NOT EXISTS ux_products_store_cat_name ON products(store, category, name)"
            )

            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS logs(
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    ts TEXT NOT NULL,
                    store TEXT NOT NULL,
                    username TEXT NOT NULL,
                    action TEXT NOT NULL,
                    category TEXT NOT NULL,
                    name TEXT NOT NULL,
                    delta REAL NOT NULL
                )
                """
            )

            # SQLite migration for older `logs` table
            try:
                info = cur.execute("PRAGMA table_info(logs)").fetchall()
                cols = {r["name"] for r in info}
                if "store" not in cols:
                    cur.execute("ALTER TABLE logs ADD COLUMN store TEXT NOT NULL DEFAULT 'spinza'")
            except Exception:
                pass


        else:
            # PostgreSQL schema
            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS users(
                    id SERIAL PRIMARY KEY,
                    store TEXT NOT NULL,
                    username TEXT NOT NULL,
                    role TEXT NOT NULL DEFAULT 'staff',
                    pw_salt TEXT,
                    pw_hash TEXT,
                    legacy_sha256 TEXT,
                    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
                )
                """
            )
            cur.execute(
                "CREATE UNIQUE INDEX IF NOT EXISTS ux_users_store_username ON users(store, username)"
            )

            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS products(
                    id SERIAL PRIMARY KEY,
                    store TEXT NOT NULL,
                    category TEXT NOT NULL,
                    name TEXT NOT NULL,
                    qty DOUBLE PRECISION NOT NULL DEFAULT 0,
                    min_qty DOUBLE PRECISION NOT NULL DEFAULT 0,
                    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                    CONSTRAINT ux_products_store_cat_name UNIQUE(store, category, name)
                )
                """
            )

            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS logs(
                    id SERIAL PRIMARY KEY,
                    ts TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                    store TEXT NOT NULL,
                    username TEXT NOT NULL,
                    action TEXT NOT NULL,
                    category TEXT NOT NULL,
                    name TEXT NOT NULL,
                    delta DOUBLE PRECISION NOT NULL
                )
                """
            )


        # Ensure at least one admin exists (fresh install).
        try:
            cur2 = db.cursor()
            row = cur2.execute("SELECT 1 FROM users WHERE role='admin' LIMIT 1").fetchone()
            if not row:
                from .security import make_password  # local import to avoid cycles

                admin_user = os.environ.get("ADMIN_USERNAME", "marco06")
                admin_pass = os.environ.get("ADMIN_PASSWORD", "spinza2025")
                salt, h = make_password(admin_pass)
                # default admin belongs to store 'spinza' unless specified
                store = os.environ.get("DEFAULT_STORE", "spinza")
                cur2.execute(
                    "INSERT OR IGNORE INTO users(store, username, role, pw_salt, pw_hash, legacy_sha256) VALUES(?,?,?,?,?,NULL)",
                    (store, admin_user, "admin", salt, h),
                )
        except Exception:
            # Do not block startup if admin seeding fails.
            pass

        db.commit()
