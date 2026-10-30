# Controllo live e assistenza remota — guida pratica

## Cosa cambia per te e per i dipendenti

L'uso ordinario del gestionale rimane quello della versione precedente: stessi accessi, inventario, fatture, chiusure, spese e permessi. Nessun programma da installare per questa modalità browser. Si aggiungono una pagina admin e le richieste di assistenza visibili al dipendente.

Il tuo indirizzo, https://spinza-inventario-web.onrender.com/, è stato verificato in sola lettura: risponde correttamente via HTTPS e apre un contesto sicuro. Il sito online non è stato modificato durante il lavoro: le novità appariranno dopo aver aggiornato il progetto e completato il deploy su Render.

## 1. Controllo live senza condividere lo schermo

Apri **Admin → Controllo live**.

- Il pannello carica le nuove operazioni ogni **3 secondi**, quando è visibile.
- Le pagine del gestionale inviano un segnale di presenza ogni **10 secondi**. Dopo **35 secondi** senza segnali la presenza scade.
- Gli stati distinguono pagina attiva con interazioni recenti, pagina aperta ma inattiva, scheda in secondo piano, segnale recente dal server e persona non rilevata online.
- Vedi la pagina segnalata, Sala/Cucina quando disponibile, l'ultima richiesta recente e le richieste che il server sta ancora elaborando, per esempio un caricamento.
- Puoi filtrare negozio, persona e tipo di evento. Gli admin sono esclusi inizialmente; puoi includerli. Puoi mostrare anche gli offline.
- **Pausa lettura** ferma lo scorrimento del registro a schermo, ma continua a ricevere gli eventi. **Riprendi lettura** mostra quelli arrivati nel frattempo.
- Gli errori di rete vengono indicati chiaramente; il pannello riprova e recupera gli eventi successivi ai cursori già ricevuti, con deduplicazione.

Questo pannello non mostra digitazioni o bozze non inviate. Le richieste e i movimenti salvati sono fonti distinte: una richiesta può generare più righe di Log. Qui si visualizzano al massimo gli ultimi 500 eventi caricati; per lo storico completo restano le Fasce orarie, il dettaglio individuale e i CSV della versione precedente.

«Attivo» è un'indicazione basata sui segnali recenti, non una certificazione che qualcuno stia premendo un tasto in quell'istante. La pagina segnalata dal browser non conferma una modifica: i movimenti salvati nei Log sono la conferma disponibile. Una richiesta rimasta senza esito non viene dichiarata riuscita e scompare dall'elenco delle richieste in corso dopo 10 minuti.

## 2. Vedere lo schermo: primo consenso

1. Il dipendente deve essere collegato al gestionale e avere una sua pagina aperta.
2. Dalla sua scheda nel Controllo live premi **Richiedi schermo**.
3. Il dipendente vede il nome dell'admin che lo richiede e può premere **Rifiuta** oppure **Accetta e scegli cosa condividere**.
4. Il browser presenta il proprio selettore. Le scelte dipendono dal browser e dal dispositivo: una scheda, una finestra oppure l'intero schermo.
5. Dopo l'accettazione, il video compare nel tuo riquadro di assistenza.

Se condivide lo schermo intero, puoi vedere anche Google o altre applicazioni che rende visibili, ma **solo guardarle**. Se condivide una singola scheda, vedi soltanto quella. Il programma non avvia una cattura nascosta e non aggira il selettore del browser.

Sono esclusi l'audio e la registrazione video automatica. Il video viaggia tramite WebRTC: non viene caricato e conservato come filmato nel database del gestionale.

## 3. Mouse e tastiera: secondo consenso separato

Il controllo è disponibile solo quando il browser consente di verificare che la sorgente condivisa sia **la stessa scheda del gestionale** aperta dal dipendente. La condivisione dell'intero schermo o di una finestra generica rimane di sola visualizzazione.

1. Quando il video è collegato e la scheda è verificabile, premi **Richiedi controllo mouse e tastiera**.
2. Il dipendente vede una seconda richiesta, distinta dalla visione dello schermo, e può rifiutarla.
3. Solo dopo **Accetta controllo di questa scheda** puoi cliccare nel video, scorrere e usare i campi del gestionale.
4. Per scrivere, clicca prima il campo nel video e usa la tastiera. È disponibile anche il riquadro **Testo da inviare al campo selezionato**, utile su telefono o per testi più lunghi, con pulsanti Tab, Enter e Backspace.

Il controllo riguarda gli elementi della pagina web del gestionale. Non controlla il mouse di Windows, la barra degli indirizzi, Google, altre schede o applicazioni. I campi password e altri campi sensibili sono esclusi dai comandi; non sono previsti comandi di sistema o scorciatoie Ctrl/Alt/Meta. I selettori nativi del browser, i file da caricare e le eventuali conferme di sistema possono richiedere un intervento locale del dipendente.

Le azioni restano soggette ai permessi dell'account del dipendente: l'assistenza non gli attribuisce privilegi admin. Le operazioni effettivamente inviate vengono registrate normalmente con quell'account; il registro separato documenta chi ha richiesto l'assistenza e chi ha accettato il controllo. Non attribuisce automaticamente ogni singolo clic locale o remoto a una persona diversa.

Ogni nuova accettazione del controllo crea una nuova autorizzazione temporanea del canale: eventuali comandi appartenenti a un consenso precedente non vengono riutilizzati dopo una revoca.

## 4. Fermare o rifiutare è sempre possibile

- Il dipendente ha **Revoca controllo**, che mantiene la sola visione del video, e **Interrompi condivisione**, che ferma tutto.
- L'admin ha **Rilascia controllo e mantieni il video** e **Chiudi e interrompi**.
- Anche il comando di interruzione del browser ferma la condivisione.
- Se cambia la sorgente catturata e non è più verificabile come quella scheda, il controllo viene revocato.
- Ricaricare, chiudere o cambiare pagina con una navigazione completa interrompe questa sessione. Per continuare dalla nuova pagina occorre una nuova richiesta e accettazione. Il controllo di finestre e moduli nella pagina corrente non richiede una nuova condivisione.
- Le richieste non accettate scadono dopo due minuti. Le sessioni senza segnali dei partecipanti scadono; la durata massima di una sessione è un'ora. Non è previsto accesso permanente o incustodito.

Richieste, accettazioni, rifiuti, revoche e interruzioni esplicite sono riportati nel registro di controllo. Il contenuto delle digitazioni di assistenza non viene salvato come registro dei tasti. La segnalazione tecnica WebRTC viene eliminata alla chiusura o alla scadenza rilevata; non è il video.

## 5. Cosa serve: HTTPS, browser e rete

**HTTPS:** il tuo collegamento Render lo usa già. Questo requisito è soddisfatto per il collegamento pubblico verificato. Per la condivisione serve un contesto sicuro del browser.

**Browser:** il programma verifica le funzioni disponibili. Il collaudo è stato eseguito in Microsoft Edge su PC. La condivisione può essere assente o limitata su alcuni telefoni/browser: in quel caso compare un messaggio, senza aggirare i permessi. Anche quando il video è possibile, il controllo resta disabilitato se non si può verificare la scheda condivisa.

La verifica della scheda usa la funzione Capture Handle quando disponibile. Riferimento tecnico: https://developer.chrome.com/docs/web-platform/capture-handle . Per l'uso quotidiano non occorre conoscere questa funzione: serve soltanto a evitare che il controllo sia applicato a una sorgente diversa da quella autorizzata.

**TURN:** è un ponte di rete per il video, necessario su alcune combinazioni di reti aziendali, firewall o connessioni mobili. È già presente il supporto alla configurazione, ma non è stato attivato o acquistato un servizio TURN. Il collaudo locale non può garantire il collegamento tra tutte le vostre reti reali.

La configurazione predefinita usa un servizio STUN per tentare il collegamento diretto. Se il video non si collega tra la tua rete e quella del dipendente, chi gestisce il sito deve aggiungere un server TURN valido tramite la variabile ambiente **CONTROL_ICE_SERVERS** su Render e riavviare il servizio. Ai dipendenti non è richiesto inserire questi parametri ogni volta.

Esempio della struttura JSON, da sostituire con i dati REALI forniti dal proprio servizio TURN:

```json
[
  {"urls": "stun:stun.l.google.com:19302"},
  {
    "urls": [
      "turn:turn.example.com:3478?transport=udp",
      "turns:turn.example.com:5349"
    ],
    "username": "UTENTE_FORNITO_DAL_SERVIZIO",
    "credential": "PASSWORD_FORNITA_DAL_SERVIZIO"
  }
]
```

`turn.example.com` è solo un segnaposto, non un server pronto all'uso. Non serve modificare il codice per inserire una configurazione valida. L'eventuale configurazione è tecnica e occasionale; il lavoro ordinario di te e dei dipendenti rimane quello descritto sopra.

## 6. Aggiornamento, dati e verifiche

Lo ZIP contiene il progetto completo aggiornato. Le nuove tabelle di controllo vengono create automaticamente al primo avvio. Database e immagini presenti nel pacchetto precedente vengono conservati identici; rimangono loghi, filtri, permessi giornalieri sui documenti e correzione dell'inventario PC.

La versione online non è stata modificata o distribuita durante il lavoro. Per vedere il nuovo menu occorre aggiornare i file del progetto usato da Render e completare il consueto deploy.

Verifiche effettuate:

- Sei nuovi test Python su presenze, permessi, cursori, richiesta in corso, scadenze, accesso ai soli partecipanti, primo e secondo consenso, revoca e pulizia della segnalazione.
- Test JavaScript del segnale di presenza e della rilevazione precedente.
- Due browser autenticati separati con collegamento WebRTC reale e un video sintetico di collaudo: nessuno schermo reale è stato acquisito nei test. La scelta del browser è stata simulata per verificare il flusso senza acquisire il desktop del computer.
- Verificati nessuna acquisizione prima dell'accettazione, seconda accettazione prima del controllo, testo inviato a un campo del gestionale, blocco dei campi password, revoca, stop e schermo intero di sola visualizzazione.
- Verificati nel browser il registro live, la ricezione di nuove quantità salvate, pausa/ripresa senza perdere eventi, filtri, riconnessione, deduplicazione e layout mobile.
- Suite generale: 70 test, 65 superati. Restano le stesse cinque segnalazioni pregresse: quattro riguardano i test della digitalizzazione già disattivata, una richiede psycopg per il test del pool PostgreSQL nell'ambiente di prova. Nessuna nuova regressione rilevata. Non è stato provato un database PostgreSQL di produzione.

La prova finale sulla vostra connessione reale dovrà verificare il selettore schermo del vostro browser e il passaggio del video tra le due reti; è quella che stabilisce se serve un TURN. Le modifiche operative del gestionale non sono state riscritte: l'integrazione è dedicata al controllo live e all'assistenza autorizzata.
