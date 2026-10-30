# Assistenza del gestionale anche da telefono

## Come usarla
1. Carica questa versione del progetto su Render con la normale procedura di aggiornamento e completa il deploy. Non sovrascrivere il database online con i database di esempio contenuti nello ZIP.
2. Admin e dipendente ricaricano il gestionale. Se compare ancora il vecchio pulsante, chiudere e riaprire la pagina.
3. Admin: apri Controllo live e premi Richiedi schermo sul dipendente.
4. Dipendente: premi **Accetta e condividi il gestionale**. Questa è la scelta principale, utilizzabile senza condivisione schermo del sistema operativo e senza WebRTC.
5. L'admin vede una riproduzione della pagina visibile con testi, campi e pulsanti. Per agire preme **Richiedi controllo mouse e tastiera**; il dipendente accetta la seconda richiesta.
6. L'admin seleziona un campo nella pagina condivisa e usa la tastiera oppure il riquadro Invia testo. Su un elenco a discesa usa le frecce dopo averlo selezionato. Può cliccare pulsanti, selezioni e scorrere con la rotella.
7. Il dipendente può sempre premere Revoca controllo o Interrompi condivisione.

## Collegamento
Il ponte usa le normali richieste HTTPS allo stesso gestionale. Per questa modalità non servono TURN, abbonamenti di assistenza o chiavi esterne. Richiede una connessione funzionante, un browser moderno con JavaScript e la pagina del gestionale aperta in primo piano. Il sito online non è stato modificato da questo lavoro: occorre il deploy dello ZIP.

La visualizzazione si aggiorna circa ogni 1,6 secondi; i comandi vengono ricevuti nel ciclo di controllo di circa 1,2 secondi. Il ritardo reale dipende dalla rete e dal server. La modalità video precedente resta disponibile come scelta secondaria sui dispositivi compatibili.

## Limiti visibili
- È una riproduzione semplificata dei contenuti visibili del gestionale: foto, canvas, PDF incorporati e video sono indicati come contenuti grafici, senza riprodurli. Password, campi nascosti e caricamento file sono esclusi. Le sezioni marcate data-assist-private sono escluse.
- La selezione di file, i messaggi nativi del browser e le altre applicazioni rimangono al dipendente.
- Un cambio pagina con ricaricamento termina la sessione: l'admin invia una nuova richiesta. Questo comportamento conserva il consenso esplicito per ogni nuova pagina.
- Non è stata verificata ogni marca e versione di telefono: il collaudo ha simulato un dispositivo mobile senza API di cattura schermo né WebRTC. È necessaria una prova finale sul telefono reale dopo il deploy.

## Dati e consensi
Le informazioni della pagina attraversano il server del gestionale. Il database conserva soltanto l'ultima rappresentazione e una piccola coda di comandi, senza uno storico delle schermate. Questi dati vengono eliminati allo stop o alla successiva verifica della scadenza. I consensi restano nei registri attività, senza riportare il contenuto della pagina o i testi inviati. Le sessioni cessano dopo assenza dei segnali per 45 secondi, o dopo un'ora. Il secondo consenso usa un codice nuovo a ogni approvazione; la revoca svuota i comandi in attesa.

## Verifica eseguita
- Test delle autorizzazioni, doppio consenso, isolamento tra utenti, consegna dei comandi, revoca, nuova approvazione e scadenza su database isolati.
- Due browser locali, dipendente con dimensioni e interazione mobile e API di cattura schermo/WebRTC disabilitate: accettazione, visualizzazione della pagina, esclusione password e contenuti nascosti, digitazione remota dopo il secondo consenso, revoca e stop.
- Nessuna lettura o modifica del database di produzione.
