# Aggiornamento: torta spese cliccabile + controllo import prima del salvataggio

Modifiche principali:

1. Home gestionale
- Il grafico a torta delle spese ora è cliccabile.
- Cliccando la torta o una categoria nella legenda compare il dettaglio delle spese incluse in quella categoria.
- Ogni riga di spesa ha il pulsante/menu **Sposta** per correggere la categoria sbagliata direttamente dalla Home.
- Lo spostamento aggiorna la categoria della spesa nel database, quindi resta corretto anche dopo refresh e nei mesi successivi.

2. Import incassi/spese da testo
- Il pulsante non salva più subito: prima mostra un riepilogo di controllo.
- Nel riepilogo vengono mostrati:
  - giorni letti;
  - totale entrate estratte;
  - totale uscite estratte;
  - metodo di pagamento rilevato per ogni giorno;
  - prime uscite lette e categoria automatica prevista;
  - giorni vuoti del mese, per capire se mancano dati o se il negozio era chiuso.
- Solo premendo **Conferma e salva tutto** i dati entrano davvero nel database.

3. Sicurezza contabile
- Se è attiva l'opzione "Sovrascrivi gli stessi giorni già presenti", la conferma sostituisce i dati dei giorni trovati nel testo, evitando doppioni.
- I giorni vuoti segnalati nel riepilogo non vengono creati automaticamente: servono solo come controllo prima di confermare.
