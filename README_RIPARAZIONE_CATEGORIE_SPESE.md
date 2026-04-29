# Riparazione categorie spese

Questa versione corregge il problema in cui molte uscite finivano in **Movimenti cassa**.

## Cosa è stato cambiato

- La categoria vecchia non viene più usata come prova per ricategorizzare una spesa.
- Le note del giorno importate dal TXT vengono pulite prima dell'analisi.
- Righe come `Fondo cassa 5€`, `Pos`, `Cash`, `Deliveroo`, `Glovo`, `Total` non influenzano più la categoria delle spese.
- `Movimenti cassa` viene assegnato solo a voci davvero di cassa, tipo `Coins`, `monete`, `cambio cassa`, `resto`, `fondo cassa`.
- Le vecchie righe finite per errore in `Movimenti cassa` vengono ricontrollate all'avvio e anche dal pulsante **Ricalcola categorie già inserite**.

## Esempi

- `Metro cure` -> Materie prime
- `Forno Spinza` -> Materie prime
- `Macellaio` -> Materie prime
- `Nexi commissione` -> Servizi finanziari
- `Qonto subscription` -> Servizi finanziari
- `Lorenzo stipendio febbraio` -> Stipendi
- `Extra sala` -> Stipendi
- `Amazon Reburger` -> Manutenzione e attrezzature
- `Coins` -> Movimenti cassa
