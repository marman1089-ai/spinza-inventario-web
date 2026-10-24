# Aggiornamento sopra gestiooo.zip

Questo pacchetto deriva esattamente dal progetto fornito `gestiooo.zip`. Preserva i moduli originali (inclusa la digitalizzazione già presente) e aggiunge la digitalizzazione OCR gratuita e la funzione conteggi/report inventario.

Estrai direttamente nella cartella del repository GitHub, sovrascrivendo i file omonimi. Non eliminare la cartella `.git`. Non aggiungere database locali, archivi di upload, chiavi API o segreti al repository.

Prima di pubblicare: esegui backup del database PostgreSQL e degli allegati memorizzati su Render. Esegui un test con un documento e un inventario. Non è stato possibile testare su Render.

ATTENZIONE: il conteggio inventario richiede conferma delle quantità e gli arrivi vengono ricavati dai movimenti registrati; la digitalizzazione OCR richiede verifica manuale dei dati riconosciuti.
