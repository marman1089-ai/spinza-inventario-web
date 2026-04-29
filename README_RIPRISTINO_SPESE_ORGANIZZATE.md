# Ripristino spese + categorie organizzate

Questa versione riparte dalla versione stabile con:

- home gestionale con tabella giorno per giorno;
- grafico a torta delle spese cliccabile;
- dettaglio spese per categoria;
- pulsante `Sposta` con menu categoria;
- import testo con anteprima, giorni vuoti e conferma prima del salvataggio.

Correzione importante: i totali delle spese tornano calcolati dal database come prima, senza filtri che potevano far sembrare i mesi azzerati.

La ricategorizzazione automatica dei dati già presenti modifica solo il campo `categoria` delle uscite riconosciute, senza cancellare o azzerare importi, date, fornitori o note.

Regole migliorate:

- `Metro`, `Sogegross/Sogergross/Socialgross`, `Sapori di Toscana`, `Icaro`, `Prinz`, `Kombucha`, `Legendari`, `Macellaio`, `Forno`, `Buns`, `Carne`, `Caffè`, `Coop`, `Esselunga`, `Carrefour` -> `Materie prime`.
- `Jess`, `Samuele`, `Renis`, `Boubou`, `Angelica`, `Stefano`, `Elio`, `Miriam`, `Amza`, `Coleschi`, `Lorenzo`, `Extra sala`, `anticipo`, `stipendio` -> `Stipendi`.
- `paid by Amza` non cambia più categoria: se la voce è `Metro paid by Amza`, resta `Materie prime`.
- `Coins`, `monete`, `cambio cassa`, `resto` -> `Movimenti cassa`.
- `Spinza` scritto da solo e `Return on investment` -> `Da verificare / movimento interno`.
- `The Fork Pay` -> `Servizi piattaforme`.
- `The Florentine`, social, Instagram, followers, verification -> `Marketing`.

Dopo il deploy, entra in `Uscite` e premi `Ricalcola categorie già inserite` per sistemare le vecchie righe.
