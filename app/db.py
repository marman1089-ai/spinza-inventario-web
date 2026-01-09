"""
Database layer - PostgreSQL (Render) / SQLite fallback

- In produzione (Render): usa PostgreSQL tramite DATABASE_URL
- In locale: fallback automatico su SQLite
"""

from __future__ import annotations

import os
import sqlite3
from pathlib import Path
from contextlib import contextmanager

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker, declarative_base

# psycopg v3 (quello che hai in requirements: psycopg[binary])
try:
    import psycopg  # type: ignore
except Exception:
    psycopg = None  # se non disponibile, gestiamo sotto

# --- SQLAlchemy base ---
Base = declarative_base()

# --- Paths / env ---
# Se su Render usi Persistent Disk, di solito è /var/data
DEFAULT_SQLITE_PATH = "/var/data/spinza.db"
SQLITE_PATH = os.getenv("SPINZA_DB_PATH", DEFAULT_SQLITE_PATH)

DATABASE_URL = os.getenv("DATABASE_URL")  # Render PostgreSQL


def _normalize_db_url(url: str) -> str:
    """
    Render a volte fornisce:
      - postgres://...
      - postgresql://...

    Per SQLAlchemy + psycopg v3, vogliamo:
      - postgresql+psycopg://...
    """
    url = url.strip()

    if url.startswith("postgres://"):
        url = url.replace("postgres://", "postgresql://", 1)

    if url.startswith("postgresql://") and not url.startswith("postgresql+psycopg://"):
        url = url.replace("postgresql://", "postgresql+psycopg://", 1)

    return url


def _sqlite_url() -> str:
    # SQLAlchemy sqlite path
    return f"sqlite:///{SQLITE_PATH}"


def get_engine():
    """
    Crea engine SQLAlchemy:
    - PostgreSQL se DATABASE_URL esiste
    - altrimenti SQLite
    """
    global DATABASE_URL

    if DATABASE_URL:
        sa_url = _normalize_db_url(DATABASE_URL)
        return create_engine(
            sa_url,
            pool_pre_ping=True,
            future=True,
        )

    # SQLite
    return create_engine(
        _sqlite_url(),
        connect_args={"check_same_thread": False},
        future=True,
    )


engine = get_engine()

SessionLocal = sessionmaker(
    bind=engine,
    autocommit=False,
    autoflush=False,
    future=True,
)


def ensure_db_exists():
    """
    Se siamo in SQLite, crea la cartella e il file se mancano.
    Su PostgreSQL non serve.
    """
    if DATABASE_URL:
        return

    p = Path(SQLITE_PATH)
    p.parent.mkdir(parents=True, exist_ok=True)
    if not p.exists():
        # crea file sqlite vuoto
        conn = sqlite3.connect(SQLITE_PATH)
        conn.close()


@contextmanager
def connect():
    """
    Restituisce una connessione DB-API:
    - PostgreSQL: psycopg.connect(...)
    - SQLite: sqlite3.connect(...)

    Usabile come:
        with connect() as db:
            ...
    """
    if DATABASE_URL:
        if psycopg is None:
            raise RuntimeError(
                "psycopg non disponibile. In requirements.txt deve esserci: psycopg[binary]==3.x"
            )

        url = DATABASE_URL
        if url.startswith("postgres://"):
            url = url.replace("postgres://", "postgresql://", 1)

        conn = psycopg.connect(url)
        try:
            yield conn
        finally:
            conn.close()
    else:
        ensure_db_exists()
        conn = sqlite3.connect(SQLITE_PATH)
        try:
            yield conn
        finally:
            conn.close()


def init_db():
    """
    Crea le tabelle tramite SQLAlchemy (Base.metadata).
    Importante: i tuoi models devono importare Base da questo file
    (es: from app.db import Base) oppure usare lo stesso Base.
    """
    ensure_db_exists()
    Base.metadata.create_all(bind=engine)


def get_db():
    """
    Dependency FastAPI:
        def endpoint(db=Depends(get_db)):
            ...
    """
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
