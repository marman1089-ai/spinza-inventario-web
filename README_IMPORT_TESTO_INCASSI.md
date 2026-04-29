# Import testo incassi e spese

Modifica aggiunta nella pagina **Gestione → Incassi**.

## Cosa è stato aggiunto

- Sezione dedicata **"Importa incassi e spese da testo"**.
- Puoi caricare un file `.txt` / `.csv` oppure incollare direttamente un testo scritto/incollato nella textarea.
- Il sistema estrae gli incassi da righe come:
  - `POS 249,50€`
  - `Cash 55€`
  - `Contanti 55€`
  - `Deliveroo 80€`
  - `Glovo 40€`
  - `Just Eat 33€`
  - `Satispay 20€`
- Le righe con importo non riconosciute come metodo di incasso vengono trattate come spese, se è attiva la spunta **"Importa anche le uscite trovate nel testo"**.
- Può leggere date complete tipo `01/04/26`, `01-04-2026`, `2026-04-01`.
- Può leggere anche blocchi mensili tipo `SPINZA APRILE 2026` seguiti da giorni `1`, `2`, `3`.
- Riconosce il negozio dal testo se trova `SPINZA`, `CAMALDOLI` o `PALAZZUOLO`; altrimenti usa il negozio selezionato.
- Mantiene compatibilità anche con il vecchio endpoint `/gestionale/incassi/importa-file`.

## Esempio testo

```txt
SPINZA APRILE 2026
01/04/26
POS 249,50€
Cash 55€
Deliveroo 80€
Metro 125€
Nexi commissione 18,50€

2
Glovo 40€
Just Eat 33€
Forno Spinza 20€
```

Con la spunta spese attiva, `Metro`, `Nexi commissione` e `Forno Spinza` vengono salvate nelle uscite.

## File modificati

- `app/main.py`
- `app/templates/cashflow_entries.html`
