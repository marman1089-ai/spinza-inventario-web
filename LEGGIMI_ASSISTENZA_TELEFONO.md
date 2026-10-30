# Avvio rapido dell'assistenza dal telefono al PC

Questa versione corregge l'attesa iniziale del pannello admin, mantenendo la copia fedele nelle proporzioni del telefono.

- L'admin prepara il canale rapido subito dopo la richiesta, prima che il dipendente accetti. Nessun contenuto viene condiviso prima del consenso.
- Il collegamento video tradizionale viene preparato soltanto se il dipendente sceglie davvero la condivisione schermo, senza ritardare la modalità del gestionale.
- L'invio di riserva non blocca più i fotogrammi: quando il canale rapido è pronto, la pagina parte anche se una richiesta HTTP precedente è ancora in attesa.
- I segnali di presenza del canale rapido sono indipendenti dalle richieste HTTP lente. Gli aggiornamenti superati vengono scartati, senza accumulare una coda.
- Le operazioni del database dei comandi asincroni vengono eseguite fuori dal ciclo di invio dei fotogrammi.
- Prima dell'arrivo della pagina compare un messaggio di attesa, al posto del riquadro video nero.

Nel test locale mirato la prima copia è comparsa in **0,50 secondi dall'accettazione**, nonostante risposte HTTP artificialmente ritardate di **20 secondi**. La prova prolungata ha mantenuto circa 5,9–6 aggiornamenti al secondo per oltre un minuto. Scrittura, revoca e stop sono stati inoltre verificati con conferma del consenso ritardata di 2 secondi e comandi ritardati di 400 millisecondi. Questi tempi sono risultati di collaudo locale, non una garanzia sulla rete reale di Marco o sull'hosting.

**Per applicarla:** carica questo ZIP completo sul progetto Render e completa il deploy. Poi chiudi e riapri le pagine su telefono e PC admin. I file hanno un nuovo identificativo per evitare la vecchia cache. Il sito online non è stato modificato durante questo lavoro.

## Funzionamento della copia del telefono

## Novità
La vista precedente con testi ricostruiti è stata sostituita. Ora la copia mantiene la struttura della pagina, gli stili del gestionale, i loghi, le immagini statiche, i campi compilati e i menu aperti. Usa la stessa larghezza e altezza della pagina sul telefono (per esempio 390 × 844), centrata e adattata allo spazio disponibile sullo schermo admin senza cambiare l'impaginazione mobile.

Il collegamento rapido invia fino a circa 6 aggiornamenti al secondo, con un margine sull'obiettivo di 5. Sotto la copia sono visibili le dimensioni originali e la frequenza effettivamente ricevuta. Durante il collaudo locale sono stati misurati 5,94 aggiornamenti al secondo con contenuto che cambiava continuamente. La frequenza reale dipende da telefono, pagina, rete e carico del servizio; non è una garanzia di 5 FPS su qualunque connessione.

## Aggiornamento su Render
1. Aggiorna il progetto con questo ZIP completo e completa il deploy. Conserva DATABASE_URL e SESSION_SECRET esistenti. Non importare sul sito i database contenuti nel pacchetto: sono rimasti invariati rispetto al tuo archivio.
2. Il comando di avvio previsto è `uvicorn app.main:app --workers 1 --host 0.0.0.0 --port $PORT`. Il file render.yaml incluso lo imposta; anche run.py usa un solo processo. La configurazione del sito deve usare una sola istanza del servizio: il collegamento rapido connette i due partecipanti nello stesso processo. Più istanze richiedono un relay condiviso aggiuntivo.
3. La libreria WebSocket è già inclusa nelle dipendenze uvicorn[standard]. Non occorrono chiavi esterne o TURN per questa modalità. Render deve consentire il normale collegamento WebSocket HTTPS.
4. Chiudi le precedenti pagine aperte e riapri il gestionale su admin e dipendente, così entrambi caricano la nuova versione. Gli indirizzi dei file sono stati aggiornati per evitare la vecchia cache.

## Uso
Admin: Controllo live → Richiedi schermo.
Dipendente: Accetta e condividi il gestionale.
Admin: la copia appare subito sotto lo stato della connessione, nelle proporzioni del telefono. Il contatore mostra dimensioni e aggiornamenti/s. Puoi osservare i cambiamenti fatti dal dipendente.
Per agire: Richiedi controllo mouse e tastiera → seconda accettazione del dipendente. Clicca un campo nella copia e usa la tastiera o Invia testo. La rotella, o un gesto verticale su un admin touch, scorre la pagina del dipendente dopo l'autorizzazione.
Revoca controllo e Interrompi condivisione rimangono disponibili al dipendente.

## Cosa viene copiato
È una replica della pagina del gestionale, non una cattura del sistema operativo. Comprende impaginazione, colori, stili, immagini statiche, valori dei campi, selezioni, menu della pagina e posizione di scorrimento. I grafici canvas vengono riprodotti come immagini quando il browser ne consente la lettura.
Password, campi nascosti, campi file e sezioni data-assist-private sono esclusi. I popup nativi del sistema (tastiera del telefono, selettore file, lista nativa di un select), video, iframe/PDF incorporati, immagini esterne protette e altre applicazioni non sono riprodotti. Non vengono eseguiti gli script del dipendente nella copia admin; i comandi agiscono soltanto sulla pagina sorgente dopo il secondo consenso.

## Connessione e interruzioni
La pagina del dipendente deve rimanere aperta in primo piano. Un cambio pagina che ricarica il gestionale termina la sessione e richiede una nuova richiesta.
Se il collegamento rapido non è disponibile, viene indicata la modalità di riserva più lenta: non viene presentata come una connessione a 5 FPS. Il sistema ritenta il collegamento rapido automaticamente. In tal caso verificare WebSocket, connessione e singola istanza/processo del servizio.
Il ponte scarta gli aggiornamenti ormai superati anziché accumulare ritardo.

## Dati
Gli aggiornamenti rapidi transitano in memoria attraverso il server e non vengono salvati come un filmato o come una cronologia di pagine. La modalità di riserva conserva soltanto l'ultima copia e una piccola coda di comandi, cancellati allo stop o alla successiva verifica di scadenza. I registri conservano le accettazioni e revoche, senza i contenuti condivisi. Entrambi i collegamenti richiedono una sessione autenticata e l'appartenenza all'assistenza; i comandi richiedono il secondo consenso e un codice nuovo a ogni approvazione.

## Collaudo
- 29 verifiche locali superate sui percorsi di assistenza: autorizzazioni, doppio consenso, estranei esclusi, origine del collegamento, revoca, rinnovo del consenso, scadenza e pulizia delle connessioni.
- Due browser locali, dipendente simulato 390 × 844 senza cattura schermo né WebRTC: copia ricevuta a 5,94 aggiornamenti/s, valori dei campi identici, colori/font/misure/posizioni verificati, contenuti riservati esclusi, scrittura remota, revoca e stop.
- Confronto visivo delle schermate originale/copia eseguito. Sono possibili lievi differenze di rendering tra browser diversi.
- Nessun accesso al database di produzione e nessun deploy effettuato. Rimane necessaria la prova sul telefono reale dopo l'aggiornamento.
