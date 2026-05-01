# Aggiornamento Professional UI Refresh

Modifiche aggiunte senza cambiare la logica del gestionale:

- nuovo file `app/static/css/pro-ui-refresh.css` con stile unico globale;
- nuovo file `app/static/js/pro-ui-refresh.js` con transizioni, progress bar e feedback di caricamento;
- `base.html` collegato ai nuovi asset e pulito da tag `<style>` duplicati;
- logo messo più in risalto dentro un riquadro chiaro/elegante;
- menu, select, pulsanti, card e tabelle resi più uniformi;
- mobile più compatto: grafici dashboard e report vendite organizzati meglio, con card più piccole;
- caricamento animato quando si cambia pagina, si salva o si importa un file;
- prefetch leggero dei link interni per migliorare la sensazione di velocità;
- compressione GZip e cache degli asset statici in `app/main.py`.

Nota: la velocità reale dipende anche da Render/Supabase, ma queste modifiche riducono peso frontend e migliorano molto la percezione durante i cambi pagina.
