#!/usr/bin/env python3
"""Spinza - Migrazione immagini già salvate nel DB -> PDF compressi

Converte immagini (BYTEA) in PDF 1 pagina compressi dentro al DB, per:
- closures
- invoices_docs
- invoice_import_drafts

Salva backup dell'originale in colonne *_original (create se mancanti).
Usalo in locale con la stessa DATABASE_URL del progetto.

Esempi:
  python migrate_existing_to_pdf.py --dry-run
  python migrate_existing_to_pdf.py --max-side 1400 --jpeg-quality 55 --batch 50
  python migrate_existing_to_pdf.py --only-unoptimized
"""

import os
import argparse
import time
import re

import psycopg
from psycopg.rows import dict_row

from pdf_utils import image_to_compressed_pdf

TABLES = [
    ("closures", "id"),
    ("invoices_docs", "id"),
    ("invoice_import_drafts", "id"),
]

IMG_EXT_RE = re.compile(r"\.(jpe?g|png|webp|bmp|tiff?)$", re.IGNORECASE)

def ensure_backup_columns(cur, table: str):
    cur.execute(f"ALTER TABLE {table} ADD COLUMN IF NOT EXISTS data_original BYTEA;")
    cur.execute(f"ALTER TABLE {table} ADD COLUMN IF NOT EXISTS content_type_original TEXT;")
    cur.execute(f"ALTER TABLE {table} ADD COLUMN IF NOT EXISTS filename_original TEXT;")
    cur.execute(f"ALTER TABLE {table} ADD COLUMN IF NOT EXISTS optimized_at TIMESTAMPTZ;")

def human(n: int) -> str:
    units = ["B", "KB", "MB", "GB", "TB"]
    f = float(n)
    i = 0
    while f >= 1024 and i < len(units) - 1:
        f /= 1024.0
        i += 1
    return f"{f:.2f} {units[i]}"

def looks_like_image(filename: str | None, content_type: str | None) -> bool:
    ct = (content_type or "").lower().strip()
    if ct.startswith("image/"):
        return True
    if filename and IMG_EXT_RE.search(filename):
        return True
    return False

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true", help="Non scrive nulla nel DB (solo stime).")
    ap.add_argument("--batch", type=int, default=50, help="Righe per batch (default 50).")
    ap.add_argument("--max-side", type=int, default=1400, help="Lato massimo immagine (default 1400).")
    ap.add_argument("--jpeg-quality", type=int, default=55, help="Qualità JPG dentro al PDF (default 55).")
    ap.add_argument("--only-unoptimized", action="store_true", help="Processa solo righe con optimized_at IS NULL.")
    args = ap.parse_args()

    db_url = os.getenv("DATABASE_URL")
    if not db_url:
        raise SystemExit("ERRORE: imposta DATABASE_URL (Supabase Postgres connection string).")

    conn = psycopg.connect(db_url, row_factory=dict_row)
    conn.autocommit = False

    total_before = 0
    total_after = 0
    converted = 0
    start = time.time()

    try:
        with conn.cursor() as cur:
            for table, _ in TABLES:
                ensure_backup_columns(cur, table)
        if args.dry_run:
            conn.rollback()
        else:
            conn.commit()

        for table, id_col in TABLES:
            where = "WHERE data IS NOT NULL"
            if args.only_unoptimized:
                where += " AND optimized_at IS NULL"
            where += " AND (content_type IS NULL OR content_type <> 'application/pdf')"

            offset = 0
            while True:
                with conn.cursor() as cur:
                    cur.execute(
                        f"""SELECT {id_col} AS id, filename, content_type, data
                             FROM {table}
                             {where}
                             ORDER BY {id_col} ASC
                             LIMIT %s OFFSET %s""",
                        (args.batch, offset),
                    )
                    rows = cur.fetchall()

                if not rows:
                    break

                for r in rows:
                    row_id = r["id"]
                    filename = r.get("filename")
                    content_type = r.get("content_type")
                    data = r.get("data")
                    if not data:
                        continue

                    if not looks_like_image(filename, content_type):
                        continue

                    b = len(data)
                    total_before += b

                    try:
                        pdf_bytes = image_to_compressed_pdf(
                            data,
                            max_side=args.max_side,
                            jpeg_quality=args.jpeg_quality,
                        )
                    except Exception as e:
                        print(f"[SKIP] {table} id={row_id} errore conversione: {e}")
                        total_after += b
                        continue

                    a = len(pdf_bytes)
                    total_after += a

                    base = (filename or "file").rsplit(".", 1)[0]
                    new_filename = base + ".pdf"

                    if args.dry_run:
                        print(f"{table} id={row_id}: {human(b)} -> {human(a)}  ({human(max(b-a,0))} risparmiati)")
                        continue

                    with conn.cursor() as cur:
                        cur.execute(
                            f"""UPDATE {table}
                                  SET data_original = COALESCE(data_original, data),
                                      content_type_original = COALESCE(content_type_original, content_type),
                                      filename_original = COALESCE(filename_original, filename),
                                      data = %s,
                                      content_type = 'application/pdf',
                                      filename = %s,
                                      optimized_at = NOW()
                                WHERE {id_col} = %s""",
                            (pdf_bytes, new_filename, row_id),
                        )
                    converted += 1

                if args.dry_run:
                    conn.rollback()
                else:
                    conn.commit()

                offset += len(rows)

            print(f"OK: finito {table}")

        elapsed = time.time() - start
        print("\n=== RISULTATO ===")
        print(f"Convertiti: {converted}")
        print(f"Totale PRIMA: {human(total_before)}")
        print(f"Totale DOPO : {human(total_after)}")
        print(f"Risparmio  : {human(max(total_before-total_after, 0))}")
        print(f"Tempo      : {elapsed:.1f}s")

        if args.dry_run:
            print("\n(Nessuna modifica scritta: era un dry-run)")

    finally:
        conn.close()

if __name__ == "__main__":
    main()
