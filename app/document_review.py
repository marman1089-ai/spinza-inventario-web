"""Admin verification of uploaded business documents."""
import secrets
from datetime import date, datetime
from zoneinfo import ZoneInfo

from fastapi import HTTPException, Request
from fastapi.responses import RedirectResponse

from .db import connect, using_postgres

SOURCES = {
    'chiusure': ('closures', 'closure_date', 'Foto chiusura'),
    'fatture': ('invoices_docs', 'doc_date', 'Fattura'),
    'spese-secondarie': ('secondary_expenses', 'expense_date', 'Spesa secondaria'),
}


def placeholder():
    return '%s' if using_postgres() else '?'


def ensure_schema(cur):
    cur.execute('''CREATE TABLE IF NOT EXISTS admin_document_reviews (
        kind TEXT NOT NULL, doc_id INTEGER NOT NULL,
        reviewed_at TEXT, reviewed_by TEXT, PRIMARY KEY(kind,doc_id))''')


def mark_upload(cur, kind, doc_id, user):
    if user.get('role') != 'admin':
        ph = placeholder()
        cur.execute(f'INSERT INTO admin_document_reviews(kind,doc_id) VALUES({ph},{ph})',
                    (kind, doc_id))


def available_document(kind, id_column='id'):
    """Legacy and admin uploads are available immediately."""
    return (f"{id_column} NOT IN (SELECT doc_id FROM admin_document_reviews "
            f"WHERE kind='{kind}' AND reviewed_at IS NULL)")


def pending_documents(cur, scope):
    result = []
    for kind, (table, date_column, label) in SOURCES.items():
        supplier = 'd.supplier' if kind == 'fatture' else "'' AS supplier"
        scope_clause = '' if scope == 'ALL' else f' AND d.store={placeholder()}'
        args = () if scope == 'ALL' else (scope,)
        rows = cur.execute(
            f"""SELECT d.id,d.store,d.filename,d.uploaded_by,CAST(d.{date_column} AS TEXT) AS doc_date,{supplier}
                FROM {table} d JOIN admin_document_reviews r ON r.doc_id=d.id AND r.kind='{kind}'
                WHERE r.reviewed_at IS NULL{scope_clause}""", args).fetchall()
        result.extend(dict(row, kind=kind, label=label) for row in rows)
    return sorted(result, key=lambda row: (row['doc_date'], row['id']), reverse=True)


def install(app, api):
    ph = placeholder

    def require_admin(request):
        user = api['require_login'](request)
        if not user or user.get('role') != 'admin':
            raise HTTPException(403, 'Verifica riservata agli admin.')
        return user, api['_effective_store'](request, user)

    @app.get('/admin/file-da-verificare/documenti/{kind}/{doc_id}')
    def review_document(request: Request, kind: str, doc_id: int):
        user, scope = require_admin(request)
        with connect() as conn:
            document = next((row for row in pending_documents(conn.cursor(), scope)
                             if row['kind'] == kind and row['id'] == doc_id), None)
        if document is None:
            raise HTTPException(404, 'Documento in attesa non trovato.')
        csrf = request.session.setdefault('closure_reports_csrf', secrets.token_urlsafe(32))
        return api['render']('document_review.html', request=request, user=user, brand=scope,
                             stores=api['STORES'], document=document, csrf=csrf)

    @app.post('/admin/file-da-verificare/documenti/{kind}/{doc_id}')
    async def approve_document(request: Request, kind: str, doc_id: int):
        user, scope = require_admin(request)
        if kind not in SOURCES:
            raise HTTPException(404, 'Documento non trovato.')
        form = await request.form()
        token = request.session.get('closure_reports_csrf')
        if not token or not secrets.compare_digest(str(form.get('csrf', '')), token):
            raise HTTPException(403, 'Sessione scaduta. Ricarica la pagina.')
        try:
            day = date.fromisoformat(str(form.get('doc_date', ''))).isoformat()
        except ValueError:
            raise HTTPException(422, 'Data del documento non valida.')
        filename = api['_archive_clean_name'](str(form.get('filename', '')))
        supplier = str(form.get('supplier', '')).strip()
        if kind == 'fatture' and (not supplier or len(supplier) > 180):
            raise HTTPException(422, 'Inserisci il fornitore, massimo 180 caratteri.')
        table, date_column, _ = SOURCES[kind]
        with connect() as conn:
            cur = conn.cursor()
            if not using_postgres():
                cur.execute('BEGIN IMMEDIATE')
            lock = ' FOR UPDATE' if using_postgres() else ''
            row = cur.execute(f'SELECT store FROM {table} WHERE id={ph()}'+lock, (doc_id,)).fetchone()
            if not row or (scope != 'ALL' and scope != row['store']):
                raise HTTPException(404, 'Documento non trovato.')
            reviewed = cur.execute(
                f'''UPDATE admin_document_reviews SET reviewed_at={ph()},reviewed_by={ph()}
                    WHERE kind={ph()} AND doc_id={ph()} AND reviewed_at IS NULL RETURNING doc_id''',
                (datetime.now(ZoneInfo('Europe/Rome')).isoformat(timespec='seconds'),
                 user['username'], kind, doc_id)).fetchone()
            if not reviewed:
                raise HTTPException(409, 'Documento già confermato.')
            fields = f'{date_column}={ph()},filename={ph()}'
            values = [day, filename]
            if kind == 'fatture':
                fields += f',supplier={ph()}'
                values.append(supplier)
            cur.execute(f'UPDATE {table} SET {fields} WHERE id={ph()}', (*values, doc_id))
            cur.execute(f'DELETE FROM admin_file_index WHERE kind={ph()} AND file_id={ph()}', (kind, doc_id))
            api['_log'](cur, store=row['store'], username=user['username'], action='DOC_CONFERMA',
                        category=kind.upper(), name=filename, delta=0)
        return RedirectResponse('/admin/file-da-verificare', 303)
