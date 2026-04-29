# Categorie contabili più precise

Aggiornamento per rendere la divisione delle spese più scrupolosa.

## Cosa cambia

- La categoria viene decisa sulla singola riga della spesa, non sul blocco intero del giorno.
- Le righe di contesto come Fondo cassa, POS, Cash, Deliveroo, Glovo e Total non influenzano più le spese.
- Materie prime ha regole forti per fornitori food/bibite: Metro, Sogegross/Sogergross/Socialgross, Sapori di Toscana, Icaro, Prinz, Kombucha, Legendari/Leggendari, Forno, Macellaio, Buns, Caffè/Coffee, Coop, Esselunga, Carrefour, Conad, Golden Italia, Aqua Golden.
- Stipendi viene usato per stipendio, anticipo, extra sala, consegna e nomi persona.
- paid by Amza non cambia categoria: se la spesa è Metro paid by Amza, resta Materie prime.
- Coins/monete/cambio/resto vanno in Movimenti cassa solo se la voce è davvero quella.

## Nuova funzione: il programma impara da Sposta

Quando nella torta spese o nella pagina Uscite sposti una voce in una categoria, il programma salva una regola basata sul fornitore/voce.

Esempio:
- sposti Prinz bibite in Materie prime;
- il programma memorizza Prinz -> Materie prime;
- aggiorna anche le vecchie voci simili senza toccare importi, date o note.

## Come usarlo

1. Carica lo zip su GitHub/Render.
2. Entra in Uscite.
3. Premi Ricalcola categorie già inserite.
4. Controlla la torta delle spese.
5. Se trovi una voce sbagliata, usa Sposta: da quel momento il programma impara anche per i dati vecchi simili.
