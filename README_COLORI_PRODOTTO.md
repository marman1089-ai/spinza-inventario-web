# Colori per prodotto, raggruppamento per sottogruppo

- Categoria: classificazione principale (ad esempio Bevande).
- Sottogruppo: determina l’ordine di visualizzazione; i prodotti appartenenti a Birra compaiono consecutivamente.
- Nome: identifica il prodotto e il suo colore personalizzato (Birra Bionda oro, Birra Rossa rosso).
- Il campo del database `category_color` è mantenuto per compatibilità con i dati già esistenti ma ora rappresenta il **colore del singolo prodotto**. Non viene più copiato automaticamente agli altri prodotti dello stesso sottogruppo.
- Ordinamento: categoria, sottogruppo, nome prodotto.
- Per prodotti già esistenti, cambiare il colore singolarmente usando Modifica prodotto. I colori salvati vengono conservati.
- Non eliminare o ricreare il database durante l’aggiornamento. Fare prima un backup.
