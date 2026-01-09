# Spinza Inventario (Web)

Web-app (telefono + PC) per inventario Spinza: login, quantità, soglia minima (rosso), log modifiche.

## Avvio in locale (test)
1. Installa Python 3.10+
2. Dentro la cartella del progetto:
   - `pip install -r requirements.txt`
   - `python -m uvicorn app.main:app --reload`
3. Apri: http://127.0.0.1:8000

## Deploy su Render (consigliato)
### 1) Metti il progetto su GitHub
- Crea repo nuovo
- Carica tutti i file di questa cartella

### 2) Crea il servizio su Render
- New → Web Service → collega il repo
- Build Command: `pip install -r requirements.txt`
- Start Command: `uvicorn app.main:app --host 0.0.0.0 --port $PORT`

### 3) Persistent Disk (per non perdere dati)
- Aggiungi un **Persistent Disk**
- Mount path: `/var/data`

### 4) Variabili ambiente (Environment)
Imposta queste variabili:
- `SPINZA_DB_PATH` = `/var/data/spinza.db`
- `SEED_DB_PATH` = `app/seed/spinza_seed.db`
- `SESSION_SECRET` = una stringa lunga random (es. 40+ caratteri)
- `ADMIN_USERNAME` = (es. `marco06`)
- `ADMIN_PASSWORD` = una password forte

> Al primo avvio, se `/var/data/spinza.db` non esiste, verrà copiato il database seed.

## Note sicurezza
- Gli utenti vecchi (salvati con SHA256) vengono aggiornati automaticamente a PBKDF2 al primo login riuscito.
