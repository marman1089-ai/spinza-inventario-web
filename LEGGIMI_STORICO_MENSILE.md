# Correzioni dello storico mensile

Lo storico mostra prima il mese corrente secondo l’ora italiana, poi i mesi
più vicini e infine quelli più lontani. A parità di distanza, il mese passato
precede quello futuro: per esempio novembre, ottobre, dicembre, settembre.
Il mese corrente è evidenziato. Se imposti un intervallo che lo esclude,
vedrai solo i mesi del filtro, sempre ordinati per vicinanza al mese corrente.

## Errori corretti nel programma

- Il mese corrente poteva mancare se non aveva movimenti. Ora compare anche
  con importi pari a zero, insieme ai mesi intermedi dello storico.
- Lo storico si interrompeva dopo 600 mesi, escludendo senza avviso quelli
  più recenti negli archivi molto lunghi. Eliminato il taglio silenzioso.
- Anche i menu di selezione dei mesi potevano perdere i mesi recenti per
  due limiti di 120 e 60 voci. Ora comprendono l’intero periodo disponibile.
- Il filtro accettava mesi inesistenti come 2026-13. Ora controlla mese e
  anno reali; i valori non validi ricadono sui limiti disponibili. I mesi
  inesistenti eventualmente presenti nei dati non sono attribuiti al mese
  corrente. Nessun record salvato viene riscritto o cancellato.
- Per determinare il mese corrente, storico e menu ora usano Europe/Rome,
  evitando differenze rispetto alla data italiana al cambio del mese.

## Controlli aggiornati

Alcuni test si riferivano alla vecchia digitalizzazione, già disattivata nel
progetto, e a vecchi permessi sui documenti. Sono stati aggiornati per
verificare l’archivio degli originali, i blocchi della funzione dismessa e
l’eliminazione dei propri documenti in giornata. La digitalizzazione non è
stata riattivata. Il test del pool PostgreSQL ora usa un driver simulato,
coerentemente con la connessione simulata, senza dipendere dal driver locale.

## Verifiche

Verificati ordine dei mesi passati e futuri, mese corrente senza movimenti,
intervalli invertiti, mesi non validi, totali, separazione dei negozi,
storici oltre 600 mesi, menu dei mesi e pagina HTML effettiva. Controllati
anche il cambio negozio mantenendo lo storico e il periodo selezionato.
Eseguiti i test del progetto su database SQLite temporanei; le verifiche
PostgreSQL usano simulazioni e non una connessione di produzione.
Verificate la sintassi Python, i template HTML e i file JavaScript.
Superati anche i tre controlli JavaScript su attività, presenza e spese.

## Installazione

A gestionale fermo, estrai aggiornamento_storico_mensile.zip nella cartella
che contiene run.py, sovrascrivi i file app/ inclusi e riavvia il gestionale.
Conserva una copia dei file precedenti. Non sostituire il database spinza.db
della tua installazione con quello del progetto completo.

Entrambi gli ZIP includono tutte le modifiche precedenti: modalità delle
spese e fondo cassa, eliminazione dei report con permessi e movimenti
collegati, permanenza nella stessa sezione quando l’admin cambia negozio.
