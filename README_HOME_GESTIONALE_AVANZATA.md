# Modifiche home gestionale - tabella e grafici avanzati

Modifiche aggiunte nella home `/gestionale`:

- Tabella giorno per giorno per il periodo visualizzato:
  - Giorno
  - Entrate
  - Uscite
  - Bilancio
  - Totale finale in fondo alla tabella
  - Se il periodo è un mese, vengono mostrati automaticamente 28/29/30/31 giorni in base al mese scelto.

- Grafici più leggibili:
  - Linee soglia in euro dentro i grafici.
  - Valore euro sopra le barre più importanti.
  - Tooltip con dettaglio al passaggio del mouse.
  - Click su barre e righe della tabella per mostrare il dettaglio sotto il grafico/tabella.

- Dettaglio entrate per giorno:
  - POS
  - Contanti/Cash
  - Deliveroo
  - Glovo
  - Just Eat
  - altri metodi salvati nel gestionale

- Dettaglio uscite raggruppato in modo intelligente:
  - Stipendi
  - Materie prime
  - Manutenzione
  - Professionisti
  - Bollette e utenze
  - Affitti e abbonamenti
  - Packaging
  - Servizi finanziari
  - Marketing
  - Delivery e logistica
  - Tasse
  - Spese secondarie

File modificati:

- `app/main.py`
- `app/templates/gestionale_home.html`
