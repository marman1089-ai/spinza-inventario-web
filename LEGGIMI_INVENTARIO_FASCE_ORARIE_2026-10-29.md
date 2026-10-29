# Spinza — aggiornamento inventario e fasce orarie

Data: 29 ottobre 2026.

## Correzione inventario cucina

La query di inserimento di inventory_period_count_lines aveva nove colonne e nove valori, ma soltanto otto segnaposto. La conferma falliva e la transazione veniva annullata. Corretti i nove segnaposto.

Conferma, righe del report, giacenze, log e rimozione della bozza rimangono nella stessa transazione. Un errore annulla l'intera operazione. Le conferme simultanee sono serializzate per negozio/reparto: una seconda conferma della stessa data restituisce 409 senza duplicare il report.

Corretto anche il riuso delle connessioni PostgreSQL: la modalità autocommit impiegata dalle vecchie migrazioni non deve propagarsi ai salvataggi successivi nel pool.

## Report consumi per periodo

- Ogni scheda conserva prodotto, categoria, unità e quantità al momento della chiusura; i conteggi successivi non la riscrivono.
- La singola scheda è visibile anche al primo conteggio, prima che sia possibile calcolare consumi.
- Elenco delle schede archiviate nel periodo, dettaglio, CSV e stampa/PDF tramite browser.
- Fatture dello stesso negozio e periodo elencate nel report, con apertura dell'originale per confronto manuale. Nessuna equivalenza automatica tra quantità consumate e importo delle fatture.
- Riconosciuti i log di entrata recenti con nome prodotto, posizione e numero d'ordine, oltre ai carichi fattura effettivi. Le fatture riconciliate senza carico non vengono conteggiate come nuovi arrivi.
- I confini dei conteggi sono giorni Europe/Rome; la ricerca nei log UTC converte tali confini.

I consumi richiedono due conteggi completi e gli arrivi registrati tra essi: non vengono inventate stime per periodi incompleti. I movimenti storici privi di tracciatura non possono essere ricostruiti dal solo aggiornamento. Il primo inventario è un punto di partenza, non un consumo.

## Admin → Fasce orarie

Nuova pagina /admin/fasce-orarie, collegata esclusivamente dal pannello admin, accanto ai Log. Filtro per giorno e negozio; asse X 00:00–24:00, asse Y utenti/dipendenti del negozio, barre verdi e minuti effettivamente rilevati. Gli utenti senza attività restano visibili; lo storico conserva il nome registrato anche se l'utente viene successivamente rimosso.

Il rilevamento riguarda /inventario, le sue pagine operative e /inventario-controllo, con un negozio selezionato; esclude le pagine report. Richiede JavaScript, pagina visibile, finestra attiva e un'interazione (tastiera, input, scroll, tocco). Invia campioni ogni 15 secondi e sospende dopo 60 secondi senza interazioni. Il primo campione avvia l'intervallo; una pausa superiore a 30 secondi tra campioni non viene riempita. Le schede simultanee sono unite per non duplicare i minuti. Non è una misura di presenza al lavoro; la risoluzione è indicativa e gli ultimi secondi prima di chiudere la pagina possono non essere rilevati.

Gli orari sono registrati come istanti UTC sul server e visualizzati in Europe/Rome, con giorni di 23/25 ore, taglio a mezzanotte e totale calcolato sul tempo trascorso. Nel giorno autunnale l'ora ripetuta condivide la posizione nel grafico, con CET/CEST nei dettagli. Nessun dato precedente all'installazione viene inventato.

## Database e migrazioni

Le tabelle inventory_period_count_headers, inventory_period_count_lines e inventory_week_drafts vengono create senza eliminare dati. Le nuove inventory_activity_sessions e inventory_activity_intervals, con indici di ricerca, vengono create all'avvio in modo idempotente. Nessun reset del database è richiesto. La verifica di creazione per l'attività viene memorizzata nel processo per evitare DDL a ogni campione.

Il codice usa segnaposto, chiavi e tipi distinti per SQLite/PostgreSQL. SSL resta richiesto. Disattivata la preparazione automatica delle query psycopg per supportare anche il pooler Supabase in modalità transazione. Aggiunta tzdata, necessaria anche su Windows.

## Installazione su Render / Supabase

1. Fare un backup del database attualmente in produzione.
2. Usare la ROOT di questo ZIP: contiene render.yaml, requirements.txt, run.py e la cartella app aggiornata. Le cartelle storiche già presenti nell'archivio originale (appsrc, fix, proj, spinza-inventario-web-main, backup) sono conservate; non sono il codice di avvio aggiornato e non vanno usate come Root Directory Render.
3. Conservare la DATABASE_URL PostgreSQL del progetto Supabase corretto e SESSION_SECRET già in uso. Non sostituirli con valori di esempio; la stessa SESSION_SECRET evita di invalidare le sessioni esistenti.
4. Build: pip install -r requirements.txt. Avvio: uvicorn app.main:app --host 0.0.0.0 --port $PORT. Restano quelli del render.yaml allegato.
5. Riavviare/deployare. La migrazione avviene automaticamente. Su Render l'avvio senza DATABASE_URL ora fallisce esplicitamente, evitando salvataggi nel file SQLite temporaneo.
6. Lasciare disattivati RESTORE_SQLITE_ON_START e MIGRATE_ON_START, salvo un ripristino deliberatamente pianificato. I database SQLite originali inclusi nello ZIP non sono una copia aggiornata di Supabase e non vanno importati sopra la produzione.
7. Verifica dopo deploy: confermare una scheda, riaprire lo stesso report dopo un riavvio del servizio, controllare quantità e CSV; usare l'inventario con un dipendente per almeno 30 secondi e aprire Admin → Fasce orarie.

Su Render i file locali sono temporanei: la persistenza di produzione dipende dalla DATABASE_URL valida verso PostgreSQL/Supabase. Questo aggiornamento non effettua deploy né modifica il tuo database remoto.

Fonti ufficiali controllate:
- [Render: filesystem temporaneo](https://render.com/docs/faq)
- [Supabase: connessioni e limitazioni del transaction pooler](https://supabase.com/docs/guides/database/connecting-to-postgres)
- [Psycopg: prepared statements](https://www.psycopg.org/psycopg3/docs/advanced/prepare.html)

## Test e limiti di verifica

18 test specifici passati su database SQLite isolati: prima/seconda archiviazione, rilettura con nuovo client e connessione, CSV, annullamento transazione con errore simulato, bozza, conferma simultanea, validazioni, permessi tra negozi, fatture originali, campioni attività, pausa/ripresa, mezzanotte/cambio ora, migrazione su copia del DB allegato senza perdita prodotti, blocco SQLite su Render. Il ripristino della modalità transazionale PostgreSQL è verificato con un pool simulato, non con Supabase reale.

Test JavaScript passato: nessuna attività per semplice apertura pagina, sospensione per pagina nascosta, finestra non attiva e inattività; ripresa con interazione; esclusione report.

Suite completa: 46 test, 42 passati e 4 problemi preesistenti. Gli stessi quattro sono stati riprodotti sulla suite originale non modificata (28 test, 24 passati):
- test_document_upload_opens_digital_review
- test_staff_photo_only_and_originals_protected
- test_admin_can_create_written_record_without_original
- test_csv_admin_import_preview_dedup_and_reconciliation

Questi test aspettano percorsi di digitalizzazione che il progetto allegato disattiva già con risposte 410 o reindirizza all'elenco fatture. Tale comportamento non è stato riattivato in questo intervento.

Test eseguiti con Python 3.12 su Windows; il progetto conserva il pin originale Python 3.11.9 per Render. Non sono stati eseguiti test su un server Render attivo o su PostgreSQL/Supabase reale, perché non sono disponibili connessione e credenziali. La compatibilità remota è stata controllata nel codice e sulle indicazioni ufficiali; la verifica finale sulla tua istanza è indicata sopra.

Comandi ripetibili dalla root (con le dipendenze installate):

    pip install -r requirements-dev.txt
    python -m unittest discover -s tests -p test_inventory_archive.py -v
    node tests/test_activity_client.cjs
    python -m unittest discover -s tests -v

I file dati originali dell'allegato sono conservati senza modifiche; database di prova, dipendenze scaricate e cache Python non sono inclusi nel pacchetto.

La versione 2 aggiunge cambio negozio accanto al logo, temi rosso/verde scuri, report attività con percentuali ed eliminazione admin delle schede errate. Vedere LEGGIMI_VERSIONE_2.md per comportamento e istruzioni aggiornati.
