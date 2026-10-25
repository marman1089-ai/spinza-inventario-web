# Correzioni 25 ottobre 2026

## Documenti multi-foto
- Fatture, chiusure e spese secondarie: il selettore ora ha `multiple` e **non** ha `capture=environment` (che su smartphone spesso costringe a una sola foto).
- Seleziona più foto già presenti nella galleria in un solo gesto; ogni caricamento genera un unico PDF multipagina nell’ordine selezionato.
- Supporto JPG, PNG, WEBP e PDF. Limite 18 MB per singolo allegato.
- Per scattare più foto direttamente dalla fotocamera, scattale prima nell'app Fotocamera e poi selezionale insieme dalla galleria.

## Avvio Render
- Saltati di default due pesanti lavori di manutenzione/ripristino che prima erano eseguiti a **ogni** avvio, con molte query verso PostgreSQL remoto.
- `RESTORE_SQLITE_ON_START=1` abilita, solo quando davvero necessario, il ripristino dal backup SQLite (ATTENZIONE: non abilitarlo normalmente).
- `REPAIR_CASH_ON_START=1` abilita una tantum la riclassificazione/riparazione movimenti contabili.
- Inizializzazione schema e account admin conservate per compatibilità.
- Su **Render Free** la sospensione per inattività può comunque causare cold start: l'ottimizzazione del codice non garantisce un avvio immediato. Verificare Render Logs: se tempo in `init_db` o errori DB, indagare latenza e disponibilità di Supabase. Per eliminare la sospensione usare un servizio sempre attivo.

## Pubblicazione
Effettuare backup database prima di sostituire i file; pubblicare il pacchetto e controllare nei log di Render tempi e possibili errori di avvio. Non è stato possibile testare la piattaforma Render remota o il database di produzione.
