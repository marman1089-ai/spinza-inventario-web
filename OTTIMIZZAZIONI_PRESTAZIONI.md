# Aggiornamento prestazioni – 25 ottobre 2026

## Modifiche effettive
- **Ripristinato `requirements.txt`**, assente nell'archivio alleggerito precedente: con il `render.yaml` incluso, l'assenza impediva la build da root.
- **Pool connessioni PostgreSQL/Supabase** (`psycopg_pool`): le connessioni TLS ora vengono riutilizzate tra richieste, invece di aprirne una nuova per ogni query/blocco `with connect()`; max 5 connessioni per istanza e min 0. Personalizzabile con `DB_POOL_MAX`.
- **Niente prefetch delle pagine sul passaggio del mouse o tocco**: il precedente script apriva pagine aggiuntive prima di cliccare, appesantendo database e server.
- **Compressione e unione PDF in thread separato** nei caricamenti di fatture, chiusure e spese secondarie: altre richieste non rimangono bloccate mentre il server elabora fotografie.
- Mantenuti il caricamento da fotocamera, più file e la gestione inventario.

## Limiti e verifica
Non è possibile promettere un secondo senza misure dal server reale e dal dispositivo. Fotografie grandi richiedono upload su rete e compressione: il tempo dipende dalla dimensione e dalla connessione. Il piano Render gratuito può sospendersi e fare cold-start; lo spazio dati e la velocità di Supabase incidono sulle pagine.

Dopo la pubblicazione testare: accesso, cambio negozio, salvataggio una modifica inventario, fattura con 2-3 foto, allegati multipagina e riavvio. Controllare Runtime Logs Render e tempi DevTools Network.

## Pubblicazione
Mettere `requirements.txt` nella root insieme a `render.yaml` e alla cartella `app/`, mantenere le variabili DATABASE_URL e SESSION_SECRET già esistenti. Effettuare backup DB prima del deploy.
