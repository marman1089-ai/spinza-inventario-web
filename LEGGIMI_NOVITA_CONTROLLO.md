# Novità del controllo: accessi, fasce orarie e registro delle operazioni

Questa guida aggiorna le precedenti note sulle fasce orarie. Le modifiche di questa versione riguardano il controllo; le funzioni operative del gestionale restano quelle della versione precedente.

## Dove andare e come usarlo

1. Accedi come amministratore e apri **Fasce orarie** dal pannello admin.
2. Scegli giorno e negozio, oppure tutti i negozi.
3. Passa sopra un indicatore del grafico: il riepilogo mostra orari precisi, accessi, richieste, movimenti e problemi di quella finestra.
4. **Clicca la fascia** per aprire il registro della persona in quell'intervallo. Sul telefono basta toccarla.
5. Usa **Registro del giorno**, accanto alla persona, per vedere tutta la giornata senza cercare le singole fasce.
6. Nel dettaglio puoi caricare gli eventi successivi, 100 alla volta, ed esportare l'intero registro selezionato in CSV. L'esportazione comprende anche gli eventi non ancora caricati a schermo.

## Accessi brevi sempre rappresentati

Prima il segnale aspettava la prima interazione. Inoltre un ingresso con inizio e fine coincidenti non produceva una barra visibile. Ora ci sono due rilevazioni complementari:

- **Registro sul server:** salva il login riuscito, le aperture delle pagine autenticate, le richieste di operazione e il logout. Non deve aspettare un clic nella pagina o il segnale periodico del browser. Un accesso brevissimo produce quindi un evento anche senza una durata misurabile.
- **Segnale del browser:** parte all'ingresso nelle pagine del gestionale e continua ogni 15 secondi quando la pagina è visibile, la finestra attiva e ci sono interazioni recenti. La chiusura o il cambio finestra inviano la pausa; un utilizzo di pochi secondi può così chiudersi con una durata breve. Se il segnale finale non arriva, resta comunque l'evento salvato dal server.

La rilevazione riguarda anche fatture, chiusure, spese secondarie, ordini e altre pagine autenticate, oltre all'inventario. File statici, segnali periodici e consultazione del pannello Fasce orarie sono esclusi dal registro delle richieste, per evitare che il monitoraggio generi da solo una lunga lista di attività.

## Come leggere il grafico

- **Verde:** finestra di attività rilevata.
- **Giallo:** ingresso o evento istantaneo. Rimane visibile anche con durata zero.
- **Bordo rosso:** la finestra comprende almeno una richiesta con errore o accesso negato.
- Gli indicatori ravvicinati si distribuiscono su più righe per restare cliccabili, anziché coprirsi.
- Lo **zoom 2×, 4× o 8×** consente di distinguere meglio ingressi vicini. Il grafico scorre orizzontalmente anche sul telefono.

Per rendere la consultazione pratica, segnali separati da non più di 90 secondi formano una finestra unica. Questa è una finestra di consultazione: **gli spazi tra i segnali non vengono aggiunti al tempo misurato**. Non vengono inventati minuti per rendere visibile un ingresso istantaneo.

Lo stato **Segnale recente** indica una rilevazione negli ultimi 30 secondi, non certifica che la persona sia ancora davanti allo schermo. Il logout esplicito interrompe questo stato finché non arriva una nuova attività. Le giornate storiche non mostrano uno stato dal vivo, anche subito dopo mezzanotte.

## Cosa trovi cliccando una fascia

Il registro è ordinato cronologicamente e combina due fonti, tenute distinguibili:

| Fonte | Informazioni disponibili |
| --- | --- |
| Accesso o richiesta al server | Persona, sede, reparto se disponibile, ora italiana con secondi e CET/CEST, pagina/operazione, esito HTTP e percorso. Per gli oggetti identificabili dal percorso viene conservato anche il contesto del prodotto, documento o ordine, incluso il nome di un documento prima dell'eliminazione. |
| Movimento nei Log | Azione già registrata dal gestionale, categoria, articolo o descrizione e valore numerico del Log. Per carichi e scarichi il valore è la variazione della giacenza; per EDIT è la scorta minima registrata, per ADD la quantità inserita, per MOVE la quantità spostata, per i conteggi la giacenza rilevata. Gli altri valori rimangono indicati come valori del Log, senza attribuire loro un significato diverso. |

Le richieste non riuscite o negate sono evidenziate. Se un'operazione genera un errore e la transazione viene annullata, rimane la richiesta con esito di errore: non viene inventato un movimento salvato.

Una richiesta può produrre uno o più movimenti. Per esempio il salvataggio di un ordine può generare una richiesta e diverse righe di Log. Sono evidenze diverse della stessa attività, non operazioni da sommare indiscriminatamente.

## Contatori e tempi: significati distinti

- **Accessi / pagine aperte** conta le aperture e i login registrati, non soltanto gli accessi con password e non le persone uniche. Passare tra tre pagine può produrre tre accessi.
- **Richieste completate** conta richieste di operazione con risposta HTTP inferiore a 400. Una risposta senza errore HTTP non dimostra da sola una modifica dei dati: i movimenti dei Log sono la conferma disponibile di ciò che è stato salvato.
- **Operazioni non riuscite** conta le richieste con esito HTTP da 400 in su, comprese richieste negate e dati non validi.
- **Movimenti** conta le registrazioni già presenti nei Log. Questi restano consultabili anche quando non esisteva ancora il nuovo registro server.
- **Tempo rilevato**, medie giornaliere e confronti settimanali rimangono separati dai contatori. Una persona può aver eseguito operazioni importanti in pochi secondi.

La misura del tempo si sospende quando la finestra perde il fuoco o dopo circa un minuto senza interazioni. È una stima d'uso del gestionale, non una misura delle ore lavorate. Dopo questo aggiornamento i segnali comprendono più sezioni del gestionale; prima erano limitati alle pagine inventario previste dalla vecchia versione. I confronti che attraversano l'aggiornamento vanno letti tenendo conto di questa differenza.

## Aggiornamento automatico e consultazione

La giornata corrente continua ad aggiornarsi ogni 15 secondi. L'aggiornamento si sospende mentre è aperto un registro o un report individuale, così non interrompe la lettura. Lo zoom scelto viene mantenuto durante gli aggiornamenti. Il dettaglio si chiude con la X, sempre raggiungibile anche scorrendo un registro lungo, oppure con Esc sul PC.

L'esportazione dei riepiloghi giornalieri e settimanali già esistente resta disponibile. Si aggiunge il CSV del registro della singola persona, per tutta la giornata o soltanto per la finestra aperta.

## Storico, dati conservati e limiti del registro

Il pannello indica da quando è presente il nuovo registro server. Per le giornate precedenti si mostrano le fasce già misurate e i movimenti già presenti nei Log, rendendo visibili anche i movimenti isolati. Non è possibile ricostruire accessi, valori precedenti o azioni che la vecchia versione non aveva salvato.

Il registro descrive ciò che è arrivato al gestionale e le informazioni effettivamente disponibili nei Log. Non registra tasti digitati, modifiche mai inviate, password o contenuto dei documenti. Le operazioni già gestite dai Log mantengono il dettaglio che questi conservano; non tutti i vecchi tipi di Log contengono ogni valore prima e dopo la modifica.

La consultazione e gli export del controllo sono riservati agli admin, con verifica sul server anche per richieste dirette. Gli orari del grafico e del dettaglio usano Europe/Rome e distinguono CET e CEST.

## Aggiornamento e verifiche

Il pacchetto è completo e si avvia con le modalità già previste. Al primo avvio il controllo crea automaticamente una nuova tabella e i relativi indici; non richiede reinserimento dei dati. Non vengono cancellati Log o fasce esistenti. I file database presenti nello ZIP sono identici a quelli del pacchetto precedente.

Sono conservati i loghi corretti, i filtri dei Log, i permessi giornalieri di eliminazione dei documenti e la correzione della finestra Aggiungi prodotto su PC. Le funzioni operative di inventario, documenti, ordini e gestione economica non sono state riscritte.

Superati tutti gli 11 nuovi test specifici del controllo. La suite completa esegue 64 test: 59 passano e rimangono le stesse cinque segnalazioni pregresse descritte sotto, senza nuove regressioni rilevate.

Verifiche specifiche: accesso senza JavaScript, login riuscito senza conservare credenziali, ingresso a durata zero, visita di due secondi, storico dai Log, operazioni e richieste negate, rollback, sede dell'oggetto eliminato, logout, mezzanotte italiana, autorizzazioni admin, paginazione e CSV. Sono inoltre stati verificati nel browser Edge su desktop e telefono i marcatori istantanei, gli accessi ravvicinati, lo zoom, il dettaglio con oltre 100 eventi, la lettura dei movimenti, la pausa dell'aggiornamento automatico e la conservazione dello zoom.

La suite generale viene confrontata con le anomalie già documentate nelle versioni precedenti: quattro segnalazioni riguardano test della digitalizzazione documenti già disattivata; un test del pool PostgreSQL richiede psycopg, assente nell'ambiente di prova. Non è stata effettuata una connessione a un database PostgreSQL di produzione. Le verifiche funzionali sono state eseguite su database SQLite temporanei separati dai dati allegati.
