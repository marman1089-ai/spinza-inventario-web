"""Ripristino prudente del backup SQLite incluso verso PostgreSQL.

Il ripristino avviene SOLTANTO quando:
- DATABASE_URL punta a PostgreSQL;
- il database PostgreSQL non contiene alcuna riga nelle tabelle applicative;
- il backup spinza.db incluso esiste e contiene dati.

Non cancella, non sovrascrive e non fonde dati con un database già popolato.
"""
from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Any, Iterable

try:
    from psycopg import sql
except ImportError:
    sql = None

from .db import connect, using_postgres

PROJECT_ROOT = Path(__file__).resolve().parent.parent
BACKUP_SQLITE_PATH = PROJECT_ROOT / "backup" / "spinza_originale.db"


def _first_value(row: Any):
    if row is None:
        return None
    if isinstance(row, dict):
        return next(iter(row.values()), None)
    return row[0]


def _sqlite_table_names(db: sqlite3.Connection) -> list[str]:
    rows = db.execute(
        """
        SELECT name
        FROM sqlite_master
        WHERE type='table'
          AND name NOT LIKE 'sqlite_%'
        ORDER BY name
        """
    ).fetchall()
    return [str(row[0]) for row in rows]


def _postgres_table_names(cur) -> list[str]:
    cur.execute(
        """
        SELECT table_name
        FROM information_schema.tables
        WHERE table_schema='public' AND table_type='BASE TABLE'
        ORDER BY table_name
        """
    )
    return [str(_first_value(row)) for row in cur.fetchall()]


def _postgres_is_completely_empty(cur, tables: Iterable[str]) -> bool:
    for table in tables:
        cur.execute(
            sql.SQL("SELECT EXISTS (SELECT 1 FROM {} LIMIT 1)").format(
                sql.Identifier(table)
            )
        )
        if bool(_first_value(cur.fetchone())):
            return False
    return True


def _source_has_data(db: sqlite3.Connection, tables: Iterable[str]) -> bool:
    for table in tables:
        safe_table = table.replace('"', '""')
        row = db.execute(f'SELECT EXISTS(SELECT 1 FROM "{safe_table}" LIMIT 1)').fetchone()
        if row and bool(row[0]):
            return True
    return False


def _sqlite_columns(db: sqlite3.Connection, table: str) -> list[str]:
    safe_table = table.replace('"', '""')
    return [str(row[1]) for row in db.execute(f'PRAGMA table_info("{safe_table}")').fetchall()]


def _postgres_columns(cur, table: str) -> list[str]:
    cur.execute(
        """
        SELECT column_name
        FROM information_schema.columns
        WHERE table_schema='public' AND table_name=%s
        ORDER BY ordinal_position
        """,
        (table,),
    )
    return [str(_first_value(row)) for row in cur.fetchall()]


def _reset_serial_sequence(cur, table: str, columns: list[str]) -> None:
    if "id" not in columns:
        return
    cur.execute("SELECT pg_get_serial_sequence(%s, %s)", (table, "id"))
    row = cur.fetchone()
    sequence_name = _first_value(row)
    if not sequence_name:
        return

    cur.execute(
        sql.SQL("SELECT MAX({}) FROM {}").format(
            sql.Identifier("id"), sql.Identifier(table)
        )
    )
    max_id_row = cur.fetchone()
    max_id = _first_value(max_id_row)
    if max_id is None:
        return

    # sequence_name arriva da PostgreSQL, non dall'utente.
    cur.execute("SELECT setval(%s, %s, true)", (sequence_name, int(max_id)))


def restore_sqlite_backup_if_postgres_empty() -> dict[str, int | str | bool]:
    """Importa il backup solo in un PostgreSQL totalmente vuoto.

    Ritorna un piccolo report per i log di avvio.
    """
    if not using_postgres():
        return {"restored": False, "reason": "not_postgres", "rows": 0}
    if sql is None:
        raise RuntimeError("psycopg non installato: impossibile ripristinare PostgreSQL")

    if not BACKUP_SQLITE_PATH.exists() or BACKUP_SQLITE_PATH.stat().st_size <= 0:
        return {"restored": False, "reason": "backup_missing", "rows": 0}

    source = sqlite3.connect(BACKUP_SQLITE_PATH)
    source.row_factory = sqlite3.Row
    try:
        source_tables = _sqlite_table_names(source)
        if not _source_has_data(source, source_tables):
            return {"restored": False, "reason": "backup_empty", "rows": 0}

        with connect() as target:
            cur = target.cursor()
            target_tables = _postgres_table_names(cur)

            # La regola più importante: se il DB online contiene anche una sola riga,
            # non tocchiamo niente e non tentiamo alcuna fusione automatica.
            if not _postgres_is_completely_empty(cur, target_tables):
                return {"restored": False, "reason": "target_not_empty", "rows": 0}

            common_tables = [t for t in source_tables if t in set(target_tables)]
            total_rows = 0
            restored_tables = 0

            for table in common_tables:
                source_columns = _sqlite_columns(source, table)
                target_columns = _postgres_columns(cur, table)
                target_column_set = set(target_columns)
                columns = [c for c in source_columns if c in target_column_set]
                if not columns:
                    continue

                quoted_table = table.replace('"', '""')
                quoted_cols = ", ".join('"' + c.replace('"', '""') + '"' for c in columns)
                rows = source.execute(
                    f'SELECT {quoted_cols} FROM "{quoted_table}"'
                ).fetchall()
                if not rows:
                    continue

                statement = sql.SQL("INSERT INTO {} ({}) VALUES ({}) ON CONFLICT DO NOTHING").format(
                    sql.Identifier(table),
                    sql.SQL(", ").join(sql.Identifier(c) for c in columns),
                    sql.SQL(", ").join(sql.Placeholder() for _ in columns),
                )
                values = [tuple(row[c] for c in columns) for row in rows]
                cur.executemany(statement, values)
                total_rows += len(values)
                restored_tables += 1
                _reset_serial_sequence(cur, table, columns)

            return {
                "restored": total_rows > 0,
                "reason": "restored" if total_rows > 0 else "no_common_rows",
                "rows": total_rows,
                "tables": restored_tables,
            }
    finally:
        source.close()
