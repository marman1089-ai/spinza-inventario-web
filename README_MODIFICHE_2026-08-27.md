# Aggiornamento gestionale — 27 agosto 2026

Modifiche applicate alla versione principale del progetto (root dello ZIP), senza cambiare la struttura dati o il funzionamento contabile esistente.

## Nuova pagina: Storico mensile
- Nuova rotta: `/gestionale/storico-mensile`
- Mesi mostrati in ordine cronologico, dal più vecchio al più recente.
- Per ogni mese: Entrate, Uscite, Bilancio, numero di movimenti.
- Filtri `Da mese` / `A mese` per scegliere esattamente il range da visualizzare.
- I mesi senza movimenti all'interno del range vengono mostrati a zero, così la sequenza resta continua.
- Totali e medie del range selezionato.
- Pulsante per aprire direttamente il dettaglio del singolo mese nella dashboard.
- Accesso aggiunto al menu Gestionale desktop, menu mobile, pulsanti della dashboard e riepilogo mensile.

## Inventario e finestre
- Aggiunta prodotto, modifica prodotto e CSV vengono aperti in un overlay realmente centrato rispetto allo schermo.
- Il pannello viene spostato sotto `body` durante l'apertura per evitare spostamenti causati da card, animazioni o posizione del pulsante.
- Calcolatrice e finestra ordine usano lo stesso comportamento di centratura.
- Chiusura tramite X, click sullo sfondo ed ESC.
- Blocco dello scroll mentre una finestra è aperta.

## UI e animazioni
- Transizioni più morbide per pagine, menu, pulsanti, card e campi.
- Supporto alle View Transitions del browser quando disponibile, con fallback automatico.
- Glow e profondità più moderni mantenendo colori e tema esistenti.
- Rispetto di `prefers-reduced-motion`.

## Controlli e precisione
- Verificata la sintassi Python dell'app.
- Verificato il parsing di tutti i template Jinja.
- Verificata l'assenza di rotte duplicate.
- Testata la nuova timeline con mesi pieni e mesi vuoti.
- Corretto il doppio pulsante `Chiudi` nel menu mobile.
- Resi più precisi i log di inizializzazione SQLite: una colonna già presente non viene più segnalata come falso errore.
- Il database `spinza.db` è stato ripristinato byte-per-byte dall'originale prima di creare lo ZIP finale.

I template vecchi/non collegati a rotte attive sono stati lasciati invariati per non alterare materiale storico o di backup.
