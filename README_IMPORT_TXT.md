Import TXT/CSV incassi

Cosa fa:
- aggiunge nella pagina /gestionale/incassi una sezione per importare un file TXT o CSV
- legge blocchi con date tipo 01/12/25
- riconosce Pos, Cash, Deliveroo, Glovo, Just Eat
- può importare anche le uscite trovate nel file
- può sovrascrivere i giorni già presenti per evitare duplicati

Come usarlo:
1. Apri Gestione -> Incassi
2. Vai su "Importa incassi da file"
3. Carica il file oppure incolla il testo
4. Lascia attivo "Sovrascrivi gli stessi giorni già presenti" se vuoi aggiornare quei giorni senza duplicati
5. Attiva "Importa anche le uscite trovate nel file" solo se vuoi salvare anche le spese lette nel testo
6. Premi "Importa file"

File modificati:
- app/main.py
- app/templates/cashflow_entries.html
