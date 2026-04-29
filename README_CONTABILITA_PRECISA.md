# Aggiornamento contabilità precisa

Modifiche incluse:

- Import testo nella sezione **Incassi** compatibile con blocchi tipo `APRIL 2025`, date `01/04/2025` e righe con bullet.
- Lettura più precisa delle entrate: POS, Cash/Contanti/QCash, Deliveroo, Glovo, Just Eat, Scuola, Satispay, PayPal.
- Gestione delle righe delivery con cash tra parentesi, esempio `Glovo 87€ (23€ cash)`: il programma confronta il totale dichiarato del giorno e decide se salvare il delivery lordo o al netto del cash, così il totale torna.
- Le righe `Fondo cassa`, `Total`, `Avg / day`, `April 2025 total sales...` vengono trattate come note/riepiloghi e non vengono importate come spese.
- Le spese vengono categorizzate automaticamente in modo più preciso: Stipendi, Materie prime, Manutenzione e attrezzature, Professionisti, Bollette e utenze, Affitti e abbonamenti, Servizi finanziari, Marketing, Packaging, Tasse, Delivery/logistica, Pulizie, Investimenti e Spese secondarie.
- Regola nomi: le voci corte che corrispondono a nomi/personale vengono trattate come **Stipendi**. Sono stati aggiunti nomi/soprannomi ricorrenti come Jess, Samuele, Boubou, Angelica, Miriam, Renis, Alex, Amza, Coleschi, Stefano, Lorenzo, Elio.
- All'avvio il gestionale prova a correggere anche i dati già inseriti: ricategorizza le uscite generiche e ripara vecchi incassi importati male dalle note.
- Nella Home gestionale è stato aggiunto anche il grafico a torta delle spese divise automaticamente per il periodo selezionato: giorno, settimana o mese.
- Nella pagina Uscite resta disponibile il pulsante **Ricalcola categorie già inserite** per rilanciare la correzione sui dati storici.
