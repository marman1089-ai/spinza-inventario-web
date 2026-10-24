# Inventario 2026 — aggiornamento del gestionale

## Sedi
- Spinza
- Reburger Camaldoli
- Reburger Maggio (sostituisce Reburger Palazzuolo nel menu inventario)

Alla prima esecuzione, i prodotti dell'inventario Palazzuolo vengono assegnati a Maggio quando non esiste già un prodotto identico nella nuova sede; le righe potenzialmente conflittuali restano intatte, così non si cancellano dati. Anche le richieste di acquisto pendenti e le utenze staff già associate a Palazzuolo vengono spostate a Maggio. Lo storico degli ordini già conclusi e le registrazioni contabili storiche non vengono riscritti.

## Inventario
- Mantiene quantità, modifica diretta, calcolatrice incremento/decremento e quantità minima per ogni prodotto.
- Mostra conteggio prodotti e prodotti sotto minimo.
- Nuovo campo **fornitore** per ogni prodotto, anche nei database esistenti. Il pulsante 🚚 sulla scheda prodotto assegna/modifica il fornitore.
- Le quantità e i prodotti preesistenti non vengono resettati.

## Carrello e ordini
- Staff: aggiunge prodotti e quantità nel carrello `/ordini`.
- Admin: visita `/ordini/preparazione`, unico spazio di preparazione con ordini raggruppati per **negozio + fornitore**.
- Ogni gruppo produce un messaggio con il pulsante **Copia messaggio**.
- Una volta inviato al fornitore, premere **Conferma ordine inviato**: si utilizza il flusso `orders` + `order_lines` esistente, con successiva gestione ricezione e storico già presente.
- I prodotti senza fornitore sono messi in evidenza e non possono essere confermati accidentalmente in gruppi sbagliati.
- La conferma ordini è accessibile solo all'admin e verifica la corrispondenza effettiva tra fornitore e prodotto.

## Installazione
Aggiornare il codice mantenendo le impostazioni d'ambiente, la `DATABASE_URL` e l'archivio persistente del deployment. Fare prima un backup del database. Non cancellare né sostituire il database live con il file `spinza.db` presente nel pacchetto. All'avvio `init_db()` applica le modifiche additive e idempotenti. L'archivio ZIP fornito è una copia aggiornata del progetto, non modifica direttamente il sito online.
