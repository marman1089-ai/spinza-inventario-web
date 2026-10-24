# OCR gratuito per fatture, chiusure e spese secondarie

- Nessuna chiave OpenAI e nessun credito API richiesti.
- Visita Fatture / Chiusure / Spese secondarie e clicca Digitalizza nella riga del documento, anche vecchio.
- Clicca **Leggi tutte le pagine con OCR gratuito**. L’OCR viene svolto nel browser con Tesseract.js e PDF.js, scaricati da jsDelivr al primo uso. Serve una connessione Internet per scaricare le librerie; non c’è costo API. Le immagini del documento non vengono trasmesse a OpenAI. Il testo estratto viene inviato soltanto al tuo server gestionale per il salvataggio.
- Il parser è euristico e può sbagliare importi, quantità, prodotto o intestazioni: verifica tutte le righe, elimina quelle errate, aggiungine manualmente e correggi i valori, poi clicca Conferma e salva.
- Puoi anche usare **Estrai dati dalla foto/PDF** per sfruttare testo PDF e Tesseract del server (solo se già installato).
- La lettura è individuale per documento, non è una conversione batch automatica.
- I file originali e le altre tabelle non vengono sovrascritti; i dati digitalizzati sono salvati separatamente.
- Se la rete blocca jsDelivr, il pannello manuale continua a funzionare.
