# Fix Report Vendite - gruppi modificabili

Aggiornamento incluso:
- Le singole voci importate nel report vendite ora possono essere spostate correttamente dentro un gruppo principale.
- Lo spostamento di una voce singola viene ricordato per i prossimi import dello stesso negozio.
- I gruppi principali prestabiliti ora hanno pulsanti diretti per rinominare e cancellare.
- Cancellando un gruppo principale, viene rimosso davvero dai mesi salvati e può essere ricreato subito.
- Cancellando una voce singola, non viene più bloccata per sempre: si può ricreare o reimportare.

File principali modificati:
- app/main.py
- app/templates/sales_report.html
