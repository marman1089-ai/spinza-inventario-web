# Aggiornamento: Negozi archiviati e Panoramica totale

Aggiunte due nuove sezioni nel menu Gestionale:

- **Negozi archiviati** (`/gestionale/archiviati`)
  - crea negozi storici non più attivi;
  - entra nel singolo negozio archiviato;
  - aggiunge entrate e uscite manualmente;
  - incolla/importa testo da note con controllo prima del salvataggio;
  - salva i movimenti nelle stesse tabelle contabili già usate da incassi e uscite.

- **Panoramica totale** (`/gestionale/panoramica-totale`)
  - mostra entrate totali, spese totali e bilancio totale di sempre;
  - separa negozi attivi e negozi archiviati;
  - divide i dati per anno;
  - divide i dati per posto/negozio;
  - permette filtri per anno e per negozio.

Nuova tabella database:

- `archived_stores`

I dati economici dei negozi archiviati vengono salvati in:

- `cash_entries`
- `cash_expenses`

con uno `store_key` dedicato, così entrano nei totali generali senza confondersi con i negozi attivi.
