# Modifica categorie uscite automatiche

Questa versione migliora la divisione automatica delle uscite.

## Cosa cambia

- Le uscite importate da testo/file non vengono più salvate come `import txt`, ma vengono già salvate nella categoria corretta.
- Tutti i nomi di persona riconosciuti come voce di spesa vengono trattati come **Stipendi**.
- Sono state rese più precise le famiglie automatiche:
  - Stipendi
  - Materie prime
  - Manutenzione
  - Professionisti
  - Bollette e utenze
  - Affitti e abbonamenti
  - Packaging
  - Servizi finanziari
  - Marketing
  - Delivery e logistica
  - Tasse
  - Pulizie e consumo interno
  - Spese secondarie

## Dati già inseriti

All'avvio dell'app il programma rilegge le uscite già salvate e corregge automaticamente solo quelle con categoria generica, per esempio:

- `import txt`
- `spese`
- `varie`
- `altro`
- `spese secondarie`

In più, nella pagina **Uscite** c'è il pulsante:

**Ricalcola categorie già inserite**

Serve per correggere subito i dati già salvati senza dover reimportare tutto.

## Esempi

- `Marco 500€` → Stipendi
- `Giulia 300€` → Stipendi
- `Metro 120€` → Materie prime
- `Consulente lavoro 663€` → Professionisti
- `Nexi commissioni 45€` → Servizi finanziari
- `Acqua 90€` → Bollette e utenze
