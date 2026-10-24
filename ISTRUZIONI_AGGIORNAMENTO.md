# Gestionale – Correzione OCR gratuito e menu unificato

## Cosa è stato corretto
- Rimosso il collegamento principale al vecchio flusso di lettura AI a pagamento che impediva la trascrizione visibile nell’archivio.
- Dopo il caricamento di fatture, chiusure o spese secondarie, il sito apre il pannello OCR gratuito ed esegue la prima lettura nel browser.
- Dagli elenchi è possibile aprire lo stesso pannello per i vecchi documenti, correggere righe ed importi e confermare.
- Nel menu compare **Strumenti digitali**, senza emoji, con tutte le nuove voci riunite.
- Inventario: restano il conteggio Sala, il conteggio Cucina e i report per periodo precedentemente implementati.

## Limiti importanti
- OCR Tesseract.js richiede browser aperto e rete per scaricare le librerie. Non è una IA semantica, soprattutto fatture scritte a mano o tabelle complesse richiedono verifica.
- La pagina /digitalizza/archivio/tutti avvia la lavorazione dei documenti non ancora elaborati; mantenerla aperta.
- Non cancellare il database PostgreSQL o documenti. Fare backup.
- Non sono state effettuate prove integrate con Supabase e Render reali.
- Gli acquisti digitalizzati non aggiungono merce automaticamente all’inventario per evitare doppi carichi.
