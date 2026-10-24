"""Verifica DATABASE_URL senza avviare il sito e mostra solo conteggi, mai password."""
from app.db import connect, using_postgres

if not using_postgres():
    raise SystemExit("DATABASE_URL PostgreSQL assente o non valida.")

with connect() as db:
    cur = db.cursor()
    cur.execute("SELECT current_database(), current_user")
    identity = cur.fetchone()
    database_name = identity.get("current_database")
    database_user = identity.get("current_user")
    print("Connessione riuscita")
    print("Database:", database_name)
    print("Utente:", database_user)
    cur.execute("""
        SELECT table_name
        FROM information_schema.tables
        WHERE table_schema='public' AND table_type='BASE TABLE'
        ORDER BY table_name
    """)
    for row in cur.fetchall():
        table = row.get("table_name")
        cur.execute(f'SELECT COUNT(*) AS total FROM "{table}"')
        print(f"{table}: {cur.fetchone().get('total')}")
