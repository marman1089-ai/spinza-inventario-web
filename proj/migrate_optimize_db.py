'''
Migrazione UNA-TANTUM:
- legge le immagini già salvate nel DB (BYTEA) nelle tabelle:
  closures, invoices_docs, invoice_import_drafts
- le ricomprime/ridimensiona per farle pesare molto meno
- opzionalmente prova anche PNG e mantiene PNG solo se è più piccolo
- salva backup dell'originale in colonne *_original (sicurezza)
'''
import os
import argparse
import time

import psycopg2
from psycopg2.extras import DictCursor

from image_optimize import optimize_image_bytes

TABLES = [
    ("closures", "id"),
    ("invoices_docs", "id"),
    ("invoice_import_drafts", "id"),
]

def ensure_backup_columns(cur, table: str):
    cur.execute(f"ALTER TABLE {table} ADD COLUMN IF NOT EXISTS data_original BYTEA;")
    cur.execute(f"ALTER TABLE {table} ADD COLUMN IF NOT EXISTS content_type_original TEXT;")
    cur.execute(f"ALTER TABLE {table} ADD COLUMN IF NOT EXISTS filename_original TEXT;")
    cur.execute(f"ALTER TABLE {table} ADD COLUMN IF NOT EXISTS optimized_at TIMESTAMPTZ;")

def human(n: int) -> str:
    units = ["B", "KB", "MB", "GB"]
    f = float(n)
    i = 0
    while f >= 1024 and i < len(units) - 1:
        f /= 1024.0
        i += 1
    return f"{f:.2f} {units[i]}"

def connect_db():
    db_url = os.getenv("DATABASE_URL")
    if not db_url:
        raise SystemExit("ERRORE: variabile ambiente DATABASE_URL non impostata.")
    return psycopg2.connect(db_url)

def iter_rows(cur, table: str, id_col: str, batch: int, only_unoptimized: bool):
    where = "WHERE data IS NOT NULL"
    if only_unoptimized:
        where += " AND optimized_at IS NULL"
    cur.execute(f"SELECT count(*) as c FROM {table} {where};")
    total = int(cur.fetchone()["c"])
    offset = 0
    while offset < total:
        cur.execute(
            f"""
            SELECT {id_col} as id, filename, content_type, data
            FROM {table}
            {where}
            ORDER BY {id_col} ASC
            LIMIT %s OFFSET %s
            """,
            (batch, offset),
        )
        rows = cur.fetchall()
        if not rows:
            break
        yield rows
        offset += len(rows)

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true", help="Non scrive nulla nel DB, stampa solo stime.")
    ap.add_argument("--batch", type=int, default=50, help="Quante righe per volta (default 50).")
    ap.add_argument("--max-side", type=int, default=1600, help="Lato massimo immagine (default 1600px).")
    ap.add_argument("--jpeg-quality", type=int, default=60, help="Qualità JPEG (default 60).")
    ap.add_argument("--try-png", action="store_true", help="Prova anche PNG e tiene PNG solo se più piccolo.")
    ap.add_argument("--only-unoptimized", action="store_true", help="Processa solo righe non ancora ottimizzate.")
    args = ap.parse_args()

    conn = connect_db()
    conn.autocommit = False

    total_before = 0
    total_after = 0
    updated = 0
    started = time.time()

    try:
        with conn.cursor(cursor_factory=DictCursor) as cur:
            for table, _ in TABLES:
                ensure_backup_columns(cur, table)
        if not args.dry_run:
            conn.commit()
        else:
            conn.rollback()

        for table, id_col in TABLES:
            with conn.cursor(cursor_factory=DictCursor) as cur:
                print(f"\n=== {table} ===")
                for rows in iter_rows(cur, table, id_col, args.batch, args.only_unoptimized):
                    for r in rows:
                        row_id = r["id"]
                        filename = r["filename"] or "file.jpg"
                        data = r["data"]
                        if not data:
                            continue

                        before = len(data)
                        total_before += before

                        try:
                            new_bytes, fmt, new_ct = optimize_image_bytes(
                                data,
                                max_side=args.max_side,
                                jpeg_quality=args.jpeg_quality,
                                try_png=args.try_png,
                                keep_png_only_if_smaller=True,
                            )
                        except Exception as e:
                            print(f"[SKIP] id={row_id} errore ottimizzazione: {e}")
                            total_after += before
                            continue

                        after = len(new_bytes)
                        total_after += after

                        base = filename.rsplit(".", 1)[0]
                        new_filename = f"{base}.jpg" if fmt == "jpeg" else f"{base}.png"
                        savings = before - after

                        if args.dry_run:
                            print(f"id={row_id}  {human(before)} -> {human(after)}  (risparmio {human(max(savings,0))})  fmt={fmt}")
                            continue

                        cur.execute(
                            f"""
                            UPDATE {table}
                            SET
                              data_original = COALESCE(data_original, data),
                              content_type_original = COALESCE(content_type_original, content_type),
                              filename_original = COALESCE(filename_original, filename),
                              data = %s,
                              content_type = %s,
                              filename = %s,
                              optimized_at = NOW()
                            WHERE {id_col} = %s
                            """,
                            (psycopg2.Binary(new_bytes), new_ct, new_filename, row_id),
                        )
                        updated += 1

                    if not args.dry_run:
                        conn.commit()
                    else:
                        conn.rollback()

                print(f"Fatto {table}.")

        elapsed = time.time() - started
        print("\n=== RISULTATO ===")
        print(f"File aggiornati: {updated}")
        print(f"Totale PRIMA: {human(total_before)}")
        print(f"Totale DOPO : {human(total_after)}")
        print(f"Risparmio  : {human(max(total_before-total_after, 0))}")
        print(f"Tempo      : {elapsed:.1f}s")

    finally:
        conn.close()

if __name__ == "__main__":
    main()
