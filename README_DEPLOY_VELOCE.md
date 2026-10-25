# Avvio Render ottimizzato – 25 ottobre 2026

## Modifiche
- Tolte le copie di progetto, database locali dimostrativi, cache, allegati e documentazione vecchia **dal pacchetto di pubblicazione**. Non toccare i dati nel database di Render/Supabase.
- Rimossa la scansione OCR automatica delle fatture storiche all'avvio (la digitalizzazione manuale della cucina resta disponibile).
- In Postgres le migrazioni SQL vengono eseguite alla **prima partenza della nuova versione** e poi saltate ai riavvii ordinari, tramite `app_schema_bootstrap`.
- Rimangono attivi caricamento multiplo, pulsanti `+ 1 file` e fotocamera.

## Attenzione
La **prima** pubblicazione puo ancora essere lenta per creare/aggiornare lo schema; le **successive partenze** sono quelle ottimizzate. Il pacchetto piu leggero da solo non puo eliminare un eventuale cold-start del piano gratuito o ritardi di rete Supabase.

## Render
Root Directory vuota; Build `pip install -r requirements.txt`; Start `uvicorn app.main:app --host 0.0.0.0 --port $PORT`.
Controlla che `DATABASE_URL` e `SESSION_SECRET` restino invariati. NON sostituire o cancellare database e media di produzione. Backup consigliato.
Per ripetere deliberatamente gli aggiornamenti al database: impostare temporaneamente `FORCE_SCHEMA_SYNC_ON_START=1` e poi rimuoverlo.
Il vecchio OCR batch e spento per impostazione predefinita; non attivare `ENABLE_LEGACY_DOCUMENT_OCR=1` se non ti serve.

Se anche il **secondo riavvio** dura 8 minuti, raccogli i Build Logs e Runtime Logs Render con i timestamp: un cold-start o connessione al database potrebbe essere la causa.
