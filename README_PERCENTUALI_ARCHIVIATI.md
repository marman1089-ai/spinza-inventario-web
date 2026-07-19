# Percentuali IVA e proprietario nei negozi archiviati

Ogni negozio archiviato dispone ora di due valori configurabili:

- percentuale IVA;
- percentuale del proprietario/appalto.

## Calcolo usato

Gli incassi salvati sono considerati lordi:

1. incasso netto IVA = incasso lordo / (1 + IVA / 100);
2. IVA scorporata = incasso lordo - incasso netto IVA;
3. quota proprietario = incasso netto IVA × percentuale proprietario / 100;
4. spese complessive = spese manuali + IVA scorporata + quota proprietario;
5. bilancio finale = incasso lordo - spese complessive.

Le percentuali possono essere indicate durante la creazione del negozio e modificate in seguito dalla pagina del singolo archivio. Il ricalcolo è automatico su mesi, anni, schede negozio e Panoramica totale. Non vengono create righe di uscita fittizie: IVA e quota proprietario rimangono costi automatici chiaramente separati dalle spese manuali.
