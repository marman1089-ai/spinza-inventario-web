# Spinza — versione 2, cambio negozio e report utilizzo

29 ottobre 2026. ZIP completo: Spinza_aggiornato_v2_2026-10-29.zip.

## Modifiche richieste

- Fasce orarie è nel pannello Admin, accanto a Log. Rimosso il collegamento diretto dal menu desktop e mobile.
- Un'unica tendina accanto al logo cambia il negozio, anche da telefono: Spinza Santa'Agostino, Reburger San Felice, Reburger Camaldoli; conservata anche la vista Tutti i negozi.
- Reburger San Felice usa la stessa chiave interna reburger_maggio: nessuno spostamento o rinomina dei dati esistenti nel DB.
- Il negozio selezionato viene usato per il logo e il tema in tutte le pagine, anche quelle che prima non passavano tale informazione all'intestazione.
- Cambiando negozio vengono rimossi i riferimenti a schede e documenti del negozio precedente. Dove possibile rimangono giorno, periodo e reparto; dalle pagine di dettaglio non riutilizzabili si torna all'inventario. I negozi dei dipendenti restano protetti dai controlli esistenti.
- Tema scuro verde per entrambi i Reburger; tema scuro rosso per Spinza; logo Reburger reso leggibile sullo sfondo scuro. Nuove transizioni leggere, indicatori di attività e feedback durante il cambio negozio. Le nuove animazioni rispettano la preferenza di movimento ridotto.

## Fasce orarie più immediate

Stato In uso ora, primo ingresso, durata della sessione in corso, totale giornaliero e confronto percentuale. Chi è attivo compare per primo. La prima interazione registra subito un ingresso; il tempo cresce con i campioni successivi. La vista di oggi si aggiorna automaticamente ogni 15 secondi e sospende il ricaricamento dei dati mentre si leggono i dettagli aperti.

Su telefono le informazioni principali compaiono in schede individuali, senza dover scorrere una tabella larga. Resta il grafico delle 24 ore con intervalli verdi, nomi dipendenti e dettagli di ingresso/uscita. Il grafico può essere scorso orizzontalmente su schermi piccoli.

Il primo ingresso indica la prima interazione con l'inventario, non il semplice login. Il rilevamento richiede pagina visibile e finestra attiva; dopo 60 secondi senza interazione viene sospeso e il cambio finestra/scheda chiude lo stato attivo. I campioni arrivano ogni 15 secondi: non è una misura esatta al secondo né un sistema di presenze. Le schede sovrapposte si uniscono.

## Report giornalieri e settimanali

Per ciascuna persona e negozio:

- Tempo del giorno scelto.
- Media dei 7 giorni di calendario precedenti, compresi quelli con zero tempo rilevato.
- Variazione percentuale: (tempo del giorno − media) / media × 100.
- Tempo della settimana del giorno scelto, dal lunedì fino a quel giorno.
- Confronto con gli stessi giorni della settimana precedente e relativa variazione percentuale.
- Dettaglio degli ultimi 7 giorni e delle ultime 4 settimane. Le settimane precedenti completate sono confrontate con settimane complete.
- CSV con dipendente, negozio, periodo, minuti, riferimento e percentuale.

Se il riferimento è zero compare N/D, senza percentuali infinite. Il giorno in corso è parziale. I dati anteriori all'installazione non esistono: all'inizio le medie avranno pochi dati e le percentuali andranno interpretate di conseguenza. Il tempo misura utilizzo rilevato, non produttività o ore lavorate.

Europe/Rome, gestione UTC, mezzanotte, ora legale e unione degli intervalli restano attivi. Nessuna ricostruzione artificiale delle attività passate.

## Eliminazione dei report errati

Aprire una scheda da Report consumi per periodo e premere Elimina report. Il pulsante è visibile soltanto agli admin e richiede una conferma esplicita. La richiesta è protetta da un token di sessione; l'eliminazione è tracciata nei Log.

Vengono eliminate la scheda e le sue righe. Le giacenze attuali non vengono modificate: se anche le quantità sono sbagliate, occorre rifare il conteggio. Eliminando l'ultimo report si può ripetere il conteggio della stessa data, purché successiva all'ultimo report rimasto.

Se viene rimosso un conteggio intermedio, il conteggio successivo viene confrontato con quello precedente rimasto e somma gli arrivi già registrati nei due intervalli. Le quantità fisiche contate nei report conservati restano intatte. Eliminando il primo riferimento, il successivo diventa un nuovo punto di partenza senza consumo calcolato. Se mancano dati sufficienti, il consumo non viene stimato. Eliminazione, aggiornamento confronti e log sono atomici; un errore annulla tutto.

## Inventario e salvataggi

Sono comprese tutte le correzioni della versione precedente: errore nella query di archiviazione, salvataggio atomico di scheda e giacenze, protezione da doppia conferma contemporanea, storico consultabile anche al primo conteggio, CSV e fatture dello stesso periodo. Restano anche le correzioni per connessioni PostgreSQL condivise e pooler Supabase.

Non occorrono rinomine dei negozi nel database. Le tabelle dei report e dell'attività vengono create all'avvio senza cancellare dati.

## Installazione

1. Estrarre lo ZIP e sostituire i file nella ROOT del repository GitHub: quella con app, requirements.txt, run.py e render.yaml. Non caricare soltanto lo ZIP.
2. Usare il codice app della root, non le copie storiche conservate in appsrc, fix, proj o spinza-inventario-web-main.
3. Fare commit e attendere il deploy di Render, se il deploy automatico è attivo.
4. Conservare DATABASE_URL e SESSION_SECRET già presenti su Render. Non importare i vecchi file SQLite sopra Supabase. I file DB originali dello ZIP sono stati conservati invariati.
5. Dopo il deploy verificare il cambio tra i tre negozi: nome, logo, colori e prodotti devono corrispondere. Aprire Admin → Fasce orarie, utilizzare l'inventario con un dipendente e controllare il segnale entro il successivo aggiornamento di 15 secondi.

Il confronto sulla tua istanza reale Render/Supabase resta da eseguire dopo il deploy: non sono disponibili le relative credenziali. Il gestionale non è stato pubblicato da questa sessione.

## Verifiche

- 18 test specifici superati: salvataggi, storico, permessi, migrazione su copia del DB allegato, rollback, doppia conferma contemporanea, timezone/DST, cambio negozio ripetuto con logo/tema, rimozione riferimenti al vecchio negozio, medie e percentuali, confronti settimanali, CSV e segnale immediato/pausa.
- Test JavaScript superato: ingresso immediato alla prima interazione, limitazione frequenza, scheda nascosta, cambio finestra, inattività, ripresa ed esclusione delle pagine report.
- Verifica in browser Edge su desktop 1440 px e telefono 390 px: nessuno sconfinamento orizzontale della pagina; logo Reburger e classe tema corretti. Anteprime con dati fittizi, senza collegamento al sito reale.
- Suite completa: 46 test, 42 superati. Restano gli stessi quattro problemi della suite originale, già riprodotti prima di queste modifiche, relativi a funzioni di digitalizzazione disattivate nell'allegato. Dettagli nel LEGGIMI_INVENTARIO_FASCE_ORARIE_2026-10-29.md.

Test ripetibili:

    pip install -r requirements-dev.txt
    python -m unittest discover -s tests -p test_inventory_archive.py -v
    node tests/test_activity_client.cjs

Fonti e dettagli Render/Supabase sono riportati nel LEGGIMI tecnico incluso. Nessun test è stato eseguito contro un database Supabase reale; la verifica del pool PostgreSQL usa una simulazione e il codice SQL è stato controllato per entrambe le varianti.
