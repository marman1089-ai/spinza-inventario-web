# Correzione deploy Render — 24 ottobre 2026

Questa versione parte dal pacchetto `GESTIONALE_RENDER_PULITO_VERIFICA.zip` e corregge gli errori dei log ricevuti:

1. **`psycopg.errors.UndefinedColumn: column "count_id" does not exist`**: erano presenti due sistemi di conteggio che utilizzavano lo stesso nome di tabella (`stock_count_lines`) con colonne incompatibili. I report Sala/Cucina usano ora le tabelle autonome `inventory_period_count_headers` e `inventory_period_count_lines`. La tabella esistente `stock_count_lines` NON viene eliminata o modificata.
2. **`ALTER TABLE users ADD COLUMN last_seen`**: in PostgreSQL usa `IF NOT EXISTS`.
3. **`AUTOINCREMENT` su PostgreSQL**: la tabella `sales_report_group_models` usa ora il tipo di chiave primaria appropriato per il database.

## Installazione

Estrarre lo ZIP e copiare i file nella cartella principale del repository GitHub (quella con `app/` e `requirements.txt`), sovrascrivendo i file esistenti. Effettuare commit e push. Non eliminare il database PostgreSQL/Supabase, gli allegati o le variabili d'ambiente di Render.

## Verifiche

Sono state superate la compilazione Python e l'inizializzazione SQLite locale delle nuove tabelle. Il deploy Render/PostgreSQL **non è stato eseguito**; ulteriori problemi eventualmente presenti vanno verificati nei log del deploy successivo.
