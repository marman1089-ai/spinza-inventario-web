# Spinza – compressione foto già nel DB (Supabase)

Questo pacchetto serve per ridurre moltissimo il peso delle foto JPG già caricate (BYTEA) nel database.
Pensato per arrivare a **1000+ foto/anno** senza saturare subito.

## Cosa fa
- ridimensiona (max lato, es. 1600px)
- ricomprime (qualità JPEG, es. 60)
- opzionale: prova anche PNG e lo usa solo se più piccolo (flag `--try-png`)
- salva backup dell’originale in colonne `*_original` (sicurezza)

## File
- `migrate_optimize_db.py`  → script che fa la migrazione una-tantum
- `image_optimize.py`       → funzioni di compressione
- `requirements_migrate.txt`→ librerie
- `README.md`               → istruzioni

## Come usarlo (Windows)
1) Scompatta lo zip
2) Apri terminale nella cartella
3) `python -m venv .venv`
4) `.venv\Scripts\activate`
5) `pip install -r requirements_migrate.txt`
6) Imposta `DATABASE_URL` (Supabase → Project Settings → Database → Connection string):
   `set DATABASE_URL=postgresql://...`
7) Prova senza scrivere:
   `python migrate_optimize_db.py --dry-run --batch 20`
8) Esegui davvero:
   `python migrate_optimize_db.py --batch 50 --max-side 1600 --jpeg-quality 60`

### Consigli
- `--jpeg-quality 55-65` è ottimo per fatture/chiusure
- `--max-side 1400-1600` basta e avanza
- usa `--only-unoptimized` se vuoi riprendere in futuro senza rifare tutto
