# File archiviati

Nuova area del gestionale disponibile da **File archiviati** nel menu.

Funzioni:
- cartelle e sottocartelle con nome libero;
- caricamento multiplo di file/foto/documenti;
- creazione diretta di file `.txt`;
- ricerca;
- anteprima sicura per immagini raster, PDF e testo;
- download;
- rinomina ed eliminazione;
- separazione dei dati per negozio (gli admin possono usare anche lo spazio `ALL`);
- file salvati come BLOB/BYTEA nel database SQLite/PostgreSQL, quindi non dipendono dal filesystem effimero di Render.

Il limite predefinito è 25 MB per singolo file e può essere modificato con la variabile d'ambiente `ARCHIVE_MAX_FILE_BYTES`.
