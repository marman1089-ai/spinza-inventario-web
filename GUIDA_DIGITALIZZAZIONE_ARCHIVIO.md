# Digitalizzazione gratuita automatica dell’archivio

Dopo avere pubblicato questa versione su Render, aprire Fatture, Chiusure o Spese secondarie e cliccare **Digitalizza automaticamente tutti i documenti già caricati**. All’apertura della pagina l’elaborazione parte in automatico, senza chiave API.

Il browser scarica Tesseract.js e PDF.js da CDN e analizza in sequenza i documenti di tutte e tre le sezioni. In visione globale solo l’amministratore può elaborare documenti di negozi diversi. È necessario tenere aperta la scheda durante l’analisi.

I documenti già digitalizzati (anche non revisionati) vengono saltati per non sovrascrivere correzioni. Ogni nuovo risultato è salvato nel database come **da verificare**. Dalle singole sezioni è possibile aprire il documento, correggere ogni campo e confermare. Se la lettura di un documento fallisce, il processo continua: l’errore compare nell’elenco. Interrompendo l’elaborazione, è possibile riprendere dai documenti ancora non digitalizzati.

**Importante:** non vengono eliminati o modificati i PDF originali. Non vengono addebitati costi API, ma rimangono eventuali costi di hosting/connessione. Il riconoscimento è euristico e può sbagliare righe, aliquote e prezzi: controllare i documenti prima di usare i valori in contabilità. Fare un backup del database prima di aggiornare il sito.
