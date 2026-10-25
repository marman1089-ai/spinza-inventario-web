# Cucina · controllo fatture settimanale

- Aprire **Inventario → Fatture cucina · settimanale** (disponibile anche ai dipendenti autenticati).
- Selezionare la settimana e una fattura nell'elenco a sinistra.
- Il PDF multipagina rimane visibile al centro, con scorrimento autonomo; a destra aggiungere prodotti e quantità consegnate.
- **Salva bozza** permette di riprendere in seguito. **Completa e avanti** colora di verde la fattura e passa alla successiva.
- **Report settimanale** somma le quantità consegnate, per prodotto, usando solo le fatture completate; permette la stampa o il salvataggio in PDF.
- Le quantità delle consegne vengono registrate a parte, senza sovrascrivere le giacenze: il conteggio fisico dell'inventario resta indipendente.
- Fatture, chiusure e spese secondarie: si possono selezionare più immagini o PDF in una volta sola oppure usare '+1'; sono riuniti in un unico PDF con più pagine, come già supportato dal backend.

## Sicurezza e aggiornamento

Le nuove registrazioni sono in una tabella aggiuntiva `kitchen_receipt_reviews`, creata al primo accesso alla funzione. Non viene cancellata o modificata la tabella fatture esistente. Ogni dipendente vede solo i documenti del proprio negozio. L'admin, nel selettore "Tutti", lavora sul negozio associato al proprio utente: per un altro negozio lo deve selezionare esplicitamente.

**Prima di pubblicare su Render/Supabase, fare il backup del database.** La cartella contiene anche il database locale e documenti del progetto: non sostituire il database in produzione con il file dello ZIP.
