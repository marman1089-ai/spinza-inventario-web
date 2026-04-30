# Modifica: riepilogo mesi salvati + eliminazione mese

Modifiche aggiunte nella Home gestionale (`/gestionale`):

- Nuovo riquadro **Mesi salvati nel gestionale**.
- Ogni mese mostra:
  - totale entrate;
  - totale uscite;
  - margine;
  - numero movimenti;
  - negozi coinvolti.
- Accanto a ogni mese c'è il pulsante **Elimina mese**.
- Il pulsante elimina insieme:
  - tutte le righe di `cash_entries` del mese;
  - tutte le righe di `cash_expenses` del mese.
- Prima della cancellazione appare una conferma nel browser.
- Dopo la cancellazione compare un messaggio verde con quante entrate e quante uscite sono state rimosse.
- La cancellazione rispetta lo scope del negozio selezionato: se stai guardando un singolo negozio elimina solo quello; se sei in vista ALL elimina il mese su tutti i negozi.
- Viene scritto anche un log dell'operazione.

File modificati:

- `app/main.py`
- `app/templates/gestionale_home.html`
