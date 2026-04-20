# Convertire anche i file già caricati (immagini -> PDF)

Questo progetto ora salva i *nuovi upload* come PDF compressi.
Per convertire **anche quelli vecchi** già nel database (BYTEA), usa lo script:

`migrate_existing_to_pdf.py`

## Requisiti
Le dipendenze sono già in `requirements.txt` (psycopg, Pillow, img2pdf).

## Come eseguirlo (Windows)
1. Scarica il codice in locale (o usa questo repo)
2. Apri terminale nella cartella del progetto
3. (Consigliato) crea venv:
   - `python -m venv .venv`
   - `.venv\Scripts\activate`
4. Installa:
   - `pip install -r requirements.txt`
5. Imposta la variabile:
   - `set DATABASE_URL=postgresql://...`  (Supabase -> Project Settings -> Database -> Connection string)

### Prova (non scrive nulla)
`python migrate_existing_to_pdf.py --dry-run --batch 20`

### Esegui davvero
`python migrate_existing_to_pdf.py --max-side 1400 --jpeg-quality 55 --batch 50`

### Se vuoi riprendere senza rifare tutto
`python migrate_existing_to_pdf.py --only-unoptimized`

## Sicurezza
Lo script salva una copia dell'originale in:
- `data_original`
- `content_type_original`
- `filename_original`

e marca la riga con `optimized_at`.
