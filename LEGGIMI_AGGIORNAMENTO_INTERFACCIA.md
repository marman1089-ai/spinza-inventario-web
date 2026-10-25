# Gestionale – revisione del 25 ottobre 2026

## Cosa cambia
- **Fatture, chiusure e spese secondarie** restano archivi fotografici/PDF. Nuovo inserimento digitale e OCR non sono più disponibili nell'interfaccia; gli endpoint obsoleti rispondono con HTTP 410. Gli originali e i dati storici esistenti non vengono cancellati.
- **Cucina:** nuova scheda di conteggio settimanale compilabile, con salvataggio bozza sul database, ricerca prodotti, conferma ufficiale separata e cronologia di schede chiuse.
- **Sala:** le quantità e i log delle singole variazioni restano operativi. È ancora disponibile il conteggio sala; chi lo usa può salvare una bozza e generare un report.
- **Report inventario:** consultazione/stampa PDF via browser e CSV della scheda storica selezionata.
- **Log inventario:** nuova visualizzazione a eventi, indicatori sintetici e filtri per utente/data.
- **Aspetto:** componenti, tipografia, pannelli, transizioni e adattamento smartphone migliorati nelle pagine rinnovate.

## Installazione
1. Eseguire il backup del database **di produzione** e, se previsti, degli allegati esterni.
2. Aggiornare il codice applicativo mantenendo `DATABASE_URL`, `SESSION_SECRET` e i volumi persistenti: il database `spinza.db` nel pacchetto **non** va caricato sopra quello di produzione.
3. Riavviare l'applicazione. La nuova tabella `inventory_week_drafts` viene creata alla prima visita del modulo schede.
4. Verificare accessi admin/dipendenti, caricamento originali, inventario, salvataggio bozza e conferma report usando un ambiente di test prima della produzione.

## Precisazioni
- Non viene aggiornato automaticamente alcun sito live.
- I report vecchi restano nel database; le nuove schede ufficiali aggiornano le giacenze al momento della conferma, **non** quando si salva una bozza.
- Il salvataggio bozza è per sede, reparto e settimana (lunedì–domenica); una nuova bozza della stessa settimana sovrascrive la precedente.
- Il testo/OCR storico, se esiste, non è stato eliminato dal DB; è semplicemente non più accessibile attraverso le rotte disattivate.
