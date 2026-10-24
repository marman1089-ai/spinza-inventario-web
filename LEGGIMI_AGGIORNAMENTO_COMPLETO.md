# Gestionale – digitalizzazione documenti e inventario Sala/Cucina

## Inclusi in questa versione
- OCR gratuito nel browser (Tesseract.js + PDF.js), anche per documenti già caricati, tramite pagina `/digitalizza/archivio/tutti`. Avvia automaticamente la coda all'apertura della pagina: è necessario tenere aperta la scheda.
- Risultati salvati come **da verificare**; pannello manuale per modificare dati, aggiungere/eliminare prodotti, assegnare reparto e categoria, correggere imponibile, IVA e metodi di pagamento.
- Campi aggiuntivi per chiusure (contanti, POS, ticket, sconti, annullamenti, numero documenti), fatture (prodotti, quantità, unità, costo, IVA, destinatario) e spese secondarie (esercente, acquisti, pagamento, resto).
- Sala: inventario esistente con movimenti e log giornalieri.
- Cucina: conteggio periodico settimanale, con confronto tra il conteggio precedente, gli arrivi riconosciuti dagli ordini confermati e la quantità attuale. Il primo conteggio crea la base.

## Limiti attuali importanti
- Il riconoscimento **non interpreta perfettamente tutte le fatture**: l'OCR gratuito genera testo e una proposta di righe. Campi come aliquote, colonne complesse, date, importi e grafia manuale potrebbero dover essere compilati a mano.
- I campi estesi vengono **salvati nell'archivio digitale** ma non generano automaticamente entrate di magazzino o registrazioni contabili definitive. Per gli arrivi della cucina valgono per ora gli ordini confermati collegati all'articolo, non le fatture OCR. La riconciliazione dei costi con le spese rimane manuale.
- Lo stato da verificare rimane separato dalla conferma umana; le righe non corrette non sono attendibili.
- I log preesistenti dell'inventario Sala rimangono; non è ancora disponibile uno storico dettagliato campo-per-campo delle correzioni OCR.
- Evitare di modificare documenti già verificati mediante nuova estrazione, perché sovrascrive i dati: la pagina mostra un avviso prima di farlo.
- Copia di sicurezza del database di **produzione** obbligatoria prima del deploy. Lo ZIP non contiene il database live.
- Non eseguiti test end-to-end sull'hosting e sugli archivi di produzione.

## Controlli contabili da effettuare manualmente
- Chiusure: contanti + POS + ticket + eventuali altri metodi = totale vendite, solo se le categorie sono effettivamente esaustive e non sovrapposte; imponibile + IVA = totale, tenendo conto di arrotondamenti e correzioni.
- Fatture e spese: confrontare somma righe e totale documento, con eventuali sconti, trasporto e IVA.
- Magazzino cucina: uscito stimato = giacenza precedente + arrivi - giacenza rilevata; non equivale automaticamente al consumo venduto.

## Avvio
Usare la configurazione Render esistente e le variabili di ambiente del progetto. Non serve `OPENAI_API_KEY`. Le librerie OCR caricate da CDN richiedono Internet lato browser. Verificare autorizzazioni, dati esistenti, menu e conteggi prima di utilizzare in produzione.
