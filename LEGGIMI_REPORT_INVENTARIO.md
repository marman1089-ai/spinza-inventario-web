# Aggiornamento: report automatici ai conteggi e periodi liberi

## Nuove pagine
- `/inventario/conteggio?area=prodotti&store=spinza`: conteggio settimanale cucina.
- `/inventario/conteggio?area=bibite&store=spinza`: conteggio sala.
- `/inventario/report?area=prodotti&store=spinza`: report cucina filtrabile per date.
- `/inventario/report?area=bibite&store=spinza`: report sala filtrabile per date.

I link sono aggiunti alla parte alta dell'inventario. È necessario selezionare il negozio corretto.

## Comportamento
- Alla conferma di un **conteggio completo** viene registrata una fotografia delle quantità, aggiornato il magazzino e aperto il report, stampabile o salvabile in PDF dal browser.
- Da due conteggi in avanti, per ciascun prodotto con storico: uscite = quantità al precedente conteggio + arrivi confermati nei log - quantità al nuovo conteggio.
- Per la cucina, il rapporto è da usare ogni settimana. Per la sala i conteggi si possono registrare quotidianamente; nel report si possono scegliere date di inizio e fine liberamente.
- Report mese: default dal primo giorno del mese a oggi. Include **solo periodi interamente chiusi da due conteggi**; non inventa il consumo dei giorni senza conteggi.
- I report comprendono quantità, non costi in euro. Uscite non equivale necessariamente a prodotti venduti (potrebbero esserci scarti, trasferimenti, rettifiche).
- Arrivi: conteggiati solo dai log `ORDER_CARICATO_INVENTARIO` e `TRANSFER_CARICATO_INVENTARIO` con nome e categoria corrispondenti. Le fatture OCR non producono ancora un ingresso a magazzino. Arrivi manuali non classificati non sono automaticamente riconoscibili.

## Attenzioni
- **Il primo conteggio crea solo la base; il primo report significativo nasce dal secondo conteggio.**
- Conteggi con data inserita nel passato sono trattati come effettuati alla FINE della giornata selezionata. Non retrodatare un conteggio fatto oggi; gli arrivi successivi al conteggio nell'arco dello stesso giorno potrebbero altrimenti essere inclusi male.
- Non è ancora implementata una riconciliazione completa di ogni possibile movimento per acquisti, resi, trasferimenti e sprechi. Correggere/verificare manualmente se ci sono discrepanze.
- I prodotti con ID rinominato/sostituito fra conteggi potrebbero non avere confronto.
- Questa versione non sovrascrive vecchi conteggi e rifiuta conteggi dello stesso reparto/negozio registrati con la stessa data o antecedenti.
- I nuovi dati saranno creati nel DB collegato al sito, non nello ZIP. Fare backup del DB prima di pubblicare.
- Non sono stati effettuati test completi su Render/Supabase in produzione.
