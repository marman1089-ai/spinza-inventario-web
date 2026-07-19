# Aggiornamento negozi archiviati – 19/07/2026

- Aggiunto pulsante **Elimina** su ogni negozio archiviato.
- La cancellazione del negozio rimuove soltanto le entrate e le uscite associate a quel negozio.
- Aggiunta sezione **Gestione mesi** dentro ogni negozio, con eliminazione selettiva del singolo mese.
- Aggiunte conferme di sicurezza animate con riepilogo dei dati che verranno rimossi.
- Aggiunta ricerca istantanea dei negozi archiviati.
- Aggiunte animazioni, contatori, feedback visivo e blocco dei doppi invii.
- Corretto il caricamento che poteva restare visibile dopo l'annullamento di una conferma.
- Aggiunti controlli sulle date e sui nomi duplicati.
- Migliorata accessibilità con riduzione automatica delle animazioni quando richiesta dal sistema.
- Reso l'avvio locale più robusto: i driver PostgreSQL e img2pdf vengono richiesti solo quando le relative funzioni sono utilizzate.
- Ripuliti i falsi errori di migrazione SQLite dai log di avvio.
