import os
import datetime
import json
import csv
import io
import calendar
from datetime import date, datetime, timedelta
import re
from urllib.parse import quote_plus, unquote_plus, urlencode

from fastapi import FastAPI, Request, Form, UploadFile, File
from .pdf_tools import ensure_pdf, merge_pdfs
from fastapi.responses import RedirectResponse, HTMLResponse, PlainTextResponse
from fastapi.staticfiles import StaticFiles
from starlette.middleware.sessions import SessionMiddleware
from starlette.middleware.gzip import GZipMiddleware
from starlette.status import HTTP_303_SEE_OTHER
from jinja2 import Environment, FileSystemLoader, select_autoescape

from .db import connect, init_db, ensure_db_exists, using_postgres
from .security import verify_password, legacy_sha256, make_password
from .migrate_from_old import run_migration
from .restore_backup import restore_sqlite_backup_if_postgres_empty

BASE_DIR = os.path.dirname(__file__)
templates = Environment(
    loader=FileSystemLoader(os.path.join(BASE_DIR, "templates")),
    autoescape=select_autoescape(["html", "xml"]),
)

def render(name: str, **ctx):
    tpl = templates.get_template(name)
    return HTMLResponse(tpl.render(**ctx))


def _safe_next_url(next_url: str | None, default: str = "/inventario") -> str:
    """Permette redirect SOLO interni (evita open-redirect)."""
    if not next_url:
        return default
    u = (next_url or "").strip()
    if not u.startswith("/"):
        return default
    return u

app = FastAPI(title="Spinza Inventario")

# Compressione sicura: rende più leggere le pagine HTML/CSS/JS senza cambiare la logica.
app.add_middleware(GZipMiddleware, minimum_size=700)

@app.middleware("http")
async def add_static_cache_headers(request: Request, call_next):
    response = await call_next(request)
    if request.url.path.startswith("/static/"):
        # Gli asset hanno versionamento ?v=... nei template: possiamo tenerli in cache.
        response.headers.setdefault("Cache-Control", "public, max-age=604800, immutable")
    return response

# === Multi-inventory (stores) ===
STORES = {
    "spinza": "Spinza",
    "reburger_camaldoli": "Reburger Camaldoli",
    "reburger_palazzuolo": "Reburger Palazzuolo",
}

app.add_middleware(
    SessionMiddleware,
    secret_key=os.environ.get("SESSION_SECRET", "CHANGE_ME_PLS"),
    same_site="lax",
)

app.mount("/static", StaticFiles(directory=os.path.join(BASE_DIR, "static")), name="static")


# =========================
# DB HELPERS (SQLITE/POSTGRES)
# =========================
def _ph() -> str:
    return "%s" if using_postgres() else "?"

def _now() -> str:
    return "NOW()" if using_postgres() else "datetime('now')"

def _today_str() -> str:
    return date.today().isoformat()


def _clean_category_color(value: str | None) -> str:
    """Colore leggero del gruppo categoria, salvato come HEX sicuro."""
    c = (value or "").strip()
    if re.fullmatch(r"#[0-9a-fA-F]{6}", c):
        return c.lower()
    return "#64748b"

def _default_category_color(category: str) -> str:
    palette = ["#ef4444", "#f97316", "#eab308", "#22c55e", "#14b8a6", "#3b82f6", "#8b5cf6", "#ec4899", "#94a3b8"]
    key = (category or "").strip().lower()
    if not key:
        return "#64748b"
    return palette[sum(ord(ch) for ch in key) % len(palette)]


# =========================
# UNIT PARSING (display totals)
# =========================
def _parse_pack_unit(unit: str):
    """Parsa unità tipo '1kg', '0,2kg', '120g', '0.5L', '500ml'.
    Ritorna (value: float|None, suffix: str|None).
    """
    u = (unit or "").strip().lower().replace(" ", "")
    if not u:
        return (None, None)
    u = u.replace("lt", "l")
    m = re.match(r"^([0-9]+(?:[\.,][0-9]+)?)\s*(kg|g|l|ml)$", u)
    if not m:
        return (None, None)
    val_s = m.group(1).replace(",", ".")
    try:
        val = float(val_s)
    except Exception:
        return (None, None)
    return (val, m.group(2))

def _compute_display_totals(total_qty: float, unit: str):
    """Heuristica:
    - Se unit è un pack (es. 0,2kg / 1kg / 0,75l / 120g / 500ml),
      calcola anche il totale convertito (kg o l) e decide come mostrarlo.
    """
    val, suf = _parse_pack_unit(unit)
    pieces = float(total_qty or 0.0)

    if val is None or suf is None:
        return {"show_pieces": False, "pieces": pieces, "conv_val": None, "conv_unit": None}

    if suf == "kg":
        conv_val = pieces * val
        conv_unit = "kg"
    elif suf == "g":
        conv_val = pieces * (val / 1000.0)
        conv_unit = "kg"
    elif suf == "l":
        conv_val = pieces * val
        conv_unit = "l"
    elif suf == "ml":
        conv_val = pieces * (val / 1000.0)
        conv_unit = "l"
    else:
        conv_val = None
        conv_unit = None

    show_pieces = (suf in ("g", "ml")) or (abs(val - 1.0) > 1e-9)
    return {"show_pieces": show_pieces, "pieces": pieces, "conv_val": conv_val, "conv_unit": conv_unit}

# =========================
# LOG HELPERS
# =========================
def _log(cur, *, store: str, username: str, action: str, category: str, name: str, delta: float = 0.0):
    """Inserisce una riga di log coerente tra SQLite/Postgres."""
    ph = _ph()
    now = _now()
    cur.execute(
        f"INSERT INTO logs(ts, store, username, action, category, name, delta) VALUES({now},{ph},{ph},{ph},{ph},{ph},{ph})",
        (store, username, action, category, name, float(delta)),
    )


def _split_logs(rows):
    """Divide i log in 3 sezioni: inventario, ordini, documenti/foto."""
    inv, ords, docs = [], [], []
    for r in rows:
        action = (r.get("action") if isinstance(r, dict) else getattr(r, "action", "")) or ""
        category = (r.get("category") if isinstance(r, dict) else getattr(r, "category", "")) or ""

        a = str(action).upper()
        c = str(category).upper()

        if a.startswith("ORDER_") or c in ("ORDINI", "ORDER"):
            ords.append(r)
        elif c in ("CHIUSURE", "FATTURE", "SPESE") or a.startswith("DOC_"):
            docs.append(r)
        else:
            inv.append(r)
    return inv, ords, docs


# =========================
# SESSION HELPERS
# =========================
def require_login(request: Request):
    user = request.session.get("user")
    if not user:
        return None

    user_id = user.get("id")
    if not user_id:
        request.session.pop("user", None)
        return None

    # Ricarica SEMPRE l'utente dal DB: evita sessioni stale/bucate
    # (es. ruolo admin non aggiornato in sessione, store mancante, ecc.)
    try:
        ph = _ph()
        now = _now()
        with connect() as conn:
            cur = conn.cursor()
            db_user = cur.execute(
                f"SELECT id, username, role, store FROM users WHERE id={ph}",
                (int(user_id),),
            ).fetchone()
            if not db_user:
                request.session.pop("user", None)
                return None
            cur.execute(f"UPDATE users SET last_seen={now} WHERE id={ph}", (int(user_id),))

            fresh_user = {
                "id": db_user["id"],
                "username": db_user["username"],
                "role": db_user["role"],
                "store": db_user.get("store"),
                "store_label": STORES.get(db_user.get("store"), db_user.get("store")),
            }
            request.session["user"] = fresh_user
            return fresh_user
    except Exception:
        # fallback: almeno mantieni la sessione esistente senza rompere la navigazione
        if user.get("store"):
            user["store_label"] = STORES.get(user.get("store"), user.get("store"))
        return user

def get_selected_store(request: Request):
    return request.session.get("selected_store")

def set_selected_store(request: Request, store: str):
    request.session["selected_store"] = store

def get_selected_area(request: Request) -> str | None:
    return request.session.get("selected_area")

def set_selected_area(request: Request, area: str):
    request.session["selected_area"] = area

def is_admin(request: Request) -> bool:
    u = request.session.get("user")
    return bool(u and u.get("role") == "admin")


def can_view_management_finance(request: Request, user: dict | None = None) -> bool:
    user = user or request.session.get("user")
    if not user:
        return False
    if user.get("role") == "admin":
        return True
    # fallback difensivo: se la sessione è vecchia ma nel DB il ruolo è admin,
    # non blocchiamo l'utente sulle pagine del gestionale.
    try:
        ph = _ph()
        with connect() as conn:
            cur = conn.cursor()
            row = cur.execute(f"SELECT role FROM users WHERE id={ph}", (int(user.get("id")),)).fetchone()
            return bool(row and row.get("role") == "admin")
    except Exception:
        return False

def _admin_store(request: Request) -> str:
    s = (request.session.get("admin_store") or "spinza")
    return s if s in STORES else "spinza"

def _render_admin(request: Request, *, user, users, msg=None, error=None):
    admin_store = _admin_store(request)

    # Arricchisce la lista utenti per la vista admin:
    # - online: True se last_seen è recente
    # - last_seen_str: ultimo accesso formattato (data + ORARIO)
    ONLINE_MINUTES = 5

    def _parse_last_seen(v):
        if v is None:
            return None
        # psycopg può tornare un datetime, sqlite spesso una stringa
        if isinstance(v, datetime):
            return v
        try:
            s = str(v).replace("T", " ").strip()
            # prova formati comuni
            for fmt in ("%Y-%m-%d %H:%M:%S.%f%z", "%Y-%m-%d %H:%M:%S%z", "%Y-%m-%d %H:%M:%S.%f", "%Y-%m-%d %H:%M:%S"):
                try:
                    return datetime.strptime(s, fmt)
                except Exception:
                    pass
        except Exception:
            return None
        return None

    def _fmt_last_seen(dt: datetime | None) -> str | None:
        if not dt:
            return None
        # Mostriamo data + orario (richiesto per gli offline)
        return dt.strftime("%d/%m/%Y %H:%M")

    now = datetime.utcnow()
    decorated = []
    for u in (users or []):
        # dict_row -> dict; sqlite.Row -> mapping
        d = dict(u)
        dt = _parse_last_seen(d.get("last_seen"))
        d["last_seen_str"] = _fmt_last_seen(dt)
        if dt:
            age = now - (dt.replace(tzinfo=None) if getattr(dt, "tzinfo", None) else dt)
            d["online"] = age.total_seconds() <= ONLINE_MINUTES * 60
        else:
            d["online"] = False
        decorated.append(d)

    return render(
        "admin.html",
        user=user,
        users=decorated,
        msg=msg,
        error=error,
        stores=STORES,
        admin_store=admin_store,
        brand=admin_store,
    )

def _admin_users_render_error(request: Request, user, error_msg: str):
    with connect() as conn:
        cur = conn.cursor()
        users = cur.execute("SELECT id, store, username, role, last_seen FROM users ORDER BY role DESC, store, username").fetchall()
    return _render_admin(request, user=user, users=users, error=error_msg)


# =========================
# ADMIN BOOTSTRAP (NO SHELL NEEDED)
# =========================
def ensure_admin_user():
    """
    Admin fisso:
      username: admin
      password: spinza2025
    Modificabili con ENV:
      ADMIN_USERNAME, ADMIN_PASSWORD
    Reset forzato con:
      RESET_ADMIN=1
    """
    admin_user = os.environ.get("ADMIN_USERNAME", "admin").strip()
    admin_pass = os.environ.get("ADMIN_PASSWORD", "spinza2025").strip()
    reset = os.environ.get("RESET_ADMIN", "0").strip() == "1"
    if not admin_user or not admin_pass:
        return

    salt, h = make_password(admin_pass)
    ph = _ph()

    with connect() as conn:
        cur = conn.cursor()
        row = cur.execute(
            f"SELECT id FROM users WHERE username={ph} AND role='admin'",
            (admin_user,),
        ).fetchone()

        if row:
            if reset:
                cur.execute(
                    f"UPDATE users SET pw_salt={ph}, pw_hash={ph}, legacy_sha256=NULL, store={ph}, role='admin' WHERE id={ph}",
                    (salt, h, "spinza", row["id"]),
                )
        else:
            cur.execute(
                f"INSERT INTO users(store, username, role, pw_salt, pw_hash, legacy_sha256) VALUES({ph},{ph},'admin',{ph},{ph},NULL)",
                ("spinza", admin_user, salt, h),
            )


# =========================
# STARTUP
# =========================
@app.on_event("startup")
def _startup():
    print("====================================")
    print("[STARTUP] Applicazione avviata")
    db_url = os.getenv("DATABASE_URL")
    print("[STARTUP] DATABASE_URL presente:", bool(db_url))
    print("[STARTUP] DB TYPE:", "POSTGRES (Supabase)" if using_postgres() else "SQLITE (ATTENZIONE)")

    ensure_db_exists()
    init_db()
    print("[STARTUP] init_db() completato")

    restore_report = restore_sqlite_backup_if_postgres_empty()
    print("[STARTUP] controllo backup SQLite:", restore_report)

    ensure_admin_user()
    print("[STARTUP] ensure_admin_user() completato")

    # Sistema i dati già inseriti: le righe importate come "import txt" o generiche
    # vengono rilette e riclassificate nelle famiglie corrette.
    fixed_expenses = _recategorize_existing_cash_expenses('ALL')
    fixed_entries = _repair_existing_cash_entries_from_notes('ALL')
    print(f"[STARTUP] uscite ricategorizzate: {fixed_expenses}")
    print(f"[STARTUP] incassi riparati da note import: {fixed_entries}")

    if os.environ.get("MIGRATE_ON_START") == "1":
        data_dir = os.environ.get("OLD_DATA_DIR", ".")
        run_migration(data_dir)
        print("[STARTUP] Migrazione completata")

    print("====================================")


# =========================
# ROUTES: HOME / STORE / AUTH
# =========================
@app.get("/", response_class=HTMLResponse)
def home(request: Request):
    if require_login(request):
        return RedirectResponse("/select-area", status_code=HTTP_303_SEE_OTHER)
    return RedirectResponse("/select-store", status_code=HTTP_303_SEE_OTHER)

@app.get("/select-store", response_class=HTMLResponse)
def select_store_get(request: Request, store: str = ""):
    store = (store or "").strip()
    if store in STORES:
        set_selected_store(request, store)
        return RedirectResponse("/login", status_code=HTTP_303_SEE_OTHER)
    return render("select_store.html", user=None, stores=STORES, brand=None)

@app.get("/login", response_class=HTMLResponse)
def login_get(request: Request):
    store = get_selected_store(request)
    if not store or store not in STORES:
        return RedirectResponse("/select-store", status_code=HTTP_303_SEE_OTHER)
    return render("login.html", error=None, store=store, store_label=STORES[store], brand=store)

@app.post("/login", response_class=HTMLResponse)
def login_post(request: Request, username: str = Form(...), password: str = Form(...)):
    username = username.strip()
    store = get_selected_store(request)
    if not store or store not in STORES:
        return RedirectResponse("/select-store", status_code=HTTP_303_SEE_OTHER)

    ph = _ph()
    with connect() as conn:
        cur = conn.cursor()
        row = cur.execute(
            f"SELECT * FROM users WHERE username={ph} AND store={ph}",
            (username, store),
        ).fetchone()

        if not row:
            return render("login.html", error="Credenziali non valide.", store=store, store_label=STORES[store], brand=store)

        ok = False
        if row.get("pw_salt") and row.get("pw_hash"):
            ok = verify_password(password, row["pw_salt"], row["pw_hash"])
        elif row.get("legacy_sha256"):
            ok = (legacy_sha256(password) == row["legacy_sha256"])
            if ok:
                salt, h = make_password(password)
                cur.execute(
                    f"UPDATE users SET pw_salt={ph}, pw_hash={ph}, legacy_sha256=NULL WHERE id={ph}",
                    (salt, h, row["id"]),
                )

        if not ok:
            return render("login.html", error="Credenziali non valide.", store=store, store_label=STORES[store], brand=store)

        request.session["user"] = {
            "id": row["id"],
            "username": row["username"],
            "role": row["role"],
            "store": row["store"],
            "store_label": STORES.get(row["store"], row["store"]),
        }

    # reset area ad ogni login; l'inventario ora ha una home separata
    request.session.pop("selected_area", None)
    return RedirectResponse("/select-area", status_code=HTTP_303_SEE_OTHER)


## NOTE:
## Route /select-area definita più sotto (con AREAS e UI completa).
## Questa vecchia versione è stata rimossa per evitare doppia registrazione.



# =========================
# WORKSPACE HOME (GESTIONALE / INVENTARIO)
# =========================
def _current_store_scope(request: Request, user: dict):
    store = (request.session.get("active_store") if is_admin(request) else user.get("store")) or "spinza"
    if store not in STORES and store != "ALL":
        store = "spinza"
    return store


def _fetch_one_int(cur, sql: str, params: tuple):
    try:
        row = cur.execute(sql, params).fetchone()
    except Exception:
        return 0
    if not row:
        return 0
    try:
        if isinstance(row, dict):
            return int(list(row.values())[0] or 0)
        return int(row[0] or 0)
    except Exception:
        try:
            return int(next(iter(dict(row).values())) or 0)
        except Exception:
            return 0


def _store_label(store: str) -> str:
    key = str(store or '').strip()
    if key in STORES:
        return STORES.get(key, key)
    try:
        archived = _archived_stores_map()
        if key in archived:
            return archived[key].get('name') or key
    except Exception:
        pass
    return key


def _dict_rows(cur, sql: str, params: tuple = ()):
    try:
        return [dict(r) for r in cur.execute(sql, params).fetchall()]
    except Exception:
        return []


def _fetch_one_float(cur, sql: str, params: tuple):
    try:
        row = cur.execute(sql, params).fetchone()
    except Exception:
        return 0.0
    if not row:
        return 0.0
    try:
        if isinstance(row, dict):
            return float(list(row.values())[0] or 0)
        return float(row[0] or 0)
    except Exception:
        try:
            return float(next(iter(dict(row).values())) or 0)
        except Exception:
            return 0.0


def _cash_scope_where(store: str):
    ph = _ph()
    if store == 'ALL':
        return '1=1', ()
    return f'store={ph}', (store,)


ARCHIVED_STORE_PREFIX = 'archived_'


def _archived_store_slug(name: str) -> str:
    base = _normalize_signature(name or 'negozio').replace(' ', '_')
    base = re.sub(r'[^a-z0-9_]+', '', base).strip('_') or 'negozio'
    base = base[:36]
    suffix = datetime.now().strftime('%Y%m%d%H%M%S%f')
    return f'{ARCHIVED_STORE_PREFIX}{base}_{suffix}'


def _archived_stores_map(cur=None) -> dict:
    """Mappa store_key -> info negozio archiviato. Non lancia mai errori se la tabella non esiste ancora."""
    def _load(cursor):
        rows = _dict_rows(cursor, "SELECT id, store_key, name, opened_at, closed_at, notes FROM archived_stores ORDER BY name ASC", ())
        out = {}
        for r in rows:
            key = str(r.get('store_key') or '').strip()
            if key:
                out[key] = r
        return out
    if cur is not None:
        return _load(cur)
    try:
        with connect() as conn:
            return _load(conn.cursor())
    except Exception:
        return {}


def _all_management_stores(cur=None, include_archived: bool = True) -> dict:
    stores = dict(STORES)
    if include_archived:
        for key, info in _archived_stores_map(cur).items():
            stores[key] = info.get('name') or key
    return stores


def _is_known_management_store(store_key: str, cur=None, include_archived: bool = True) -> bool:
    key = str(store_key or '').strip()
    if key in STORES:
        return True
    if include_archived:
        return key in _archived_stores_map(cur)
    return False


def _is_archived_store_key(store_key: str, cur=None) -> bool:
    key = str(store_key or '').strip()
    return key.startswith(ARCHIVED_STORE_PREFIX) and key in _archived_stores_map(cur)


def _store_kind(store_key: str, archived_map: dict | None = None) -> str:
    key = str(store_key or '').strip()
    archived_map = archived_map if archived_map is not None else _archived_stores_map()
    return 'archiviato' if key in archived_map or key.startswith(ARCHIVED_STORE_PREFIX) else 'attivo'


def _weekly_dates(days: int = 7):
    end = date.today()
    start = end - timedelta(days=days-1)
    dates = []
    cur = start
    while cur <= end:
        dates.append(cur)
        cur += timedelta(days=1)
    return dates


def _period_bounds(period_type: str, anchor_day: date):
    period_type = (period_type or 'day').lower()
    if period_type == 'month':
        start = anchor_day.replace(day=1)
        if start.month == 12:
            next_month = start.replace(year=start.year + 1, month=1, day=1)
        else:
            next_month = start.replace(month=start.month + 1, day=1)
        end = next_month - timedelta(days=1)
    elif period_type == 'week':
        start = anchor_day - timedelta(days=anchor_day.weekday())
        end = start + timedelta(days=6)
    else:
        start = anchor_day
        end = anchor_day
        period_type = 'day'
    return period_type, start, end


def _shift_previous_period(period_type: str, start: date, end: date):
    if period_type == 'month':
        prev_end = start - timedelta(days=1)
        prev_start = prev_end.replace(day=1)
    else:
        span_days = (end - start).days + 1
        prev_end = start - timedelta(days=1)
        prev_start = prev_end - timedelta(days=span_days - 1)
    return prev_start, prev_end


def _pct_change(current: float, previous: float):
    current = float(current or 0)
    previous = float(previous or 0)
    diff = current - previous
    if previous == 0:
        if current == 0:
            return 0.0
        return 100.0
    return (diff / abs(previous)) * 100.0


def _load_cash_payment_methods(cur):
    rows = _dict_rows(cur, "SELECT name FROM cash_payment_methods ORDER BY sort_order ASC, name ASC")
    names = [str(r.get('name') or '').strip() for r in rows if str(r.get('name') or '').strip()]
    base = ['contanti', 'pos', 'deliveroo', 'glovo', 'just eat']
    for name in base:
        if name not in names:
            names.append(name)
    return names


def _ensure_payment_method(cur, name: str, username: str = 'system'):
    name = (name or '').strip()
    if not name:
        return

    ph = _ph()
    try:
        # IMPORTANT: on Postgres, swallowing a duplicate-key error without a
        # rollback poisons the whole transaction and the next INSERT ends with
        # `InFailedSqlTransaction`. So we avoid the error altogether.
        existing = _fetch_one_int(cur, f"SELECT COUNT(*) FROM cash_payment_methods WHERE name={ph}", (name,))
        if existing:
            return
        max_order = _fetch_one_int(cur, "SELECT COALESCE(MAX(sort_order), 0) FROM cash_payment_methods", ())
        cur.execute(
            f"INSERT INTO cash_payment_methods(name, sort_order, is_default, created_by) VALUES({ph},{ph},0,{ph})",
            (name, int(max_order) + 10, username or 'system'),
        )
    except Exception:
        # Best effort: if an unexpected DB error happens, clear the aborted
        # transaction state on Postgres so saving the incasso can still work.
        try:
            cur.connection.rollback()
        except Exception:
            pass

def _safe_amount(value, default: float = 0.0) -> float:
    raw = str(value or "").strip()
    if not raw:
        return default
    raw = raw.replace("€", "").replace("EUR", "").replace("eur", "").replace(" ", "")
    raw = raw.replace(",", ".")
    try:
        amount = float(raw)
    except Exception:
        return default
    return amount if amount >= 0 else default


def _existing_columns(cur, table_name: str):
    """Return lowercase column names for a table on both SQLite and Postgres."""
    table_name = (table_name or '').strip()
    if not table_name:
        return set()
    try:
        if using_postgres():
            rows = cur.execute(
                "SELECT column_name FROM information_schema.columns WHERE table_schema='public' AND table_name=%s",
                (table_name,),
            ).fetchall()
            names = []
            for r in rows:
                if isinstance(r, dict):
                    names.append(r.get('column_name'))
                else:
                    names.append(r[0])
            return {str(x).lower() for x in names if x}
        rows = cur.execute(f"PRAGMA table_info({table_name})").fetchall()
        names = []
        for r in rows:
            if isinstance(r, dict):
                names.append(r.get('name'))
            elif hasattr(r, 'keys') and 'name' in r.keys():
                names.append(r['name'])
            else:
                names.append(r[1])
        return {str(x).lower() for x in names if x}
    except Exception:
        return set()


def _insert_cash_entry_compat(cur, store: str, flow_date: str, payment_method: str, amount: float, notes: str, created_by: str):
    cols = _existing_columns(cur, 'cash_entries')
    if not cols:
        cols = {'store', 'flow_date', 'payment_method', 'amount', 'orders_count', 'notes', 'created_by'}
    values = {
        'store': store,
        'flow_date': flow_date,
        'payment_method': payment_method,
        'amount': float(amount or 0),
        'orders_count': 0,
        'notes': notes or '',
        'created_by': created_by or 'system',
    }
    ordered_cols = [c for c in ['store', 'flow_date', 'payment_method', 'amount', 'orders_count', 'notes', 'created_by'] if c in cols]
    if not ordered_cols:
        raise RuntimeError('cash_entries table not available')
    placeholders = ','.join([_ph()] * len(ordered_cols))
    sql = f"INSERT INTO cash_entries({', '.join(ordered_cols)}) VALUES({placeholders})"
    params = tuple(values[c] for c in ordered_cols)
    cur.execute(sql, params)


def _insert_cash_expense_compat(cur, store: str, flow_date: str, category: str, supplier: str, payment_method: str, amount: float, notes: str, created_by: str):
    cols = _existing_columns(cur, 'cash_expenses')
    if not cols:
        cols = {'store', 'flow_date', 'category', 'supplier', 'payment_method', 'amount', 'notes', 'created_by'}
    values = {
        'store': store,
        'flow_date': flow_date,
        'category': category or 'import txt',
        'supplier': supplier or '',
        'payment_method': payment_method or '',
        'amount': float(amount or 0),
        'notes': notes or '',
        'created_by': created_by or 'system',
    }
    ordered_cols = [c for c in ['store', 'flow_date', 'category', 'supplier', 'payment_method', 'amount', 'notes', 'created_by'] if c in cols]
    if not ordered_cols:
        raise RuntimeError('cash_expenses table not available')
    placeholders = ','.join([_ph()] * len(ordered_cols))
    sql = f"INSERT INTO cash_expenses({', '.join(ordered_cols)}) VALUES({placeholders})"
    params = tuple(values[c] for c in ordered_cols)
    cur.execute(sql, params)


def _decode_uploaded_text(raw_bytes: bytes):
    payload = raw_bytes or b''
    for encoding in ('utf-8-sig', 'utf-8', 'cp1252', 'latin-1'):
        try:
            return payload.decode(encoding)
        except Exception:
            continue
    return payload.decode('utf-8', errors='ignore')


def _is_truthy(value) -> bool:
    return str(value or '').strip().lower() in {'1', 'true', 'on', 'yes', 'si', 'sì'}


def _join_unique_notes(parts, max_len: int = 700):
    out = []
    seen = set()
    for part in parts or []:
        clean = re.sub(r'\s+', ' ', str(part or '').strip())
        if not clean:
            continue
        key = clean.lower()
        if key in seen:
            continue
        seen.add(key)
        out.append(clean)
    joined = ' | '.join(out)
    return joined[:max_len].strip()


def _amount_token_to_float(token: str):
    """Converte importi scritti all'italiana o all'inglese: 1.234,56 / 1234.56 / 1234."""
    raw = str(token or '').replace('€', '').replace(' ', '').replace('\u00a0', '').strip()
    if not raw:
        return None
    # 43.396,63 => 43396.63
    if ',' in raw and '.' in raw:
        raw = raw.replace('.', '').replace(',', '.')
    # 67,50 => 67.50
    elif ',' in raw:
        raw = raw.replace(',', '.')
    # 1.500 => 1500 se il punto sembra separatore migliaia
    elif raw.count('.') == 1:
        left, right = raw.split('.', 1)
        if len(right) == 3 and len(left) <= 3:
            raw = left + right
    try:
        return float(raw)
    except Exception:
        return None


def _amount_tokens(text: str):
    s = (text or '').replace('\u00a0', ' ')
    # Numeri con o senza separatori: 1500, 1.500, 67.50, 67,50, 43.396,63
    return re.findall(r"(?<![A-Za-z0-9])([0-9]{1,3}(?:[\.\s][0-9]{3})+(?:,[0-9]{1,2})?|[0-9]+(?:[\.,][0-9]{1,2})?)\s*€?", s)


def _find_amount_values(text: str):
    values = []
    for token in _amount_tokens(text):
        val = _amount_token_to_float(token)
        if val is not None:
            values.append(val)
    return values


def _parse_flexible_amount(text: str):
    """Importo generico per le spese. Preferisce il risultato dopo '='; altrimenti prende il primo importo reale della riga."""
    s = (text or '').replace('\u00a0', ' ')
    if '=' in s:
        after_eq = s.split('=')[-1]
        values = _find_amount_values(after_eq)
        if values:
            return values[-1]
    values = _find_amount_values(s)
    if not values:
        return None
    return values[0]


def _parse_income_amount(line: str, payment_method: str = ''):
    """Importo per le entrate: Glovo 87€ (23€ cash) deve salvare 87, non 23."""
    s = (line or '').replace('\u00a0', ' ')
    lower = _normalize_import_text_for_matching(s)
    # Scuola 2 x 16€ = 32,00€ => prendo il totale dopo '='.
    if '=' in s:
        values_after_eq = _find_amount_values(s.split('=')[-1])
        if values_after_eq:
            return values_after_eq[-1]
    # Scuola 4 x 16€ senza totale: calcolo 64.
    mult = re.search(r"\b([0-9]+(?:[\.,][0-9]{1,2})?)\s*x\s*([0-9]+(?:[\.,][0-9]{1,2})?)\b", lower)
    if mult and (payment_method or '').lower() == 'scuola':
        left = _amount_token_to_float(mult.group(1)) or 0
        right = _amount_token_to_float(mult.group(2)) or 0
        product = left * right
        if product > 0:
            return product
    # Tolgo parentesi tipo "(23€ cash)" perché sono note, non incasso principale del canale.
    main_part = re.sub(r"\([^)]*\)", " ", s)
    values = _find_amount_values(main_part)
    if values:
        return values[0]
    return _parse_flexible_amount(s)


def _normalize_import_payment_method(label: str):
    s = (label or '').strip().lower()
    s = re.sub(r"^[\-•⁃–—·*]+", '', s).strip()
    s = re.sub(r'\s+', ' ', s)
    aliases = {
        'pos': 'pos',
        'card': 'pos',
        'carta': 'pos',
        'bancomat': 'pos',
        'cash': 'contanti',
        'qcash': 'contanti',
        'q cash': 'contanti',
        'contanti': 'contanti',
        'contante': 'contanti',
        'deliveroo': 'deliveroo',
        'glovo': 'glovo',
        'just eat': 'just eat',
        'justeat': 'just eat',
        'scuola': 'scuola',
        'satispay': 'satispay',
        'paypal': 'paypal',
    }
    if s in aliases:
        return aliases[s]
    # Permetto solo suffissi tecnici, non nomi fornitori tipo "Cash coffee".
    for key, value in aliases.items():
        if s.startswith(key + ' '):
            rest = s[len(key):].strip()
            if rest in {'totale', 'incasso', 'entrata', 'vendite'}:
                return value
    return ''


def _normalize_import_text_for_matching(text: str) -> str:
    """Versione semplice senza accenti, utile per riconoscere mesi/negozi."""
    try:
        import unicodedata
        text = unicodedata.normalize('NFKD', text or '')
        text = ''.join(ch for ch in text if not unicodedata.combining(ch))
    except Exception:
        text = text or ''
    return text.lower().strip()


MONTH_WORDS = {
    'gennaio': 1, 'jan': 1, 'january': 1,
    'febbraio': 2, 'feb': 2, 'february': 2,
    'marzo': 3, 'mar': 3, 'march': 3,
    'aprile': 4, 'apr': 4, 'april': 4,
    'maggio': 5, 'may': 5,
    'giugno': 6, 'jun': 6, 'june': 6,
    'luglio': 7, 'jul': 7, 'july': 7,
    'agosto': 8, 'aug': 8, 'august': 8,
    'settembre': 9, 'sett': 9, 'sep': 9, 'september': 9,
    'ottobre': 10, 'oct': 10, 'october': 10,
    'novembre': 11, 'nov': 11, 'november': 11,
    'dicembre': 12, 'dec': 12, 'december': 12,
}


_SKIP_IMPORT_AMOUNT_PREFIXES = (
    'total', 'totale', 'totali', 'totale mese', 'totale incassi', 'incassi totali',
    'avg', 'average', 'media', 'fondo cassa', 'fondo', 'saldo', 'netto', 'lordo'
)

_SKIP_IMPORT_AMOUNT_CONTAINS = (
    'total sales', 'totale vendite', 'totale sales', 'avg / day', 'avg/day',
    'media / giorno', 'media/giorno', 'average / day', 'average/day',
    'total month', 'totale mese', 'totale aprile', 'totale gennaio', 'totale febbraio',
    'totale marzo', 'totale maggio', 'totale giugno', 'totale luglio', 'totale agosto',
    'totale settembre', 'totale ottobre', 'totale novembre', 'totale dicembre'
)


def _should_skip_import_amount_line(line: str) -> bool:
    lower = _normalize_import_text_for_matching(line)
    if not lower:
        return True
    if lower.startswith(_SKIP_IMPORT_AMOUNT_PREFIXES):
        return True
    if any(key in lower for key in _SKIP_IMPORT_AMOUNT_CONTAINS):
        return True
    # Righe riepilogo tipo "April 2024 Total sales 32.371,88€" o "April 2023 total sales...".
    if any(re.search(r'\b' + re.escape(month_word) + r'\b', lower) for month_word in MONTH_WORDS) and 'total' in lower:
        return True
    return False


def _detect_import_store(line: str, current_store: str) -> str:
    upper = (line or '').upper()
    # I nomi dei negozi possono comparire dentro una spesa (es. "Forno spinza 4000€").
    # Cambio negozio solo su righe intestazione, non su righe contabili con importo.
    if '€' in upper:
        return current_store or 'spinza'
    if 'SPINZA' in upper:
        return 'spinza'
    if 'CAMALDOLI' in upper:
        return 'reburger_camaldoli'
    if 'PALAZZUOLO' in upper or 'PALAZZUOLI' in upper:
        return 'reburger_palazzuolo'
    return current_store or 'spinza'


def _detect_import_month_year(line: str, current_month=None, current_year=None):
    normalized = _normalize_import_text_for_matching(line)
    month = current_month
    year = current_year
    month_found_in_line = False

    # Formati tipo 04/2026, 04-2026, 2026-04
    m_num = re.search(r'\b(20\d{2})[\-/\.](0?[1-9]|1[0-2])\b', normalized)
    if m_num:
        year = int(m_num.group(1)); month = int(m_num.group(2)); month_found_in_line = True
    m_num = re.search(r'\b(0?[1-9]|1[0-2])[\-/\.](20\d{2})\b', normalized)
    if m_num:
        month = int(m_num.group(1)); year = int(m_num.group(2)); month_found_in_line = True

    # Formati tipo APRILE 2026 / APRIL 2026 / SPINZA FEBRUARY 2026.
    # Non uso numeri a 1-2 cifre come anno, altrimenti righe tipo "Deliveroo 80€" diventano 2080.
    for word, value in MONTH_WORDS.items():
        if re.search(r'\b' + re.escape(word) + r'\b', normalized):
            month = value
            month_found_in_line = True
            break
    y = re.search(r'\b(20\d{2})\b', normalized)
    if y and month_found_in_line:
        year = int(y.group(1))
    return month, year


def _start_import_block(blocks, current, store, iso_date):
    if current and (current.get('incomes') or current.get('expenses') or current.get('notes')):
        blocks.append(current)
    return {
        'store': store,
        'date': iso_date,
        'incomes': [],
        'expenses': [],
        'notes': [],
        'declared_total': None,
    }


def _extract_expense_name_from_line(line: str, amount=None) -> str:
    """Tiene il nome vero della spesa anche se l'importo è all'inizio: "15€ coins" => "coins"."""
    s = re.sub(r"\s+", " ", (line or '').replace('\u00a0', ' ')).strip()
    s = re.sub(r"^[\-•⁃–—·*]+", "", s).strip()
    # tolgo note su chi ha pagato, ma le lascio comunque nelle note originali dell'uscita
    s = re.sub(r"\b(paid by|pagato da|pagata da|by)\s+[A-Za-zÀ-ÿ0-9_ .'-]+$", "", s, flags=re.I).strip()
    # rimuovo espressioni matematiche di importo e importi singoli
    s = re.sub(r"=\s*€?\s*[0-9]{1,3}(?:[\.\s][0-9]{3})*(?:,[0-9]{1,2})?\s*€?", " ", s)
    s = re.sub(r"€?\s*[0-9]{1,3}(?:[\.\s][0-9]{3})+(?:,[0-9]{1,2})?\s*€?", " ", s)
    s = re.sub(r"€?\s*[0-9]+(?:[\.,][0-9]{1,2})?\s*€?", " ", s)
    s = re.sub(r"\b(x|per)\b", " ", s, flags=re.I)
    s = re.sub(r"[|:;,_/\\()\[\]{}]+", " ", s)
    s = re.sub(r"\s+", " ", s).strip(" -–—")
    return (s or 'Import TXT')[:120]



def _cash_component_inside_parentheses(line: str) -> float:
    total = 0.0
    for inside in re.findall(r"\(([^)]*)\)", line or ''):
        norm = _normalize_import_text_for_matching(inside)
        if 'cash' not in norm and 'contant' not in norm:
            continue
        amount = _parse_flexible_amount(inside)
        if amount:
            total += float(amount)
    return total


def _reconcile_import_blocks(blocks):
    """Usa la riga Total del giorno per capire se il cash scritto tra parentesi va sottratto dal delivery.
    Esempio: Glovo 87€ (23€ cash) con Total che torna solo sottraendo 23 => salva Glovo 64.
    Se invece il Total torna con 87 pieno, lascia 87.
    """
    for block in blocks or []:
        declared = block.get('declared_total')
        if not declared:
            continue
        incomes = block.get('incomes') or []
        gross = sum(float(x.get('amount') or 0) for x in incomes)
        cash_components = []
        for income in incomes:
            method = str(income.get('payment_method') or '').lower()
            if method not in {'glovo', 'deliveroo', 'just eat'}:
                cash_components.append(0.0)
                continue
            cash_part = _cash_component_inside_parentheses(str(income.get('raw') or ''))
            cash_components.append(cash_part)
        cash_total = sum(cash_components)
        if cash_total <= 0:
            continue
        net = gross - cash_total
        # Scelgo la versione che combacia meglio con il totale dichiarato.
        if abs(net - float(declared)) + 0.01 < abs(gross - float(declared)):
            for income, cash_part in zip(incomes, cash_components):
                if cash_part > 0:
                    income['amount'] = max(0.0, round(float(income.get('amount') or 0) - cash_part, 2))
                    income['cash_component_note'] = cash_part
    return blocks

def _parse_import_date_from_line(line: str, context_month=None, context_year=None):
    clean = re.sub(r"\s+", ' ', (line or '').replace(' ', ' ')).strip()
    normalized = _normalize_import_text_for_matching(clean)

    # 2026-04-01
    m = re.search(r"\b(20\d{2})[\-/\.](\d{1,2})[\-/\.](\d{1,2})\b", clean)
    if m:
        yy, mm, dd = m.groups()
        try:
            return date(int(yy), int(mm), int(dd)).isoformat()
        except Exception:
            return None

    # 01/04/26, 01-04-2026, 1.4.26
    m = re.search(r"\b(\d{1,2})[\-/\.](\d{1,2})[\-/\.](\d{2,4})\b", clean)
    if m:
        dd, mm, yy = m.groups()
        year = int(yy)
        if year < 100:
            year += 2000
        try:
            return date(year, int(mm), int(dd)).isoformat()
        except Exception:
            return None

    # 01/04 oppure 01-04: uso anno dal titolo/mese corrente se presente
    m = re.search(r"\b(\d{1,2})[\-/\.](\d{1,2})\b", clean)
    if m and context_year:
        dd, mm = m.groups()
        try:
            return date(int(context_year), int(mm), int(dd)).isoformat()
        except Exception:
            return None

    # 1 aprile 2026 / 1 apr / 1 APRILE
    for word, month_value in MONTH_WORDS.items():
        m = re.search(r"\b(\d{1,2})\s+" + re.escape(word) + r"\b", normalized)
        if m:
            day = int(m.group(1))
            y = re.search(r"\b(20\d{2}|\d{2})\b", normalized)
            year = int(y.group(1)) if y else int(context_year or date.today().year)
            if year < 100:
                year += 2000
            try:
                return date(year, month_value, day).isoformat()
            except Exception:
                return None

    # Riga solo giorno tipo "1", "01", "1 lunedì", "01 - lun" dentro un titolo APRILE 2026.
    # Evito righe con euro/importi o parole da incasso/spesa.
    if context_month and context_year and '€' not in clean:
        if not re.search(r"\b(pos|cash|contanti|deliveroo|glovo|just\s*eat|satispay|paypal|totale|fondo|metro|forno|nexi|affitto)\b", normalized):
            m = re.match(r"^(\d{1,2})(?:\s|$|[\-\./])", normalized)
            if m:
                day = int(m.group(1))
                if 1 <= day <= 31:
                    try:
                        return date(int(context_year), int(context_month), day).isoformat()
                    except Exception:
                        return None
    return None


def _parse_import_txt_blocks(raw_text: str, fallback_store: str = 'spinza'):
    text = (raw_text or '').replace('\r\n', '\n').replace('\r', '\n')
    lines = [ln.strip() for ln in text.split('\n')]
    current = None
    blocks = []
    store = fallback_store or 'spinza'
    context_month = None
    context_year = None

    def close_current():
        nonlocal current
        if current and (current['incomes'] or current['expenses'] or current['notes']):
            blocks.append(current)
        current = None

    for raw in lines:
        line = re.sub(r"\s+", ' ', raw.replace(' ', ' ')).strip()
        line = re.sub(r"^[\-•⁃–—·*]+", '', line).strip()
        if not line:
            continue

        detected_store = _detect_import_store(line, store)
        if detected_store != store:
            close_current()
            store = detected_store
        context_month, context_year = _detect_import_month_year(line, context_month, context_year)

        # Non trasformare i riepiloghi di fine mese in nuove giornate o spese.
        if _should_skip_import_amount_line(line):
            if current:
                current['notes'].append(line)
                lower_skip = _normalize_import_text_for_matching(line)
                if lower_skip.startswith(('total', 'totale')) and 'sales' not in lower_skip:
                    total_amount = _parse_flexible_amount(line)
                    if total_amount is not None:
                        current['declared_total'] = total_amount
            continue

        iso_date = _parse_import_date_from_line(line, context_month, context_year)
        if iso_date:
            current = _start_import_block(blocks, current, store, iso_date)
            continue

        # Se il testo ha solo intestazioni prima della data, non salvo nulla finché non trovo una giornata.
        if not current:
            continue

        lower = _normalize_import_text_for_matching(line)
        if _should_skip_import_amount_line(line):
            current['notes'].append(line)
            continue

        amount = _parse_flexible_amount(line)
        if amount is None:
            current['notes'].append(line)
            continue

        label = re.split(r"[0-9]", line, 1)[0].strip(' :-–—')
        pay_method = _normalize_import_payment_method(label)
        if pay_method:
            income_amount = _parse_income_amount(line, pay_method)
            if income_amount is not None:
                amount = income_amount
            current['incomes'].append({'payment_method': pay_method, 'amount': amount, 'raw': line})
            # Salvo sempre la riga originale nelle note: serve per controllo contabile e riparazioni future.
            current['notes'].append(line)
        else:
            # Ogni riga con importo non riconosciuta come metodo di pagamento viene trattata come spesa.
            expense_name = _extract_expense_name_from_line(line, amount)
            current['expenses'].append({'name': expense_name[:120], 'amount': amount, 'raw': line})

    close_current()
    return _reconcile_import_blocks(blocks)


def _expense_category_options():
    """Categorie principali usate nei grafici e nei menu 'Sposta'."""
    preferred = [
        'Stipendi',
        'Materie prime',
        'Manutenzione e attrezzature',
        'Professionisti',
        'Bollette e utenze',
        'Affitti e abbonamenti',
        'Servizi finanziari',
        'Servizi piattaforme',
        'Marketing',
        'Packaging',
        'Tasse',
        'Delivery e logistica',
        'Pulizie e consumo interno',
        'Da verificare / movimento interno',
        'Movimenti cassa',
        'Spese secondarie',
    ]
    seen = set()
    out = []
    for name in preferred + [r[0] for r in _EXPENSE_RULES]:
        key = _normalize_signature(name)
        if key and key not in seen:
            out.append(name)
            seen.add(key)
    return out



def _import_expense_override_key(block, expense_idx, fallback_store: str = 'spinza'):
    """Chiave stabile per collegare una spesa del preview alla categoria scelta prima del salvataggio."""
    store_key = str((block or {}).get('store') or fallback_store or 'spinza').strip()
    if not _is_known_management_store(store_key):
        store_key = fallback_store if _is_known_management_store(fallback_store) else 'spinza'
    flow_date = str((block or {}).get('date') or '').strip()
    try:
        idx = int(expense_idx or 0)
    except Exception:
        idx = 0
    return f"{store_key}|{flow_date}|{idx}"

def _build_import_preview(blocks, raw_text: str, fallback_store: str, include_expenses: bool, replace_existing_dates: bool):
    """Riepilogo leggibile prima di salvare l'import TXT: totali, righe estratte e giorni mancanti."""
    preview_rows = []
    expense_rows = []
    month_map = {}
    income_total = 0.0
    expense_total = 0.0
    income_count = 0
    expense_count = 0
    category_options = _expense_category_options()

    for block in blocks or []:
        ds = str(block.get('date') or '').strip()
        store_key = str(block.get('store') or fallback_store or 'spinza').strip()
        if not _is_known_management_store(store_key):
            store_key = fallback_store if _is_known_management_store(fallback_store) else 'spinza'
        incomes = block.get('incomes') or []
        expenses = block.get('expenses') or []
        inc_total = round(sum(float(x.get('amount') or 0) for x in incomes), 2)
        exp_total = round(sum(float(x.get('amount') or 0) for x in expenses), 2)
        income_total += inc_total
        expense_total += exp_total
        income_count += len([x for x in incomes if float(x.get('amount') or 0) > 0])
        expense_count += len([x for x in expenses if float(x.get('amount') or 0) > 0])
        try:
            dd = datetime.strptime(ds, '%Y-%m-%d').date()
            mk = ds[:7]
            month_map.setdefault((store_key, mk), set()).add(dd.day)
        except Exception:
            dd = None

        methods = {}
        for item in incomes:
            name = str(item.get('payment_method') or '').strip().lower() or 'non specificato'
            methods[name] = methods.get(name, 0.0) + float(item.get('amount') or 0)

        expense_samples = []
        for exp_idx, item in enumerate(expenses):
            amount = round(float(item.get('amount') or 0), 2)
            if amount <= 0:
                continue
            raw_line = str(item.get('raw') or '').strip()
            supplier = str(item.get('name') or raw_line or 'Import TXT').strip()[:120]
            category = _auto_expense_category('import txt', supplier, raw_line)
            if category and category not in category_options:
                category_options.append(category)
            expense_row = {
                'key': _import_expense_override_key(block, exp_idx, fallback_store),
                'date': ds,
                'store': store_key,
                'store_label': _store_label(store_key),
                'name': supplier[:80],
                'raw': raw_line[:180],
                'amount': amount,
                'category': category,
            }
            expense_rows.append(expense_row)
            if len(expense_samples) < 6:
                expense_samples.append({
                    'name': expense_row['name'],
                    'amount': amount,
                    'category': category,
                })

        preview_rows.append({
            'date': ds,
            'store': store_key,
            'store_label': _store_label(store_key),
            'income_total': inc_total,
            'expense_total': exp_total,
            'income_count': len(incomes),
            'expense_count': len(expenses),
            'methods': [{'name': k, 'amount': round(v, 2)} for k, v in sorted(methods.items())],
            'expense_samples': expense_samples,
            'declared_total': block.get('declared_total'),
        })

    missing_groups = []
    for (store_key, mk), days_present in sorted(month_map.items(), key=lambda x: (x[0][1], x[0][0])):
        try:
            year, month = [int(x) for x in mk.split('-', 1)]
            last_day = calendar.monthrange(year, month)[1]
            missing = [f'{mk}-{day:02d}' for day in range(1, last_day + 1) if day not in days_present]
            missing_groups.append({
                'store': store_key,
                'store_label': _store_label(store_key),
                'month_key': mk,
                'month_label': _month_label(mk),
                'present_count': len(days_present),
                'days_in_month': last_day,
                'missing_dates': missing,
                'missing_count': len(missing),
            })
        except Exception:
            continue

    return {
        'raw_text': raw_text or '',
        'fallback_store': fallback_store,
        'include_expenses': bool(include_expenses),
        'replace_existing_dates': bool(replace_existing_dates),
        'rows': preview_rows,
        'expense_rows': expense_rows,
        'category_options': category_options,
        'days_count': len(preview_rows),
        'income_count': income_count,
        'expense_count': expense_count,
        'income_total': round(income_total, 2),
        'expense_total': round(expense_total, 2),
        'missing_groups': missing_groups,
    }


PALETTE_DARK = [
    '#38bdf8',  # azzurro
    '#2563eb',  # blu
    '#818cf8',  # indaco
    '#a855f7',  # viola
    '#ec4899',  # rosa
    '#ef4444',  # rosso
    '#f97316',  # arancio
    '#f59e0b',  # ambra
    '#eab308',  # giallo
    '#22c55e',  # verde
    '#14b8a6',  # teal
    '#06b6d4',  # ciano
    '#84cc16',  # lime
    '#f43f5e',  # rose
    '#8b5cf6',  # violetto
]


def _build_conic_gradient(rows, total: float, value_key: str = 'total', palette: list[str] | None = None, empty_color: str = 'rgba(148,163,184,.18)'):
    palette = palette or PALETTE_DARK
    total = float(total or 0)
    if total <= 0 or not rows:
        return empty_color
    cursor = 0.0
    segments = []
    for idx, row in enumerate(rows):
        value = float(row.get(value_key) or 0)
        if value <= 0:
            continue
        share = (value / total) * 100.0
        color = row.get('color') or palette[idx % len(palette)]
        row['color'] = color
        end = min(100.0, cursor + share)
        segments.append(f"{color} {cursor:.2f}% {end:.2f}%")
        cursor = end
    if cursor < 100.0:
        segments.append(f"{empty_color} {cursor:.2f}% 100%")
    return 'conic-gradient(' + ', '.join(segments) + ')'


def _scope_period_label(period_type: str, previous: bool = False) -> str:
    kind = (period_type or 'month').lower()
    labels = {
        'month': ('Questo mese', 'Mese scorso'),
        'week': ('Questa settimana', 'Settimana precedente'),
        'day': ('Questo giorno', 'Giorno precedente'),
    }
    pair = labels.get(kind, labels['month'])
    return pair[1] if previous else pair[0]


def _strip_accents(value: str) -> str:
    try:
        import unicodedata
        text = unicodedata.normalize('NFKD', str(value or ''))
        return ''.join(ch for ch in text if not unicodedata.combining(ch))
    except Exception:
        return str(value or '')


def _normalize_signature(value: str) -> str:
    raw = re.sub(r'[^a-z0-9]+', ' ', _strip_accents(str(value or '')).lower())
    return re.sub(r'\s+', ' ', raw).strip()


def _title_case_words(value: str) -> str:
    value = str(value or '').strip()
    if not value:
        return ''
    return ' '.join(part[:1].upper() + part[1:] for part in value.split())


_GENERIC_EXPENSE_CATEGORIES = {
    '', 'import txt', 'import', 'txt', 'uscita', 'uscite', 'spesa', 'spese', 'varie', 'varia',
    'altro', 'altra', 'spese varie', 'spesa varia', 'spese secondarie', 'secondarie', 'non specificato'
}

_KNOWN_EMPLOYEE_NAMES = {
    'lipo', 'niccolo', 'nicolo', 'nicola', 'elio', 'mattia', 'marco', 'giulia', 'mira',
    'andrea', 'alessandro', 'alessandra', 'alessio', 'antonio', 'anna', 'arianna', 'beatrice',
    'carlo', 'chiara', 'claudia', 'cristian', 'cristiano', 'daniele', 'davide', 'denise',
    'edoardo', 'elena', 'emanuele', 'emma', 'fabio', 'federica', 'filippo', 'francesca',
    'francesco', 'gabriele', 'gaia', 'giorgia', 'giorgio', 'giovanni', 'giuseppe', 'ilaria',
    'irene', 'laura', 'leonardo', 'lorenzo', 'luca', 'luigi', 'maria', 'martina', 'matteo',
    'michele', 'paolo', 'riccardo', 'roberto', 'sara', 'serena', 'simone', 'sofia',
    'stefano', 'tommaso', 'valentina', 'valerio', 'veronica',
    # Nomi/soprannomi reali che compaiono negli appunti contabili: se una voce è solo un nome, è personale.
    'jess', 'samuele', 'boubou', 'angelica', 'aneglica', 'miriam', 'renis', 'alisa',
    'alex', 'amza', 'coleschi', 'mohamed', 'youssef', 'hamza', 'mario', 'marta'
}

_NON_EMPLOYEE_SINGLE_WORDS = {
    'metro', 'carrefour', 'esselunga', 'coop', 'conad', 'lidl', 'aldi', 'pam', 'nexi', 'sumup',
    'qonto', 'enel', 'eni', 'tim', 'wind', 'iliad', 'vodafone', 'amazon', 'google', 'meta',
    'instagram', 'facebook', 'tiktok', 'deliveroo', 'glovo', 'justeat', 'just', 'satispay',
    'paypal', 'iva', 'inps', 'f24', 'tari', 'imu', 'affitto', 'canone', 'abbonamento',
    'luce', 'gas', 'acqua', 'telefono', 'internet', 'banca', 'pos', 'commissione', 'commissioni',
    'farina', 'pomodoro', 'mozzarella', 'bufala', 'salumi', 'salume', 'prosciutto', 'salsiccia',
    'nduja', 'verdure', 'bibite', 'bevande', 'forno', 'frigo', 'freezer', 'lavastoviglie',
    'cartoni', 'vaschette', 'buste', 'tovaglioli', 'packaging', 'consulente', 'commercialista',
    'notaio', 'avvocato', 'studio', 'manutenzione', 'riparazione', 'benzina', 'carburante',
    'fornitore', 'fornitori', 'materie', 'materia', 'prime', 'marketing', 'pubblicita',
    'spinza', 'reburger', 'camaldoli', 'palazzuolo', 'palazzuoli', 'cure', 'lecure', 'le',
    'atollo', 'villani', 'bnb', 'foscolo', 'scuola', 'cinese', 'coins', 'coin', 'bit',
    'brico', 'ferramenta', 'vetraio', 'cappa', 'impastatrice', 'motorino', 'nastrocolor',
    'sogergross', 'lumina', 'the', 'fork', 'florentine', 'cheque', 'rent', 'rata',
    'return', 'investment', 'coffee', 'caffe', 'caffè', 'aqua', 'golden', 'italia',
    'buns', 'carne', 'macellaio', 'forno', 'vodafone', 'wifi', 'vodafone', 'oneri',
    'commissioni', 'imposta', 'bollo', 'instagram', 'insta', 'followers', 'verification',
    'architect', 'architetto', 'alberghiera', 'mini', 'pinner'
}

_EMPLOYEE_WORD_HINTS = (
    'stipend', 'salario', 'salari', 'personale', 'dipendent', 'busta paga', 'retribuz',
    'anticipo', 'acconto', 'extra sala', 'extra cucina', 'extra marzo', 'extra aprile',
    'consegna', 'alberghiera', 'ore ', 'ore:', 'turno', 'turni', 'collaborator', 'lavorator'
)

_EXPENSE_RULES = [
    # Ordine importante: prima voci molto specifiche, poi categorie generiche.
    ('Professionisti', [
        'commercialist', 'consulent', 'consulente del lavoro', 'cedolino', 'paghe', 'professionist',
        'avvocato', 'notaio', 'studio professionale', 'labor consultant', 'architect', 'architetto',
        'architett', 'geometra', 'ingegnere', 'progettista', 'sicurezza lavoro', 'haccp', 'medico competente'
    ]),
    ('Tasse', [
        'inps', 'iva', 'tari', 'imu', 'tassa', 'tasse', 'f24', 'imposta', 'agenzia entrate',
        'ritenuta', 'diritto camerale', 'bollo', 'imposta da bollo', 'sanzione', 'tributo'
    ]),
    ('Bollette e utenze', [
        'luce', 'gas', 'acqua bnb', 'aqua bnb', 'acqua b&b', 'enel', 'eni', 'utenz', 'bollett',
        'telefono', 'internet', 'wifi', 'wi fi', 'tim', 'wind', 'iliad', 'vodafone', 'energia',
        'publiacqua', 'lumina', 'fibra', 'rete'
    ]),
    ('Affitti e abbonamenti', [
        'affitto', 'rent ', ' rent', 'locazione', 'canone locazione', 'canone affitto',
        'bnb camaldoli', 'reburger camaldoli', 'villani', 'spinza affitto'
    ]),
    ('Servizi finanziari', [
        'nexi', 'commissione nexi', 'nexi commissione', 'commissioni nexi', 'oneri commissioni',
        'banca', 'bonifico', 'interessi', 'sumup', 'transaz', 'carta', 'conto corrente',
        'canone bancario', 'qonto', 'rata qonto', 'qonto subscription', 'subscription qonto'
    ]),
    ('Servizi piattaforme', [
        'the fork', 'fork pay', 'thefork', 'forkpay', 'just eat fee', 'glovo fee', 'deliveroo fee', 'commissione glovo', 'commissioni glovo', 'commissione deliveroo', 'commissioni deliveroo', 'commissione just eat', 'commissioni just eat', 'promo glovo', 'promo deliveroo'
    ]),
    ('Marketing', [
        'social', 'social media', 'ads', 'marketing', 'pubblic', 'sponsorizz', 'meta', 'instagram',
        'insta', 'followers', 'verification', 'facebook', 'google ads', 'tiktok', 'volantini',
        'grafica', 'the florentine', 'florentine'
    ]),
    ('Packaging', [
        'packaging', 'cartoni', 'cartone', 'vaschette', 'buste', 'sacchetti', 'tovagliol',
        'posate', 'bicchieri', 'contenitori', 'etichette', 'lecure packaging', 'packaging lecure',
        'scatole', 'scatola', 'take away', 'takeaway', 'monouso', 'coperchi', 'porta pinza', 'porta pizza'
    ]),
    ('Manutenzione e attrezzature', [
        'ripar', 'manut', 'attrezz', 'guasto', 'tecnic', 'frigo', 'freezer', 'lavastoviglie',
        'impianto', 'idraulic', 'elettric', 'macchinario', 'forno rotto', 'brico', 'ferramenta',
        'vetraio', 'cappa', 'impastatrice', 'passaggio motorino', 'motorino', 'mini pinner',
        'pinner', 'nastrocolor', 'amazon', 'atollo', 'utensile', 'ricambio', 'materiale tecnico'
    ]),
    ('Materie prime', [
        'mozzarella', 'farina', 'pomodor', 'salume', 'salumi', 'prosciutto', 'salsiccia', 'nduja',
        'verdure', 'ortofrutta', 'ortolano', 'frutta', 'lattuga', 'rucola', 'funghi', 'cipolle',
        'basilico', 'ingredient', 'materie', 'materia prima', 'latte', 'latticini', 'caseificio',
        'bufala', 'pecorino', 'grana', 'parmigiano', 'stracciatella', 'olio', 'tonno', 'acciughe',
        'macellaio', 'macelleria', 'carne', 'pollo', 'bovino', 'suino', 'buns', 'pane', 'forno',
        'panificio', 'molino', 'caffe', 'caffè', 'coffee', 'dolci', 'dessert', 'gelato', 'cotto',
        'metro', 'metro cure', 'metro le cure', 'cheque metro', 'fornit', 'fornitore', 'fornitori',
        'sapori di toscana', 'sapori toscana', 'sapori', 'toscana sapori', 'sogegross', 'sogergross',
        'socialgros', 'socialgross', 'social gros', 'social gross', 'carrefour', 'esselunga', 'coop',
        'coop cure', 'conad', 'lidl', 'aldi', 'pam', 'makro', 'ce di', 'cedi', 'cash and carry',
        'bevande', 'bibite', 'drink', 'soft drink', 'birra', 'birre', 'ichnusa', 'vino', 'vini',
        'acqua golden', 'aqua golden', 'golden italia', 'acqua bottiglia', 'acqua naturale',
        'acqua frizzante', 'prinz', 'prinz beverage', 'kombucha', 'kombucha legendari', 'legendari',
        'leggendari', 'icaro', 'coca cola', 'cocacola', 'coca-cola', 'fanta', 'sprite', 'pepsi',
        'red bull', 'san pellegrino', 'sanpellegrino'
    ]),
    ('Delivery e logistica', [
        'delivery', 'glovo rimborso', 'deliveroo rimborso', 'just eat rimborso', 'justeat rimborso',
        'logistic', 'trasporto', 'benzina', 'carburante', 'corriere', 'spedizione', 'parcheggio'
    ]),
    ('Pulizie e consumo interno', [
        'detersiv', 'pulizia', 'sanificant', 'carta mani', 'scottex', 'sapone', 'sgrassatore',
        'candeggina', 'igiene', 'guanti', 'rotoloni', 'bobine', 'carta igienica', 'lavaggio', 'detergente'
    ]),
    ('Da verificare / movimento interno', [
        'return on investment', 'investimento', 'roi', 'movimento interno', 'giroconto'
    ]),
    ('Movimenti cassa', [
        'coins', 'coin', 'monete', 'spicci', 'cambio cassa', 'resto', 'fondo cassa'
    ]),
    ('Spese secondarie', [
        'secondar', 'varie', 'cinese', 'bit', 'altro', 'spesa piccola'
    ]),
]


def _clean_expense_candidate(text: str) -> str:
    s = str(text or '')
    s = re.sub(r'\b(import\s*txt|txt|uscita|spesa|spese)\b', ' ', s, flags=re.I)
    s = re.sub(r'€?\s*\d+(?:[\.,]\d{1,2})?\s*€?', ' ', s)
    s = re.sub(r'[|:;,_/\\()\[\]{}]+', ' ', s)
    return re.sub(r'\s+', ' ', s).strip()


def _standard_expense_category_names() -> set:
    """Categorie contenitore: non devono guidare la riclassificazione."""
    names = {
        'stipendi', 'materie prime', 'manutenzione e attrezzature', 'professionisti',
        'bollette e utenze', 'affitti e abbonamenti', 'servizi finanziari',
        'servizi piattaforme', 'marketing', 'packaging', 'tasse', 'delivery e logistica',
        'pulizie e consumo interno', 'da verificare movimento interno', 'movimenti cassa',
        'spese secondarie'
    }
    try:
        for fam, _ in _EXPENSE_RULES:
            names.add(_normalize_signature(fam))
    except Exception:
        pass
    return names


def _category_is_only_container(category: str) -> bool:
    norm = _normalize_signature(category)
    return (not norm) or norm in _GENERIC_EXPENSE_CATEGORIES or norm in _standard_expense_category_names()


def _is_income_or_summary_note_part(part: str) -> bool:
    """Riconosce le righe di contesto del giorno che NON sono la spesa specifica."""
    text = str(part or '').strip()
    if not text:
        return True
    norm = _normalize_signature(text)
    if not norm:
        return True
    try:
        if _should_skip_import_amount_line(text):
            return True
    except Exception:
        pass
    label = re.split(r"[0-9]", text, 1)[0].strip(' :-–—')
    try:
        if _normalize_import_payment_method(label):
            return True
    except Exception:
        pass
    if re.match(r'^(pos|cash|qcash|contanti|deliveroo|glovo|just\s*eat|justeat|scuola|satispay|paypal)\b', norm):
        return True
    if norm.startswith(('fondo cassa', 'fondo ', 'total ', 'totale ', 'avg ', 'media ')):
        return True
    if 'total sales' in norm or 'avg day' in norm or 'media giorno' in norm:
        return True
    return False


def _clean_expense_notes_for_rules(notes: str) -> str:
    parts = re.split(r'\s*\|\s*|\n+', str(notes or ''))
    kept = []
    seen = set()
    for part in parts:
        clean = re.sub(r'\s+', ' ', str(part or '').replace('\u00a0', ' ')).strip()
        if not clean or _is_income_or_summary_note_part(clean):
            continue
        key = _normalize_signature(clean)
        if key in seen:
            continue
        seen.add(key)
        kept.append(clean)
    return ' | '.join(kept[:6])


def _expense_text_for_rules(row, *, strip_paid_by: bool = True) -> str:
    """Testo contabile usato dalle regole automatiche.

    La categoria vecchia non deve trascinare il risultato. Se una spesa era finita
    in "Movimenti cassa", ricostruisco la categoria da fornitore + riga originale pulita.
    """
    category_part = str(row.get('category') or '').strip()
    if _category_is_only_container(category_part):
        category_part = ''
    supplier_part = str(row.get('supplier') or '').strip()
    notes_part = _clean_expense_notes_for_rules(str(row.get('notes') or ''))
    text = ' '.join([category_part, supplier_part, notes_part])
    if strip_paid_by:
        # Chi ha pagato non decide la categoria: "Metro paid by Amza" resta Materie prime.
        text = re.sub(r"\b(paid by|pagato da|pagata da|pagato con|pagata con|by)\s+[A-Za-zÀ-ÿ0-9_ .'-]+", ' ', text, flags=re.I)
    return re.sub(r'\s+', ' ', text).strip()


def _expense_primary_text(row) -> str:
    """Solo voce principale, senza note di contesto: utile per Movimenti cassa / giroconti."""
    category_part = str(row.get('category') or '').strip()
    if _category_is_only_container(category_part):
        category_part = ''
    supplier_part = str(row.get('supplier') or '').strip()
    if supplier_part:
        return supplier_part
    return category_part or _clean_expense_notes_for_rules(str(row.get('notes') or ''))


def _looks_like_employee_name(row) -> bool:
    text = _expense_text_for_rules(row)
    norm_joined = _normalize_signature(text)

    if any(hint in norm_joined for hint in _EMPLOYEE_WORD_HINTS):
        return True

    for raw in [_expense_primary_text(row), _clean_expense_notes_for_rules(str(row.get('notes') or ''))]:
        candidate = _clean_expense_candidate(raw)
        norm = _normalize_signature(candidate)
        if not norm:
            continue
        tokens = [t for t in norm.split() if t]
        if len(tokens) > 5:
            continue
        if any(t in _KNOWN_EMPLOYEE_NAMES for t in tokens):
            return True
        if any(t in _NON_EMPLOYEE_SINGLE_WORDS for t in tokens):
            continue
        # Non trasformo qualunque parola sconosciuta in stipendio: riduce errori con fornitori nuovi.
    return False


_MATERIE_PRIME_STRONG_HINTS = (
    'sapori di toscana', 'sapori toscana', 'sogegross', 'sogergross', 'socialgros', 'socialgross',
    'metro', 'metro cure', 'metro le cure', 'carrefour', 'esselunga', 'coop cure', 'coop', 'conad',
    'forno spinza', 'forno', 'panificio', 'molino', 'macellaio', 'macelleria', 'carne', 'buns',
    'caffe', 'caffè', 'coffee', 'aqua golden', 'acqua golden', 'golden italia', 'prinz', 'kombucha',
    'legendari', 'leggendari', 'icaro', 'bevande', 'bibite', 'birra', 'birre', 'ichnusa',
    'caseificio', 'latticini', 'molino', 'ortofrutta', 'ortolano', 'fornitore', 'fornitori',
    'materia prima', 'materie prime', 'cotto', 'salumi', 'salume', 'prosciutto', 'mozzarella',
    'bufala', 'pomodoro', 'verdure', 'funghi'
)



# Regole contabili molto forti: prima di qualsiasi categoria generica.
# Queste sono pensate per i tuoi appunti reali: fornitori food/bibite in Materie prime,
# nomi/paghe in Stipendi, parole tecniche in utenze/commissioni/ecc.
_STRICT_CATEGORY_RULES = [
    ('Materie prime', [
        # fornitori food/bibite ricorrenti
        'sapori di toscana', 'sapori toscana', 'sogegross', 'sogergross', 'socialgros', 'socialgross',
        'social gros', 'social gross', 'metro', 'metro cure', 'metro le cure', 'cheque metro', 'makro',
        'carrefour', 'esselunga', 'coop', 'coop cure', 'conad', 'lidl', 'aldi', 'pam', 'cash and carry',
        'icaro', 'prinz', 'kombucha', 'legendari', 'leggendari', 'golden italia', 'aqua golden', 'acqua golden',
        # fornitori/ingredienti
        'forno', 'forno spinza', 'panificio', 'molino', 'farina', 'macellaio', 'macelleria', 'carne', 'buns',
        'caffe', 'caffè', 'coffee', 'cash coffee', 'bibite', 'bevande', 'soft drink', 'birra', 'birre', 'drink',
        'coca cola', 'cocacola', 'coca-cola', 'fanta', 'sprite', 'red bull', 'san pellegrino', 'sanpellegrino',
        'caseificio', 'latticini', 'mozzarella', 'bufala', 'pecorino', 'grana', 'parmigiano', 'stracciatella',
        'salumi', 'salume', 'prosciutto', 'cotto', 'salsiccia', 'nduja', 'pomodoro', 'verdure', 'funghi',
        'ortofrutta', 'ortolano', 'olio', 'dolci', 'dessert', 'materia prima', 'materie prime'
    ]),
    ('Stipendi', [
        'stipendio', 'stipendi', 'salario', 'salari', 'busta paga', 'paghe dipendenti', 'dipendente',
        'paga', 'paghe', 'personale', 'collaborazione', 'collaboratore', 'straordinari',
        'anticipo', 'acconto stipendio', 'extra sala', 'extra cucina', 'extra marzo', 'extra aprile',
        'consegna', 'turno', 'turni', 'ore lavoro', 'alberghiera',
        # nomi/persone usate nei tuoi appunti: se compaiono come voce principale, sono personale
        'jess', 'samuele', 'boubou', 'angelica', 'aneglica', 'miriam', 'renis', 'alisa', 'alex', 'amza',
        'coleschi', 'lorenzo', 'elio', 'stefano', 'mira', 'giulia', 'lipo', 'niccolo', 'nicolo', 'mattia',
        'mohamed', 'youssef', 'hamza'
    ]),
    ('Affitti e abbonamenti', [
        'affitto', 'affitti', 'rent', 'locazione', 'canone locazione', 'canone affitto', 'rent spinza',
        'affitto bnb', 'affitto reburger', 'affitto villani', 'rent villani'
    ]),
    ('Servizi finanziari', [
        'nexi', 'sumup', 'qonto', 'rata qonto', 'oneri commissioni', 'commissione', 'commissioni',
        'banca', 'bonifico', 'canone bancario', 'transazione', 'transazioni'
    ]),
    ('Bollette e utenze', [
        'luce', 'gas', 'acqua bnb', 'aqua bnb', 'acqua b&b', 'lumina', 'enel', 'eni', 'wifi', 'wi fi',
        'vodafone', 'tim', 'wind', 'iliad', 'telefono', 'internet', 'fibra', 'utenza', 'utenze', 'bolletta', 'bollette'
    ]),
    ('Professionisti', [
        'consulente', 'commercialista', 'consulente del lavoro', 'paghe', 'cedolino', 'architect', 'architetto',
        'chiara architect', 'geometra', 'avvocato', 'notaio', 'studio professionale'
    ]),
    ('Marketing', [
        'social media', 'social', 'instagram', 'insta', 'followers', 'verification', 'the florentine',
        'florentine', 'ads', 'sponsorizzata', 'sponsorizzate', 'pubblicita', 'pubblicità', 'marketing'
    ]),
    ('Packaging', [
        'packaging', 'cartoni', 'cartone', 'vaschette', 'buste', 'sacchetti', 'tovaglioli', 'bicchieri',
        'contenitori', 'scatole', 'scatola', 'monouso', 'coperchi', 'take away', 'takeaway',
        'packaging lecure', 'packaging le cure'
    ]),
    ('Manutenzione e attrezzature', [
        'amazon', 'brico', 'ferramenta', 'vetraio', 'cappa', 'impastatrice', 'motorino', 'mini pinner',
        'pinner', 'nastrocolor', 'riparazione', 'manutenzione', 'attrezzatura', 'attrezzature', 'utensile',
        'ricambio', 'frigo', 'freezer', 'lavastoviglie'
    ]),
    ('Tasse', [
        'imposta da bollo', 'imposta', 'bollo', 'f24', 'iva', 'inps', 'tari', 'imu', 'tassa', 'tasse', 'tributo'
    ]),
    ('Servizi piattaforme', [
        'the fork pay', 'the fork', 'thefork', 'fork pay', 'forkpay'
    ]),
    ('Spese secondarie', [
        'cinese', 'bit'
    ]),
]


def _match_strict_category_from_text(text: str) -> str:
    """Categoria forte basata sulla singola riga/voce, non sul blocco del giorno.

    Nota: i nomi di persona vengono controllati DOPO i fornitori/parole operative.
    Così "Alisa social media" resta Marketing e "Rent Villani Amza" resta Affitti.
    """
    norm = _normalize_signature(text)
    if not norm:
        return ''
    # Prima tutte le categorie operative, esclusi i nomi/stipendi.
    for category, keywords in _STRICT_CATEGORY_RULES:
        if category == 'Stipendi':
            continue
        for kw in keywords:
            if _keyword_matches_normalized(norm, kw):
                return category
    # Poi il personale.
    for category, keywords in _STRICT_CATEGORY_RULES:
        if category != 'Stipendi':
            continue
        for kw in keywords:
            if _keyword_matches_normalized(norm, kw):
                return category
    return ''


def _extract_learning_pattern_from_supplier(supplier: str, notes: str = '') -> str:
    """Crea una chiave riutilizzabile per imparare correzioni manuali: Metro Cure -> metro, Sogergross Le Cure -> sogergross."""
    raw = str(supplier or '').strip() or str(notes or '').strip()
    raw = re.sub(r'€?\s*\d+(?:[\.,]\d{1,2})?\s*€?', ' ', raw)
    raw = re.sub(r'\b(paid by|pagato da|pagata da|by)\b.*$', ' ', raw, flags=re.I)
    norm = _normalize_signature(raw)
    # tolgo parole che sono solo sede/contesto e non il fornitore
    stop = {
        'spinza', 'reburger', 'camaldoli', 'palazzuolo', 'palazzuoli', 'cure', 'lecure', 'le', 'la', 'il',
        'atollo', 'villani', 'bnb', 'foscolo', 'marzo', 'aprile', 'febbraio', 'gennaio', 'maggio', 'giugno',
        'luglio', 'agosto', 'settembre', 'ottobre', 'novembre', 'dicembre', 'paid', 'by', 'amza'
    }
    tokens = [t for t in norm.split() if t and t not in stop]
    if not tokens:
        return norm[:80]
    joined = ' '.join(tokens)
    for phrase in ['sapori di toscana', 'social gross', 'social gros', 'soge gross', 'the fork', 'the florentine', 'cash coffee']:
        pn = _normalize_signature(phrase)
        if pn in joined:
            return pn
    return ' '.join(tokens[:2])[:80]


def _ensure_cash_expense_category_rules_table(cur):
    """Tabella piccola per far imparare al gestionale le correzioni fatte con Sposta."""
    try:
        if using_postgres():
            cur.execute("""
                CREATE TABLE IF NOT EXISTS cash_expense_category_rules (
                    id SERIAL PRIMARY KEY,
                    store TEXT NOT NULL DEFAULT 'ALL',
                    pattern TEXT NOT NULL DEFAULT '',
                    pattern_norm TEXT NOT NULL DEFAULT '',
                    category TEXT NOT NULL DEFAULT '',
                    is_active INTEGER NOT NULL DEFAULT 1,
                    created_by TEXT NOT NULL DEFAULT 'system',
                    ts TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            """)
        else:
            cur.execute("""
                CREATE TABLE IF NOT EXISTS cash_expense_category_rules (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    store TEXT NOT NULL DEFAULT 'ALL',
                    pattern TEXT NOT NULL DEFAULT '',
                    pattern_norm TEXT NOT NULL DEFAULT '',
                    category TEXT NOT NULL DEFAULT '',
                    is_active INTEGER NOT NULL DEFAULT 1,
                    created_by TEXT NOT NULL DEFAULT 'system',
                    ts TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            """)
    except Exception:
        pass


def _load_learned_expense_rules(cur, store: str = 'ALL') -> list[dict]:
    try:
        _ensure_cash_expense_category_rules_table(cur)
        ph = _ph()
        rows = _dict_rows(
            cur,
            f"SELECT pattern_norm, category FROM cash_expense_category_rules WHERE is_active=1 AND (store={ph} OR store='ALL') ORDER BY LENGTH(pattern_norm) DESC, id DESC",
            (store or 'ALL',),
        )
        return [r for r in rows if r.get('pattern_norm') and r.get('category')]
    except Exception:
        return []


def _match_learned_category(row, rules: list[dict] | None = None, cur=None, store: str = 'ALL') -> str:
    rules = rules if rules is not None else (_load_learned_expense_rules(cur, store) if cur is not None else [])
    if not rules:
        return ''
    text = _normalize_signature(_expense_primary_text(row) + ' ' + _expense_text_for_rules(row))
    if not text:
        return ''
    for rule in rules:
        pat = str(rule.get('pattern_norm') or '').strip()
        if pat and _keyword_matches_normalized(text, pat):
            return str(rule.get('category') or '').strip()
    return ''


def _save_learned_expense_rule(cur, *, store: str, supplier: str, notes: str, category: str, username: str):
    pattern = _extract_learning_pattern_from_supplier(supplier, notes)
    pattern_norm = _normalize_signature(pattern)
    category = str(category or '').strip()
    if not pattern_norm or not category:
        return pattern_norm
    _ensure_cash_expense_category_rules_table(cur)
    ph = _ph()
    try:
        existing = cur.execute(
            f"SELECT id FROM cash_expense_category_rules WHERE store={ph} AND pattern_norm={ph} LIMIT 1",
            (store or 'ALL', pattern_norm),
        ).fetchone()
        if existing:
            rid = dict(existing).get('id') if not isinstance(existing, tuple) else existing[0]
            cur.execute(f"UPDATE cash_expense_category_rules SET category={ph}, is_active=1 WHERE id={ph}", (category, rid))
        else:
            cur.execute(
                f"INSERT INTO cash_expense_category_rules(store, pattern, pattern_norm, category, is_active, created_by) VALUES({ph},{ph},{ph},{ph},1,{ph})",
                (store or 'ALL', pattern, pattern_norm, category, username or 'system'),
            )
    except Exception:
        pass
    return pattern_norm


def _apply_category_to_similar_expenses(cur, *, brand: str, pattern_norm: str, new_category: str) -> int:
    """Aggiorna i vecchi dati simili senza toccare importi, date o note."""
    if not pattern_norm or not new_category:
        return 0
    ph = _ph()
    where = ''
    params = []
    if brand and brand != 'ALL' and brand in STORES:
        where = f' WHERE store={ph}'
        params.append(brand)
    rows = _dict_rows(cur, f"SELECT id, supplier, notes FROM cash_expenses{where}", tuple(params))
    changed = 0
    for row in rows:
        text = _normalize_signature(str(row.get('supplier') or '') + ' ' + str(row.get('notes') or ''))
        if _keyword_matches_normalized(text, pattern_norm):
            cur.execute(f"UPDATE cash_expenses SET category={ph} WHERE id={ph}", (new_category, int(row.get('id'))))
            changed += 1
    return changed

def _keyword_matches_normalized(normalized_text: str, keyword: str) -> bool:
    norm_k = _normalize_signature(keyword)
    if not norm_k:
        return False
    # Parole corte/frasi: match intero per evitare falsi positivi, es. RENIS non deve diventare ENI.
    if len(norm_k) <= 4 or ' ' in norm_k or norm_k in {'rent', 'eni', 'tim', 'bit', 'pos', 'coin', 'coins'}:
        return re.search(r'(?<![a-z0-9])' + re.escape(norm_k) + r'(?![a-z0-9])', normalized_text) is not None
    return norm_k in normalized_text


def _has_any_normalized(text: str, keywords) -> bool:
    norm = _normalize_signature(text)
    return any(_keyword_matches_normalized(norm, k) for k in keywords if k)


def _is_strong_materie_prime(row) -> bool:
    text = _expense_text_for_rules(row)
    return _has_any_normalized(text, _MATERIE_PRIME_STRONG_HINTS)


def _is_exact_or_near_cash_movement(row) -> bool:
    # Solo se la voce principale è davvero coins/monete/cambio/resto/fondo cassa.
    # Non guardo le note del giorno, perché contengono spesso "Fondo cassa 5€".
    text = _normalize_signature(_expense_primary_text(row))
    if not text:
        return False
    tokens = set(text.split())
    cash_tokens = {'coins', 'coin', 'monete', 'spicci', 'cambio', 'cassa', 'resto', 'fondo'}
    if tokens and tokens.issubset(cash_tokens) and any(t in tokens for t in {'coins', 'coin', 'monete', 'spicci', 'cambio', 'resto', 'fondo'}):
        return True
    return text in {'coins', 'coin', 'monete', 'spicci', 'cambio cassa', 'resto', 'resto cassa', 'fondo cassa'}


def _is_exact_or_near_internal_movement(row) -> bool:
    primary = _normalize_signature(_expense_primary_text(row))
    text = _normalize_signature(_expense_text_for_rules(row))
    cleaned = re.sub(r'\b(import|txt|uscita|spesa|spese|le|la|il|di|da)\b', ' ', primary)
    cleaned = re.sub(r'\s+', ' ', cleaned).strip()
    tokens = {t for t in cleaned.split() if t}
    if tokens and tokens.issubset({'spinza', 'reburger'}):
        return True
    return any(k in text for k in ['return on investment', 'roi', 'giroconto', 'movimento interno'])


def _rule_family_match(row):
    normalized_text = _normalize_signature(_expense_text_for_rules(row))
    for family, keywords in _EXPENSE_RULES:
        # Movimenti cassa viene deciso solo dalla voce principale, non dalle note del giorno.
        if family == 'Movimenti cassa':
            if _is_exact_or_near_cash_movement(row):
                return family
            continue
        if any(_keyword_matches_normalized(normalized_text, k) for k in keywords):
            return family
    return ''


def _expense_family(row) -> str:
    # 1) Movimenti cassa solo se la singola voce lo dice davvero.
    if _is_exact_or_near_cash_movement(row):
        return 'Movimenti cassa'
    if _is_exact_or_near_internal_movement(row):
        return 'Da verificare / movimento interno'

    # 2) Prima regole forti sulla singola riga/fornitore: evita che Materie prime resti vuota.
    strict = _match_strict_category_from_text(_expense_primary_text(row)) or _match_strict_category_from_text(_expense_text_for_rules(row))
    if strict:
        # Se è paid by Amza, Amza viene già tolto dal testo regole.
        return strict

    # 3) Regole classiche.
    family = _rule_family_match(row)
    if family:
        return family

    # 4) Nomi persona / extra / stipendi.
    if _looks_like_employee_name(row):
        return 'Stipendi'

    category = str(row.get('category') or '').strip()
    if _category_is_only_container(category):
        return 'Spese secondarie'
    return _title_case_words(category) if category else 'Spese secondarie'

def _auto_expense_category(category: str = '', supplier: str = '', notes: str = '') -> str:
    return _expense_family({'category': category or '', 'supplier': supplier or '', 'notes': notes or ''})


def _is_strong_auto_category(row, new_category: str) -> bool:
    if not new_category:
        return False
    if new_category in {'Movimenti cassa', 'Da verificare / movimento interno'}:
        return _is_exact_or_near_cash_movement(row) or _is_exact_or_near_internal_movement(row)
    if new_category == 'Stipendi':
        return _looks_like_employee_name(row)
    if new_category == 'Materie prime':
        return _is_strong_materie_prime(row)
    return _rule_family_match(row) == new_category


def _should_auto_replace_expense_category(category: str, supplier: str = '', notes: str = '') -> bool:
    norm_cat = _normalize_signature(category)
    row = {'category': category, 'supplier': supplier, 'notes': notes}
    new_category = _auto_expense_category(category, supplier, notes)

    # Le categorie generiche/provvisorie vanno ricalcolate senza toccare importi o date.
    if norm_cat in _GENERIC_EXPENSE_CATEGORIES:
        return True

    # Rimedio dati vecchi: correggo solo quando la nuova categoria è riconosciuta da regole forti.
    if _normalize_signature(new_category) != norm_cat and _is_strong_auto_category(row, new_category):
        return True

    return False


def _recategorize_existing_cash_expenses(scope_store: str = 'ALL') -> int:
    """Rimedia i dati già inseriti: aggiorna solo la categoria delle vecchie uscite, senza cancellare o azzerare importi."""
    updated = 0
    ph = _ph()
    try:
        with connect() as conn:
            cur = conn.cursor()
            where = ''
            params = []
            if scope_store and scope_store != 'ALL' and scope_store in STORES:
                where = f' WHERE store={ph}'
                params.append(scope_store)
            rules = _load_learned_expense_rules(cur, scope_store or 'ALL')
            rows = _dict_rows(cur, f'SELECT id, category, supplier, notes FROM cash_expenses{where}', tuple(params))
            for row in rows:
                current = str(row.get('category') or '').strip()
                supplier = str(row.get('supplier') or '').strip()
                notes = str(row.get('notes') or '').strip()
                learned = _match_learned_category({'category': current, 'supplier': supplier, 'notes': notes}, rules=rules)
                new_category = learned or _auto_expense_category(current, supplier, notes)
                if not new_category or _normalize_signature(new_category) == _normalize_signature(current):
                    continue
                # Le regole imparate e le regole forti possono correggere anche vecchie categorie sbagliate.
                if learned or _should_auto_replace_expense_category(current, supplier, notes):
                    cur.execute(f'UPDATE cash_expenses SET category={ph} WHERE id={ph}', (new_category, int(row.get('id'))))
                    updated += 1
    except Exception as e:
        print('[WARN] Ricategorizzazione uscite non completata:', e)
    return updated


def _repair_existing_cash_entries_from_notes(scope_store: str = 'ALL') -> int:
    """Corregge vecchi import dove righe tipo "Glovo 87€ (23€ cash)" erano state lette male.
    Lavora per giornata: se trova la riga Total, decide se salvare delivery lordo o al netto del cash in parentesi.
    """
    updated = 0
    ph = _ph()

    def extract_raw_for_method(notes: str, method: str, original_method: str):
        parts = re.split(r'\s*\|\s*|\n+', notes or '')
        method_tokens = {method, str(original_method or '').strip().lower()}
        if method == 'contanti':
            method_tokens.update({'cash', 'qcash', 'q cash', 'contanti'})
        if method == 'just eat':
            method_tokens.update({'justeat', 'just eat'})
        for part in parts:
            norm = _normalize_import_text_for_matching(part)
            label = re.split(r"[0-9]", part or '', 1)[0].strip(' :-–—')
            detected = _normalize_import_payment_method(label)
            if detected and detected == method:
                return part
            if detected:
                continue
            # fallback solo per vecchie note molto pulite, evitando fornitori tipo "Cash coffee".
            if any(norm == tok or norm.startswith(tok + ' ') and len(norm.split()) <= 3 for tok in method_tokens if tok):
                return part
        return ''

    def extract_declared_total(notes_list):
        for notes in notes_list:
            for part in re.split(r'\s*\|\s*|\n+', notes or ''):
                norm = _normalize_import_text_for_matching(part)
                if norm.startswith(('total', 'totale')) and 'sales' not in norm:
                    amount = _parse_flexible_amount(part)
                    if amount is not None:
                        return float(amount)
        return None

    try:
        with connect() as conn:
            cur = conn.cursor()
            where = ''
            params = []
            if scope_store and scope_store != 'ALL' and scope_store in STORES:
                where = f' WHERE store={ph}'
                params.append(scope_store)
            rows = _dict_rows(cur, f'SELECT id, store, flow_date, payment_method, amount, notes FROM cash_entries{where}', tuple(params))
            grouped = {}
            for row in rows:
                key = (str(row.get('store') or ''), str(row.get('flow_date') or ''))
                grouped.setdefault(key, []).append(row)

            for key, day_rows in grouped.items():
                declared = extract_declared_total([str(r.get('notes') or '') for r in day_rows])
                prepared = []
                for row in day_rows:
                    original_method = str(row.get('payment_method') or '').strip()
                    method = _normalize_import_payment_method(original_method) or original_method.lower()
                    raw_line = extract_raw_for_method(str(row.get('notes') or ''), method, original_method)
                    parsed = None
                    cash_part = 0.0
                    if raw_line:
                        parsed = _parse_income_amount(raw_line, method)
                        if method in {'glovo', 'deliveroo', 'just eat'}:
                            cash_part = _cash_component_inside_parentheses(raw_line)
                    prepared.append({
                        'row': row,
                        'method': method,
                        'raw_line': raw_line,
                        'gross_amount': float(parsed if parsed is not None else (row.get('amount') or 0)),
                        'cash_part': float(cash_part or 0),
                    })
                gross = sum(x['gross_amount'] for x in prepared)
                net = gross - sum(x['cash_part'] for x in prepared)
                subtract_cash = False
                if declared is not None and sum(x['cash_part'] for x in prepared) > 0:
                    subtract_cash = abs(net - declared) + 0.01 < abs(gross - declared)
                for item in prepared:
                    row = item['row']
                    new_amount = item['gross_amount']
                    if subtract_cash and item['cash_part'] > 0:
                        new_amount = max(0.0, round(new_amount - item['cash_part'], 2))
                    old_amount = float(row.get('amount') or 0)
                    if abs(old_amount - float(new_amount)) > 0.009:
                        cur.execute(f'UPDATE cash_entries SET amount={ph} WHERE id={ph}', (float(new_amount), int(row.get('id'))))
                        updated += 1
    except Exception as e:
        print('[WARN] Riparazione incassi non completata:', e)
    return updated


def _expense_is_fixed(label: str, family: str) -> bool:
    text = f"{label} {family}".lower()
    return any(k in text for k in ['affitto', 'canone', 'abbon', 'stipend', 'personale', 'commercialist', 'consulent', 'nexi', 'qonto', 'internet', 'telefono', 'luce', 'gas', 'acqua'])


def _expense_signature(row) -> tuple[str, str, str]:
    category = str(row.get('category') or '').strip()
    supplier = str(row.get('supplier') or '').strip()
    notes = str(row.get('notes') or '').strip()
    family = _expense_family(row)
    preferred = supplier or category or notes or 'Spesa varia'
    label = preferred
    if supplier and category and _normalize_signature(supplier) != _normalize_signature(category):
        label = f'{supplier} · {category}'
    elif category and not supplier:
        label = category
    elif supplier:
        label = supplier
    if not label:
        label = 'Spesa varia'
    signature = _normalize_signature(f'{supplier} {category}') or _normalize_signature(label) or 'spesa varia'
    return signature, label, family


def _build_expense_overview(cur, scope_store: str, period_type: str = 'month', anchor_s: str = ''):
    anchor_day = _parse_date_safe(anchor_s, date.today())
    period_type, start_d, end_d = _period_bounds(period_type, anchor_day)
    start_s = start_d.isoformat()
    end_s = end_d.isoformat()
    where_sql, params = _cash_scope_where(scope_store)
    ph = _ph()
    rows = _dict_rows(cur, f"SELECT id, flow_date, store, category, supplier, payment_method, amount, notes FROM cash_expenses WHERE {where_sql} ORDER BY flow_date ASC, id ASC", params)

    family_map = {}
    family_detail_map = {}
    period_map = {}
    all_map = {}
    total_period = 0.0

    for row in rows:
        amount = float(row.get('amount') or 0)
        if amount <= 0:
            continue
        flow_date = str(row.get('flow_date') or '')
        month_key = flow_date[:7] if len(flow_date) >= 7 else ''
        sig, label, family = _expense_signature(row)

        slot = all_map.setdefault(sig, {
            'label': label,
            'family': family,
            'months': set(),
            'total_all': 0.0,
            'count_all': 0,
        })
        slot['total_all'] += amount
        slot['count_all'] += 1
        if month_key:
            slot['months'].add(month_key)

        if start_s <= flow_date <= end_s:
            total_period += amount
            fam = family_map.setdefault(family, {'name': family, 'total': 0.0, 'count': 0})
            fam['total'] += amount
            fam['count'] += 1
            family_detail_map.setdefault(family, []).append({
                'id': int(row.get('id') or 0),
                'date': flow_date,
                'store': row.get('store') or '',
                'store_label': STORES.get(row.get('store') or '', row.get('store') or ''),
                'category': row.get('category') or '',
                'supplier': row.get('supplier') or '',
                'payment_method': row.get('payment_method') or '',
                'amount': amount,
                'notes': row.get('notes') or '',
            })

            grp = period_map.setdefault(sig, {
                'label': label,
                'family': family,
                'total': 0.0,
                'count': 0,
            })
            grp['total'] += amount
            grp['count'] += 1

    family_rows = sorted(family_map.values(), key=lambda x: (-float(x.get('total') or 0), x.get('name') or ''))
    for idx, row in enumerate(family_rows):
        row['share_pct'] = round((float(row.get('total') or 0) / total_period) * 100.0, 1) if total_period > 0 else 0.0
        row['color'] = PALETTE_DARK[idx % len(PALETTE_DARK)]
        details = sorted(family_detail_map.get(row.get('name') or '', []), key=lambda x: (x.get('date') or '', -float(x.get('amount') or 0)))
        row['details'] = details
        row['detail_total'] = round(sum(float(x.get('amount') or 0) for x in details), 2)
    family_pie_style = _build_conic_gradient(family_rows, total_period)

    routine_rows = []
    recurring_rows = []
    for sig, item in period_map.items():
        meta = all_map.get(sig, {})
        months_count = len(meta.get('months') or set())
        record = {
            'label': item.get('label') or 'Spesa varia',
            'family': item.get('family') or 'Varie',
            'total': float(item.get('total') or 0),
            'count': int(item.get('count') or 0),
            'months_count': months_count,
            'avg_monthly': round((float(meta.get('total_all') or 0) / months_count), 2) if months_count else 0.0,
            'is_fixed': _expense_is_fixed(item.get('label') or '', item.get('family') or ''),
        }
        record['share_pct'] = round((record['total'] / total_period) * 100.0, 1) if total_period > 0 else 0.0
        routine_rows.append(record)
        if months_count >= 2 or record['is_fixed']:
            recurring_rows.append(record.copy())

    routine_rows.sort(key=lambda x: (-x['total'], x['label']))
    recurring_rows.sort(key=lambda x: (-x['total'], x['label']))

    return {
        'period_total': total_period,
        'family_rows': family_rows,
        'family_pie_style': family_pie_style,
        'recurring_rows': recurring_rows[:10],
        'routine_rows': routine_rows[:14],
        'recurring_total': round(sum(x['total'] for x in recurring_rows), 2),
        'routine_grouped_total': round(sum(x['total'] for x in routine_rows), 2),
        'recurring_count': len(recurring_rows),
        'routine_count': len(routine_rows),
        'category_options': _expense_category_options(),
    }

def _build_cash_dashboard(cur, scope_store: str, period_type: str = 'week', anchor_s: str = ''):
    anchor_day = date.today()
    if anchor_s:
        try:
            anchor_day = datetime.strptime(anchor_s, '%Y-%m-%d').date()
        except Exception:
            anchor_day = date.today()
    period_type, start_d, end_d = _period_bounds(period_type, anchor_day)
    prev_start, prev_end = _shift_previous_period(period_type, start_d, end_d)

    where_sql, params = _cash_scope_where(scope_store)
    ph = _ph()
    start_s = start_d.isoformat()
    end_s = end_d.isoformat()
    prev_start_s = prev_start.isoformat()
    prev_end_s = prev_end.isoformat()

    entries = _dict_rows(cur, f"SELECT store, flow_date, SUM(amount) AS total FROM cash_entries WHERE {where_sql} AND flow_date BETWEEN {ph} AND {ph} GROUP BY store, flow_date ORDER BY flow_date ASC", params + (start_s, end_s))
    expenses = _dict_rows(cur, f"SELECT store, flow_date, SUM(amount) AS total FROM cash_expenses WHERE {where_sql} AND flow_date BETWEEN {ph} AND {ph} GROUP BY store, flow_date ORDER BY flow_date ASC", params + (start_s, end_s))

    by_entry = {(r['store'], str(r['flow_date'])): float(r.get('total') or 0) for r in entries}
    by_expense = {(r['store'], str(r['flow_date'])): float(r.get('total') or 0) for r in expenses}

    stores = list(STORES.keys()) if scope_store == 'ALL' else [scope_store]
    compare = []
    days_count = (end_d - start_d).days + 1
    day_list = [start_d + timedelta(days=i) for i in range(days_count)]
    for s in stores:
        days = []
        total_in = 0.0
        total_out = 0.0
        for d in day_list:
            ds = d.isoformat()
            inc = by_entry.get((s, ds), 0.0)
            usc = by_expense.get((s, ds), 0.0)
            net = inc - usc
            total_in += inc
            total_out += usc
            days.append({
                'date': ds,
                'label': d.strftime('%d/%m'),
                'income': inc,
                'expense': usc,
                'net': net,
            })
        max_abs = max([abs(x['net']) for x in days] + [1.0])
        for x in days:
            x['height'] = max(8, int((abs(x['net']) / max_abs) * 120)) if x['net'] != 0 else 8
            x['positive'] = x['net'] >= 0
        compare.append({
            'store': s,
            'label': _store_label(s),
            'days': days,
            'income_total': total_in,
            'expense_total': total_out,
            'net_total': total_in - total_out,
        })

    income_current = _fetch_one_float(cur, f"SELECT COALESCE(SUM(amount),0) FROM cash_entries WHERE {where_sql} AND flow_date BETWEEN {ph} AND {ph}", params + (start_s, end_s))
    expense_current = _fetch_one_float(cur, f"SELECT COALESCE(SUM(amount),0) FROM cash_expenses WHERE {where_sql} AND flow_date BETWEEN {ph} AND {ph}", params + (start_s, end_s))
    income_previous = _fetch_one_float(cur, f"SELECT COALESCE(SUM(amount),0) FROM cash_entries WHERE {where_sql} AND flow_date BETWEEN {ph} AND {ph}", params + (prev_start_s, prev_end_s))
    expense_previous = _fetch_one_float(cur, f"SELECT COALESCE(SUM(amount),0) FROM cash_expenses WHERE {where_sql} AND flow_date BETWEEN {ph} AND {ph}", params + (prev_start_s, prev_end_s))

    totals = {
        'income_today': _fetch_one_float(cur, f"SELECT COALESCE(SUM(amount),0) FROM cash_entries WHERE {where_sql} AND flow_date={ph}", params + (date.today().isoformat(),)),
        'expense_today': _fetch_one_float(cur, f"SELECT COALESCE(SUM(amount),0) FROM cash_expenses WHERE {where_sql} AND flow_date={ph}", params + (date.today().isoformat(),)),
        'income_period': income_current,
        'expense_period': expense_current,
        'net_period': income_current - expense_current,
        'income_previous_period': income_previous,
        'expense_previous_period': expense_previous,
        'net_previous_period': income_previous - expense_previous,
        'income_growth_pct': _pct_change(income_current, income_previous),
        'expense_growth_pct': _pct_change(expense_current, expense_previous),
        'net_growth_pct': _pct_change(income_current - expense_current, income_previous - expense_previous),
        'entries_count': _fetch_one_int(cur, f"SELECT COUNT(*) FROM cash_entries WHERE {where_sql}", params),
        'expenses_count': _fetch_one_int(cur, f"SELECT COUNT(*) FROM cash_expenses WHERE {where_sql}", params),
    }
    totals['net_today'] = totals['income_today'] - totals['expense_today']
    totals['margin_pct'] = round((totals['net_period'] / income_current) * 100.0, 1) if income_current > 0 else 0.0
    totals['cost_incidence_pct'] = round((expense_current / income_current) * 100.0, 1) if income_current > 0 else 0.0

    recent_entries = _dict_rows(cur, f"SELECT id, flow_date, store, payment_method, amount, notes, ts FROM cash_entries WHERE {where_sql} ORDER BY ts DESC, id DESC LIMIT 10", params)
    recent_expenses = _dict_rows(cur, f"SELECT id, flow_date, store, category, supplier, payment_method, amount, notes, ts FROM cash_expenses WHERE {where_sql} ORDER BY ts DESC, id DESC LIMIT 10", params)

    income_breakdown = _dict_rows(
        cur,
        f"SELECT LOWER(TRIM(payment_method)) AS name, COALESCE(SUM(amount),0) AS total FROM cash_entries WHERE {where_sql} AND flow_date BETWEEN {ph} AND {ph} GROUP BY LOWER(TRIM(payment_method)) ORDER BY total DESC, name ASC",
        params + (start_s, end_s),
    )
    raw_expense_rows = _dict_rows(
        cur,
        f"SELECT category, supplier, notes, amount FROM cash_expenses WHERE {where_sql} AND flow_date BETWEEN {ph} AND {ph}",
        params + (start_s, end_s),
    )
    expense_groups = {}
    for erow in raw_expense_rows:
        family = _expense_family(erow)
        slot = expense_groups.setdefault(family, {'name': family, 'total': 0.0, 'count': 0})
        slot['total'] += float(erow.get('amount') or 0)
        slot['count'] += 1
    expense_breakdown = sorted(expense_groups.values(), key=lambda x: (-float(x.get('total') or 0), x.get('name') or ''))

    for idx, row in enumerate(income_breakdown, start=1):
        row['rank'] = idx
        row['label'] = (row.get('name') or 'non specificato').strip() or 'non specificato'
        row['share_pct'] = round((float(row.get('total') or 0) / income_current) * 100.0, 1) if income_current > 0 else 0.0
    for idx, row in enumerate(expense_breakdown, start=1):
        row['rank'] = idx
        row['label'] = (row.get('name') or 'non specificata').strip() or 'non specificata'
        row['share_pct'] = round((float(row.get('total') or 0) / expense_current) * 100.0, 1) if expense_current > 0 else 0.0

    top_income = income_breakdown[0] if income_breakdown else {'label': 'Nessuno', 'total': 0.0, 'share_pct': 0.0}
    top_expense = expense_breakdown[0] if expense_breakdown else {'label': 'Nessuna', 'total': 0.0, 'share_pct': 0.0}
    insights = {
        'top_income_method': top_income,
        'top_expense_category': top_expense,
        'income_breakdown': income_breakdown[:6],
        'expense_breakdown': expense_breakdown[:6],
        'income_methods_count': len(income_breakdown),
        'expense_categories_count': len(expense_breakdown),
    }

    period_meta = {
        'type': period_type,
        'anchor': anchor_day.isoformat(),
        'start': start_s,
        'end': end_s,
        'prev_start': prev_start_s,
        'prev_end': prev_end_s,
        'label': 'giorno' if period_type == 'day' else ('settimana' if period_type == 'week' else 'mese'),
        'month_key': start_s[:7],
        'range_label': _period_range_label(period_type, start_d, end_d),
    }
    return compare, totals, recent_entries, recent_expenses, period_meta, insights


def _build_store_period_chart(cur, store: str, period_type: str = 'week', anchor_s: str = ''):
    anchor_day = date.today()
    if anchor_s:
        try:
            anchor_day = datetime.strptime(anchor_s, '%Y-%m-%d').date()
        except Exception:
            pass
    period_type, start_d, end_d = _period_bounds(period_type, anchor_day)
    ph = _ph()
    start_s = start_d.isoformat()
    end_s = end_d.isoformat()
    entries = _dict_rows(cur, f"SELECT flow_date, SUM(amount) AS total FROM cash_entries WHERE store={ph} AND flow_date BETWEEN {ph} AND {ph} GROUP BY flow_date ORDER BY flow_date ASC", (store, start_s, end_s))
    expenses = _dict_rows(cur, f"SELECT flow_date, SUM(amount) AS total FROM cash_expenses WHERE store={ph} AND flow_date BETWEEN {ph} AND {ph} GROUP BY flow_date ORDER BY flow_date ASC", (store, start_s, end_s))
    by_entry = {str(r['flow_date']): float(r.get('total') or 0) for r in entries}
    by_expense = {str(r['flow_date']): float(r.get('total') or 0) for r in expenses}
    rows = []
    max_amount = 1.0
    days_count = (end_d - start_d).days + 1
    for i in range(days_count):
        d = start_d + timedelta(days=i)
        ds = d.isoformat()
        inc = by_entry.get(ds, 0.0)
        usc = by_expense.get(ds, 0.0)
        max_amount = max(max_amount, inc, usc, abs(inc-usc))
        rows.append({'date': ds, 'label': d.strftime('%d/%m'), 'income': inc, 'expense': usc, 'net': inc-usc})
    for r in rows:
        r['income_h'] = max(8, int((r['income']/max_amount)*130)) if r['income'] else 8
        r['expense_h'] = max(8, int((r['expense']/max_amount)*130)) if r['expense'] else 8
        r['net_h'] = max(8, int((abs(r['net'])/max_amount)*130)) if r['net'] else 8
        r['positive'] = r['net'] >= 0
    return rows


def _parse_date_safe(raw: str, fallback: date | None = None) -> date:
    fallback = fallback or date.today()
    try:
        return datetime.strptime(str(raw or '').strip(), '%Y-%m-%d').date()
    except Exception:
        return fallback


def _shift_anchor_date(period_type: str, anchor_day: date, direction: int) -> date:
    direction = -1 if int(direction) < 0 else 1
    kind = (period_type or 'month').lower()
    if kind == 'day':
        return anchor_day + timedelta(days=direction)
    if kind == 'week':
        return anchor_day + timedelta(days=7 * direction)
    base = anchor_day.replace(day=1)
    month = base.month + direction
    year = base.year
    while month < 1:
        month += 12
        year -= 1
    while month > 12:
        month -= 12
        year += 1
    return date(year, month, 1)


def _period_range_label(period_type: str, start_d: date, end_d: date) -> str:
    kind = (period_type or 'month').lower()
    if kind == 'month':
        return _month_label(start_d.strftime('%Y-%m'))
    if kind == 'day':
        return start_d.strftime('%d/%m/%Y')
    same_year = start_d.year == end_d.year
    left = start_d.strftime('%d/%m')
    right = end_d.strftime('%d/%m/%Y' if not same_year else '%d/%m')
    return f'{left} → {right}'


def _resolve_period_state(period_type: str = 'month', month_key: str = '', anchor_date: str = '', nav: str = ''):
    kind = (period_type or 'month').lower()
    if kind not in {'day', 'week', 'month'}:
        kind = 'month'
    selected_month = _month_key_from_value(month_key or anchor_date or _today_str())
    month_start = _parse_date_safe(selected_month + '-01', date.today().replace(day=1))
    anchor_day = _parse_date_safe(anchor_date, month_start)
    nav = (nav or '').strip().lower()
    if nav in {'prev', 'next'}:
        anchor_day = _shift_anchor_date(kind, anchor_day, -1 if nav == 'prev' else 1)
        selected_month = anchor_day.strftime('%Y-%m')
    elif anchor_day.strftime('%Y-%m') != selected_month:
        anchor_day = month_start
    if kind == 'month':
        anchor_day = anchor_day.replace(day=1)
        selected_month = anchor_day.strftime('%Y-%m')
    return kind, selected_month, anchor_day


def _query_url(path: str, **params) -> str:
    clean = {}
    for key, value in params.items():
        if value is None:
            continue
        if isinstance(value, str) and value == '':
            continue
        clean[key] = value
    query = urlencode(clean)
    return f'{path}?{query}' if query else path


def _month_options_for_scope(cur, scope_store: str, selected_month: str = ''):
    months = {selected_month or date.today().strftime('%Y-%m'), date.today().strftime('%Y-%m')}
    where_sql, params = _cash_scope_where(scope_store)
    for table_name in ('cash_entries', 'cash_expenses'):
        rows = _dict_rows(cur, f"SELECT flow_date FROM {table_name} WHERE {where_sql} ORDER BY flow_date DESC", params)
        for row in rows:
            flow_date = str(row.get('flow_date') or '').strip()
            if len(flow_date) >= 7:
                months.add(flow_date[:7])
    ph = _ph()
    if scope_store == 'ALL':
        report_rows = _dict_rows(cur, 'SELECT month_key FROM sales_report_periods ORDER BY month_key DESC', ())
    else:
        report_rows = _dict_rows(cur, f'SELECT month_key FROM sales_report_periods WHERE store={ph} ORDER BY month_key DESC', (scope_store,))
    for row in report_rows:
        key = str(row.get('month_key') or '').strip()
        if len(key) == 7:
            months.add(key)
    if months:
        first_key = min(months)
        last_key = max(months)
        first_d = _parse_date_safe(first_key + '-01', date.today().replace(month=1, day=1)).replace(day=1)
        last_d = _parse_date_safe(last_key + '-01', date.today().replace(month=12, day=1)).replace(day=1)
    else:
        today = date.today()
        first_d = today.replace(month=1, day=1)
        last_d = today.replace(month=12, day=1)
    if first_d.year == last_d.year:
        first_d = first_d.replace(month=1, day=1)
        last_d = last_d.replace(month=12, day=1)
    options = []
    cursor = first_d
    safety = 0
    while cursor <= last_d and safety < 120:
        key = cursor.strftime('%Y-%m')
        options.append({'key': key, 'label': _month_label(key)})
        safety += 1
        if cursor.month == 12:
            cursor = date(cursor.year + 1, 1, 1)
        else:
            cursor = date(cursor.year, cursor.month + 1, 1)
    options.sort(key=lambda x: x['key'], reverse=True)
    return options[:60]



def _next_month_start(month_key: str) -> date:
    month_key = _month_key_from_value(month_key)
    try:
        start = datetime.strptime(month_key + '-01', '%Y-%m-%d').date()
    except Exception:
        start = date.today().replace(day=1)
    if start.month == 12:
        return date(start.year + 1, 1, 1)
    return date(start.year, start.month + 1, 1)


def _cash_month_summaries(cur, scope_store: str, limit: int | None = 120):
    """Riepilogo mensile di entrate e uscite salvate nel gestionale.

    Il limite predefinito mantiene invariata la home esistente; la pagina storico
    può chiedere l'intera serie passando ``limit=None``.
    """
    where_sql, params = _cash_scope_where(scope_store)
    months = {}

    def slot(month_key: str):
        return months.setdefault(month_key, {
            'key': month_key,
            'label': _month_label(month_key),
            'income_total': 0.0,
            'expense_total': 0.0,
            'income_count': 0,
            'expense_count': 0,
            'stores': set(),
            # Serve alla home: accanto a ogni mese mostro subito come sono divise le uscite.
            'expense_families': {},
        })

    for row in _dict_rows(cur, f"SELECT store, flow_date, amount FROM cash_entries WHERE {where_sql}", params):
        flow_date = str(row.get('flow_date') or '').strip()
        if len(flow_date) < 7:
            continue
        month_key = flow_date[:7]
        item = slot(month_key)
        item['income_total'] += float(row.get('amount') or 0)
        item['income_count'] += 1
        store = str(row.get('store') or '').strip()
        if store:
            item['stores'].add(store)

    for row in _dict_rows(cur, f"SELECT store, flow_date, category, supplier, notes, amount FROM cash_expenses WHERE {where_sql}", params):
        flow_date = str(row.get('flow_date') or '').strip()
        if len(flow_date) < 7:
            continue
        amount = float(row.get('amount') or 0)
        month_key = flow_date[:7]
        item = slot(month_key)
        item['expense_total'] += amount
        item['expense_count'] += 1
        family = _expense_family(row)
        fam = item['expense_families'].setdefault(family, {'name': family, 'total': 0.0, 'count': 0})
        fam['total'] += amount
        fam['count'] += 1
        store = str(row.get('store') or '').strip()
        if store:
            item['stores'].add(store)

    rows = []
    for item in months.values():
        income = float(item.get('income_total') or 0)
        expense = float(item.get('expense_total') or 0)
        item['net_total'] = income - expense
        item['total_count'] = int(item.get('income_count') or 0) + int(item.get('expense_count') or 0)
        item['store_labels'] = ', '.join(_store_label(s) for s in sorted(item.get('stores') or [])) or '—'
        families = sorted((item.get('expense_families') or {}).values(), key=lambda x: (-float(x.get('total') or 0), x.get('name') or ''))
        for fam in families:
            fam['total'] = round(float(fam.get('total') or 0), 2)
            fam['share_pct'] = round((float(fam.get('total') or 0) / expense) * 100.0, 1) if expense > 0 else 0.0
        item['expense_breakdown'] = families[:4]
        item['expense_breakdown_count'] = len(families)
        item['expense_top_family'] = families[0] if families else {'name': '—', 'total': 0.0, 'share_pct': 0.0}
        item['income_total'] = round(income, 2)
        item['expense_total'] = round(expense, 2)
        item['net_total'] = round(item['net_total'], 2)
        # I set/dizionari tecnici non servono al template.
        item.pop('expense_families', None)
        rows.append(item)
    rows.sort(key=lambda x: x.get('key') or '', reverse=True)
    if limit is None:
        return rows
    try:
        safe_limit = max(0, int(limit))
    except Exception:
        safe_limit = 120
    return rows[:safe_limit]


def _month_keys_between(start_key: str, end_key: str, max_months: int = 600):
    """Restituisce mesi YYYY-MM inclusivi, senza dipendere dal database."""
    start_key = _month_key_from_value(start_key)
    end_key = _month_key_from_value(end_key)
    try:
        cursor = datetime.strptime(start_key + '-01', '%Y-%m-%d').date()
        end_d = datetime.strptime(end_key + '-01', '%Y-%m-%d').date()
    except Exception:
        cursor = date.today().replace(day=1)
        end_d = cursor
    if cursor > end_d:
        cursor, end_d = end_d, cursor
    out = []
    safety = 0
    while cursor <= end_d and safety < max(1, int(max_months or 600)):
        out.append(cursor.strftime('%Y-%m'))
        safety += 1
        cursor = _next_month_start(cursor.strftime('%Y-%m'))
    return out


def _cash_month_timeline(cur, scope_store: str, from_month: str = '', to_month: str = ''):
    """Serie mensile continua per la nuova pagina storico/range."""
    raw_rows = _cash_month_summaries(cur, scope_store, limit=None)
    by_key = {str(row.get('key') or ''): dict(row) for row in raw_rows if row.get('key')}
    existing_keys = sorted(by_key)
    today_key = date.today().strftime('%Y-%m')
    available_from = existing_keys[0] if existing_keys else today_key
    available_to = existing_keys[-1] if existing_keys else today_key

    def valid_or(raw, fallback):
        raw = str(raw or '').strip()
        return raw if re.match(r'^\d{4}-\d{2}$', raw) else fallback

    selected_from = valid_or(from_month, available_from)
    selected_to = valid_or(to_month, available_to)
    # Il range resta confinato allo storico realmente disponibile: evita input
    # enormi/accidentali e rende i filtri sempre coerenti con i mesi selezionabili.
    selected_from = max(available_from, min(selected_from, available_to))
    selected_to = max(available_from, min(selected_to, available_to))
    range_swapped = selected_from > selected_to
    if range_swapped:
        selected_from, selected_to = selected_to, selected_from

    all_keys = _month_keys_between(available_from, available_to)
    selected_keys = [k for k in all_keys if selected_from <= k <= selected_to]
    rows = []
    for key in selected_keys:
        row = by_key.get(key)
        if row is None:
            row = {
                'key': key,
                'label': _month_label(key),
                'income_total': 0.0,
                'expense_total': 0.0,
                'net_total': 0.0,
                'income_count': 0,
                'expense_count': 0,
                'total_count': 0,
                'store_labels': '—',
                'expense_breakdown': [],
                'expense_breakdown_count': 0,
                'expense_top_family': {'name': '—', 'total': 0.0, 'share_pct': 0.0},
            }
        row['detail_url'] = _query_url(
            '/gestionale',
            period_type='month',
            month_key=key,
            anchor_date=key + '-01',
        )
        rows.append(row)

    totals = {
        'income_total': round(sum(float(r.get('income_total') or 0) for r in rows), 2),
        'expense_total': round(sum(float(r.get('expense_total') or 0) for r in rows), 2),
        'net_total': 0.0,
        'income_count': sum(int(r.get('income_count') or 0) for r in rows),
        'expense_count': sum(int(r.get('expense_count') or 0) for r in rows),
        'months_count': len(rows),
    }
    totals['net_total'] = round(totals['income_total'] - totals['expense_total'], 2)
    totals['avg_income'] = round(totals['income_total'] / len(rows), 2) if rows else 0.0
    totals['avg_expense'] = round(totals['expense_total'] / len(rows), 2) if rows else 0.0

    options = [{'key': key, 'label': _month_label(key)} for key in all_keys]
    return {
        'rows': rows,  # ordine cronologico: dal più vecchio al più recente
        'totals': totals,
        'options': options,
        'selected_from': selected_from,
        'selected_to': selected_to,
        'available_from': available_from,
        'available_to': available_to,
        'range_swapped': range_swapped,
    }


def _cash_month_delete_stats(cur, scope_store: str, month_key: str):
    month_key = _month_key_from_value(month_key)
    start_s = month_key + '-01'
    end_s = _next_month_start(month_key).isoformat()
    where_sql, params = _cash_scope_where(scope_store)
    ph = _ph()
    stats = {'income_count': 0, 'expense_count': 0, 'income_total': 0.0, 'expense_total': 0.0}

    income_rows = _dict_rows(
        cur,
        f"SELECT COUNT(*) AS count_items, COALESCE(SUM(amount),0) AS total_amount FROM cash_entries WHERE {where_sql} AND flow_date >= {ph} AND flow_date < {ph}",
        params + (start_s, end_s),
    )
    if income_rows:
        stats['income_count'] = int(income_rows[0].get('count_items') or 0)
        stats['income_total'] = float(income_rows[0].get('total_amount') or 0)

    expense_rows = _dict_rows(
        cur,
        f"SELECT COUNT(*) AS count_items, COALESCE(SUM(amount),0) AS total_amount FROM cash_expenses WHERE {where_sql} AND flow_date >= {ph} AND flow_date < {ph}",
        params + (start_s, end_s),
    )
    if expense_rows:
        stats['expense_count'] = int(expense_rows[0].get('count_items') or 0)
        stats['expense_total'] = float(expense_rows[0].get('total_amount') or 0)
    return stats


def _build_scope_period_chart(cur, scope_store: str, period_type: str = 'week', anchor_s: str = ''):
    anchor_day = _parse_date_safe(anchor_s, date.today())
    period_type, start_d, end_d = _period_bounds(period_type, anchor_day)
    ph = _ph()
    where_sql, params = _cash_scope_where(scope_store)
    start_s = start_d.isoformat()
    end_s = end_d.isoformat()

    entries = _dict_rows(cur, f"SELECT flow_date, SUM(amount) AS total FROM cash_entries WHERE {where_sql} AND flow_date BETWEEN {ph} AND {ph} GROUP BY flow_date ORDER BY flow_date ASC", params + (start_s, end_s))
    expenses = _dict_rows(cur, f"SELECT flow_date, SUM(amount) AS total FROM cash_expenses WHERE {where_sql} AND flow_date BETWEEN {ph} AND {ph} GROUP BY flow_date ORDER BY flow_date ASC", params + (start_s, end_s))
    by_entry = {str(r['flow_date']): float(r.get('total') or 0) for r in entries}
    by_expense = {str(r['flow_date']): float(r.get('total') or 0) for r in expenses}

    income_detail_rows = _dict_rows(
        cur,
        f"SELECT flow_date, LOWER(TRIM(payment_method)) AS name, COALESCE(SUM(amount),0) AS total FROM cash_entries WHERE {where_sql} AND flow_date BETWEEN {ph} AND {ph} GROUP BY flow_date, LOWER(TRIM(payment_method)) ORDER BY flow_date ASC, total DESC",
        params + (start_s, end_s),
    )
    expense_detail_rows = _dict_rows(
        cur,
        f"SELECT flow_date, category, supplier, notes, amount FROM cash_expenses WHERE {where_sql} AND flow_date BETWEEN {ph} AND {ph} ORDER BY flow_date ASC, amount DESC",
        params + (start_s, end_s),
    )

    income_details = {}
    for row in income_detail_rows:
        ds = str(row.get('flow_date') or '')
        name = (row.get('name') or 'non specificato').strip() or 'non specificato'
        total = float(row.get('total') or 0)
        if total <= 0:
            continue
        income_details.setdefault(ds, []).append({'label': name, 'total': total})

    expense_details = {}
    for row in expense_detail_rows:
        ds = str(row.get('flow_date') or '')
        amount = float(row.get('amount') or 0)
        if amount <= 0:
            continue
        family = _expense_family(row)
        slot = expense_details.setdefault(ds, {}).setdefault(family, {'label': family, 'total': 0.0, 'count': 0})
        slot['total'] += amount
        slot['count'] += 1

    rows = []
    max_amount = 1.0
    days_count = (end_d - start_d).days + 1
    weekdays = ['Lun', 'Mar', 'Mer', 'Gio', 'Ven', 'Sab', 'Dom']

    def euro(v):
        return f"€ {float(v or 0):.2f}"

    def lines_for(items, total, empty_label):
        if not items:
            return f"Totale: {euro(total)}\n{empty_label}"
        out = [f"Totale: {euro(total)}"]
        for item in items[:8]:
            extra = ''
            if item.get('count') and int(item.get('count') or 0) > 1:
                extra = f" ({int(item.get('count') or 0)} mov.)"
            out.append(f"• {item.get('label')}: {euro(item.get('total') or 0)}{extra}")
        return '\n'.join(out)

    for i in range(days_count):
        d = start_d + timedelta(days=i)
        ds = d.isoformat()
        inc = by_entry.get(ds, 0.0)
        usc = by_expense.get(ds, 0.0)
        net = inc - usc
        max_amount = max(max_amount, inc, usc, abs(net))
        income_items = sorted(income_details.get(ds, []), key=lambda x: -float(x.get('total') or 0))
        expense_items = sorted((expense_details.get(ds, {}) or {}).values(), key=lambda x: -float(x.get('total') or 0))
        net_detail = f"Entrate: {euro(inc)}\nUscite: {euro(usc)}\nBilancio: {euro(net)}"
        rows.append({
            'date': ds,
            'label': d.strftime('%d/%m'),
            'day_label': f"{weekdays[d.weekday()]} {d.strftime('%d/%m')}",
            'income': inc,
            'expense': usc,
            'net': net,
            'income_detail': lines_for(income_items, inc, 'Nessuna entrata salvata'),
            'expense_detail': lines_for(expense_items, usc, 'Nessuna uscita salvata'),
            'net_detail': net_detail,
            'income_items': income_items,
            'expense_items': expense_items,
        })

    chart_height = 150
    for r in rows:
        r['income_h'] = max(8, int((r['income'] / max_amount) * chart_height)) if r['income'] else 8
        r['expense_h'] = max(8, int((r['expense'] / max_amount) * chart_height)) if r['expense'] else 8
        r['net_h'] = max(8, int((abs(r['net']) / max_amount) * chart_height)) if r['net'] else 8
        r['positive'] = r['net'] >= 0

    tick_values = [max_amount * x for x in (0.25, 0.50, 0.75, 1.0)]
    ticks = []
    for value in tick_values:
        label = f"€ {value:.0f}" if value >= 10 else f"€ {value:.2f}"
        ticks.append({'value': value, 'label': label, 'pct': round((value / max_amount) * 100.0, 2) if max_amount else 0, 'px': int((value / max_amount) * chart_height) if max_amount else 0})

    return {
        'rows': rows,
        'scale': {
            'max': max_amount,
            'ticks': ticks,
        }
    }

def _sales_month_overview(cur, store: str, month_key: str):
    selected_store = (store or 'spinza').strip()
    if selected_store not in STORES:
        selected_store = 'spinza'
    selected_month = _month_key_from_value(month_key)
    ph = _ph()
    row = cur.execute(f'SELECT id FROM sales_report_periods WHERE store={ph} AND month_key={ph}', (selected_store, selected_month)).fetchone()
    tree = _sales_report_tree(cur, int(row['id'])) if row else []
    groups = sorted(tree, key=lambda x: float(x.get('total_amount') or 0), reverse=True)[:7]
    total = sum(float(g.get('total_amount') or 0) for g in groups)
    palette = ['#2563eb', '#38bdf8', '#8b5cf6', '#ec4899', '#14b8a6', '#22c55e', '#f59e0b', '#ef4444', '#f97316', '#84cc16']
    out = []
    cursor = 0.0
    segments = []
    for idx, group in enumerate(groups):
        value = float(group.get('total_amount') or 0)
        share = (value / total * 100.0) if total > 0 else 0.0
        color = palette[idx % len(palette)]
        out.append({
            'name': group.get('name') or f'Gruppo {idx+1}',
            'total_amount': value,
            'share_pct': round(share, 1),
            'color': color,
        })
        next_cursor = cursor + share
        segments.append(f'{color} {cursor:.2f}% {next_cursor:.2f}%')
        cursor = next_cursor
    pie_style = 'conic-gradient(' + ', '.join(segments) + ')' if segments else 'conic-gradient(rgba(148,163,184,.22) 0 100%)'
    return {
        'store': selected_store,
        'store_label': _store_label(selected_store),
        'month_key': selected_month,
        'month_label': _month_label(selected_month),
        'groups': out,
        'total_amount': total,
        'pie_style': pie_style,
    }


def _month_key_from_value(raw: str) -> str:
    s = (raw or '').strip()
    if not s:
        return date.today().strftime('%Y-%m')
    try:
        if len(s) == 7:
            return datetime.strptime(s, '%Y-%m').strftime('%Y-%m')
        return datetime.strptime(s, '%Y-%m-%d').strftime('%Y-%m')
    except Exception:
        return date.today().strftime('%Y-%m')


def _month_label(month_key: str) -> str:
    try:
        d = datetime.strptime((month_key or '') + '-01', '%Y-%m-%d').date()
    except Exception:
        d = date.today().replace(day=1)
    months = ['gennaio', 'febbraio', 'marzo', 'aprile', 'maggio', 'giugno', 'luglio', 'agosto', 'settembre', 'ottobre', 'novembre', 'dicembre']
    return f"{months[d.month-1]} {d.year}"


def _previous_month_key(month_key: str):
    month_key = _month_key_from_value(month_key)
    try:
        dt = datetime.strptime(month_key + '-01', '%Y-%m-%d').date()
    except Exception:
        dt = date.today().replace(day=1)
    prev_last = dt - timedelta(days=1)
    return prev_last.strftime('%Y-%m')


def _pct_change_value(current: float, previous: float):
    current = float(current or 0)
    previous = float(previous or 0)
    if abs(previous) < 1e-9:
        return None
    return round(((current - previous) / previous) * 100.0, 1)


def _sales_tree_value_map(nodes, prefix=''):
    out = {}
    for n in nodes or []:
        path = f"{prefix}/{(n.get('name') or '').strip().lower()}" if prefix else (n.get('name') or '').strip().lower()
        out[path] = {
            'amount': float(n.get('total_amount') if n.get('has_children') else n.get('amount') or 0),
            'quantity': float(n.get('total_quantity') if n.get('has_children') else n.get('quantity') or 0),
        }
        out.update(_sales_tree_value_map(n.get('children') or [], path))
    return out


def _annotate_sales_tree_growth(nodes, previous_map, prefix=''):
    for n in nodes or []:
        path = f"{prefix}/{(n.get('name') or '').strip().lower()}" if prefix else (n.get('name') or '').strip().lower()
        prev = previous_map.get(path) or {}
        current_amount = float(n.get('total_amount') if n.get('has_children') else n.get('amount') or 0)
        current_quantity = float(n.get('total_quantity') if n.get('has_children') else n.get('quantity') or 0)
        n['growth_amount_pct'] = _pct_change_value(current_amount, float(prev.get('amount') or 0))
        n['growth_quantity_pct'] = _pct_change_value(current_quantity, float(prev.get('quantity') or 0))
        _annotate_sales_tree_growth(n.get('children') or [], previous_map, path)
    return nodes



DEFAULT_SALES_REPORT_GROUPS = [
    "Pinze classiche",
    "Pinze speciali",
    "Pinze stagionali",
    "Soft drink",
    "Alcol drink",
    "Delivery",
    "Desert (Dolci)",
]


def _sales_name_norm(value: str) -> str:
    return ' '.join((value or '').strip().lower().split())


def _sales_report_model_groups(cur, store: str):
    ph = _ph()
    rows = _dict_rows(cur, f"SELECT id, name, name_norm, sort_order, is_active FROM sales_report_group_models WHERE store={ph} AND COALESCE(is_active,1)=1 ORDER BY sort_order ASC, name ASC, id ASC", (store,))
    return rows


def _upsert_sales_report_model_group(cur, store: str, name: str, username: str = 'system', sort_order: int | None = None, is_active: int = 1):
    ph = _ph()
    clean = (name or '').strip()
    norm = _sales_name_norm(clean)
    if not norm:
        return None
    row = cur.execute(f"SELECT id, sort_order FROM sales_report_group_models WHERE store={ph} AND name_norm={ph}", (store, norm)).fetchone()
    if row:
        rid = int(row['id']) if isinstance(row, sqlite3.Row) or hasattr(row, 'keys') else int(row[0])
        keep_sort = int(sort_order if sort_order is not None else (row['sort_order'] if isinstance(row, sqlite3.Row) or hasattr(row, 'keys') else row[1]) or 0)
        cur.execute(f"UPDATE sales_report_group_models SET name={ph}, name_norm={ph}, sort_order={ph}, is_active={ph}, created_by={ph} WHERE id={ph}", (clean, norm, keep_sort, int(is_active or 0), username or 'system', rid))
        return rid
    if sort_order is None:
        sort_order = _fetch_one_int(cur, f"SELECT COALESCE(MAX(sort_order),0) FROM sales_report_group_models WHERE store={ph}", (store,)) + 10
    cur.execute(f"INSERT INTO sales_report_group_models(store, name, name_norm, sort_order, is_active, created_by) VALUES({ph},{ph},{ph},{ph},{ph},{ph})", (store, clean, norm, int(sort_order or 0), int(is_active or 0), username or 'system'))
    row = cur.execute(f"SELECT id FROM sales_report_group_models WHERE store={ph} AND name_norm={ph}", (store, norm)).fetchone()
    return int(row['id']) if row else None


def _delete_sales_report_model_group(cur, store: str, name: str):
    ph = _ph()
    norm = _sales_name_norm(name)
    if not norm:
        return
    cur.execute(f"DELETE FROM sales_report_group_models WHERE store={ph} AND name_norm={ph}", (store, norm))


def _ensure_sales_report_root_models(cur, store: str, username: str = 'system'):
    existing = _sales_report_model_groups(cur, store)
    if existing:
        return existing
    for idx, label in enumerate(DEFAULT_SALES_REPORT_GROUPS, start=1):
        _upsert_sales_report_model_group(cur, store, label, username=username, sort_order=idx*10, is_active=1)
    return _sales_report_model_groups(cur, store)


def _ensure_sales_report_groups_from_models(cur, period_id: int, store: str, username: str = 'system'):
    ph = _ph()
    models = _ensure_sales_report_root_models(cur, store, username)
    existing = _dict_rows(cur, f"SELECT id, name FROM sales_report_groups WHERE period_id={ph} AND parent_id IS NULL ORDER BY sort_order ASC, id ASC", (int(period_id),))
    existing_norm = {_sales_name_norm(r.get('name') or '') for r in existing}
    for model in models:
        if _sales_name_norm(model.get('name') or '') in existing_norm:
            continue
        cur.execute(
            f"INSERT INTO sales_report_groups(period_id, parent_id, name, base_name, amount, quantity, sort_order, created_by) VALUES({ph},NULL,{ph},{ph},0,0,{ph},{ph})",
            (int(period_id), model.get('name') or '', model.get('name') or '', int(model.get('sort_order') or 0), username or 'system')
        )


def _sales_report_root_groups(cur, period_id: int):
    ph = _ph()
    return _dict_rows(cur, f"SELECT id, name, sort_order FROM sales_report_groups WHERE period_id={ph} AND parent_id IS NULL ORDER BY sort_order ASC, name ASC, id ASC", (int(period_id),))


def _find_root_group_id_by_name(cur, period_id: int, group_name: str):
    target = _sales_name_norm(group_name)
    for row in _sales_report_root_groups(cur, period_id):
        if _sales_name_norm(row.get('name') or '') == target:
            return int(row['id'])
    return None


def _sales_report_rule_for_name(cur, store: str, base_name: str):
    ph = _ph()
    norm = _sales_name_norm(base_name)
    if not norm:
        return None
    row = cur.execute(
        f"SELECT id, store, source_name_norm, source_name, target_group_name, target_name, is_deleted FROM sales_report_name_rules WHERE store={ph} AND source_name_norm={ph}",
        (store, norm),
    ).fetchone()
    return dict(row) if row else None


def _upsert_sales_report_rule(cur, store: str, base_name: str, username: str = 'system', target_group_name: str = '', target_name: str = '', is_deleted: int = 0):
    ph = _ph()
    norm = _sales_name_norm(base_name)
    if not norm:
        return
    row = _sales_report_rule_for_name(cur, store, base_name)
    source_name = (base_name or '').strip()
    if row:
        cur.execute(
            f"UPDATE sales_report_name_rules SET source_name={ph}, target_group_name={ph}, target_name={ph}, is_deleted={ph}, created_by={ph} WHERE id={ph}",
            (source_name, (target_group_name or '').strip(), (target_name or '').strip(), int(is_deleted or 0), username or 'system', int(row['id']))
        )
    else:
        cur.execute(
            f"INSERT INTO sales_report_name_rules(store, source_name_norm, source_name, target_group_name, target_name, is_deleted, created_by) VALUES({ph},{ph},{ph},{ph},{ph},{ph},{ph})",
            (store, norm, source_name, (target_group_name or '').strip(), (target_name or '').strip(), int(is_deleted or 0), username or 'system')
        )


def _sales_report_apply_rules_to_existing_rows(cur, store: str, base_name: str, username: str = 'system'):
    ph = _ph()
    norm = _sales_name_norm(base_name)
    if not norm:
        return
    rule = _sales_report_rule_for_name(cur, store, base_name) or {}
    rows = _dict_rows(cur, f"""
        SELECT g.id, g.period_id, g.parent_id, g.name, g.base_name
        FROM sales_report_groups g
        JOIN sales_report_periods p ON p.id=g.period_id
        WHERE p.store={ph} AND LOWER(TRIM(COALESCE(g.base_name, g.name)))={ph}
    """, (store, norm))
    for row in rows:
        gid = int(row['id'])
        period_id = int(row['period_id'])
        if int(rule.get('is_deleted') or 0):
            cur.execute(f"DELETE FROM sales_report_groups WHERE id={ph}", (gid,))
            continue
        target_name = (rule.get('target_name') or '').strip() or (row.get('name') or '').strip() or (base_name or '').strip()
        target_parent = None
        target_group = (rule.get('target_group_name') or '').strip()
        if target_group:
            _ensure_sales_report_groups_from_models(cur, period_id, store, username)
            target_parent = _find_root_group_id_by_name(cur, period_id, target_group)
        cur.execute(f"UPDATE sales_report_groups SET name={ph}, parent_id={ph} WHERE id={ph}", (target_name, target_parent, gid))

def _ensure_sales_report_period(cur, store: str, month_key: str, username: str = 'system'):
    ph = _ph()
    row = cur.execute(f"SELECT id, label FROM sales_report_periods WHERE store={ph} AND month_key={ph}", (store, month_key)).fetchone()
    if row:
        try:
            period_id = int(row['id'])
        except Exception:
            period_id = int(row[0])
        _ensure_sales_report_groups_from_models(cur, period_id, store, username)
        return period_id
    label = _month_label(month_key)
    cur.execute(
        f"INSERT INTO sales_report_periods(store, month_key, label, created_by) VALUES({ph},{ph},{ph},{ph})",
        (store, month_key, label, username or 'system'),
    )
    row = cur.execute(f"SELECT id FROM sales_report_periods WHERE store={ph} AND month_key={ph}", (store, month_key)).fetchone()
    try:
        period_id = int(row['id'])
    except Exception:
        period_id = int(row[0])
    _ensure_sales_report_groups_from_models(cur, period_id, store, username)
    return period_id


def _sales_report_periods(cur, scope_store: str):
    where_sql, params = _cash_scope_where(scope_store)
    sql = f"SELECT id, store, month_key, label FROM sales_report_periods WHERE {where_sql} ORDER BY month_key DESC, id DESC"
    rows = _dict_rows(cur, sql, params)
    for r in rows:
        r['store_label'] = _store_label(r.get('store'))
        r['label_full'] = f"{r.get('label') or _month_label(r.get('month_key') or '')} · {r['store_label']}"
    return rows


def _sales_report_tree(cur, period_id: int):
    ph = _ph()
    rows = _dict_rows(cur, f"SELECT id, period_id, parent_id, name, base_name, amount, quantity, sort_order FROM sales_report_groups WHERE period_id={ph} ORDER BY sort_order ASC, name ASC, id ASC", (int(period_id),))
    by_parent = {}
    by_id = {}
    for r in rows:
        r['amount'] = float(r.get('amount') or 0)
        r['quantity'] = float(r.get('quantity') or 0)
        rid = int(r.get('id') or 0)
        by_id[rid] = r
        pid = r.get('parent_id')
        by_parent.setdefault(pid, []).append(r)

    totals_cache = {}

    def full_totals(row_id: int):
        row_id = int(row_id)
        cached = totals_cache.get(row_id)
        if cached is not None:
            return cached
        row = by_id.get(row_id) or {}
        amount = float(row.get('amount') or 0)
        quantity = float(row.get('quantity') or 0)
        for child in by_parent.get(row_id, []):
            child_amount, child_quantity = full_totals(int(child.get('id') or 0))
            amount += child_amount
            quantity += child_quantity
        totals_cache[row_id] = (amount, quantity)
        return totals_cache[row_id]

    def sibling_totals_for(parent_id=None):
        children = by_parent.get(parent_id, [])
        amount = 0.0
        quantity = 0.0
        for child in children:
            child_amount, child_quantity = full_totals(int(child.get('id') or 0))
            amount += child_amount
            quantity += child_quantity
        return {'amount': amount, 'quantity': quantity}

    def build(parent_id=None, sibling_totals=None):
        items = []
        children = by_parent.get(parent_id, [])
        totals = sibling_totals or sibling_totals_for(parent_id)
        for idx, child in enumerate(children, start=1):
            child_id = int(child['id'])
            own_amount = float(child.get('amount') or 0)
            own_quantity = float(child.get('quantity') or 0)
            total_amount, total_quantity = full_totals(child_id)
            child_amount_total = max(0.0, total_amount - own_amount)
            child_quantity_total = max(0.0, total_quantity - own_quantity)
            node_children = build(child_id, sibling_totals_for(child_id))
            node = {
                'id': child_id,
                'name': child.get('name') or 'Voce',
                'base_name': child.get('base_name') or child.get('name') or 'Voce',
                'parent_id': child.get('parent_id'),
                'amount': own_amount,
                'quantity': own_quantity,
                'total_amount': total_amount,
                'total_quantity': total_quantity,
                'sort_order': int(child.get('sort_order') or idx*10),
                'children': node_children,
            }
            node['share_amount'] = round((total_amount / float(totals.get('amount') or 0)) * 100, 1) if float(totals.get('amount') or 0) > 0 else 0.0
            node['share_quantity'] = round((total_quantity / float(totals.get('quantity') or 0)) * 100, 1) if float(totals.get('quantity') or 0) > 0 else 0.0
            node['child_amount_total'] = child_amount_total
            node['child_quantity_total'] = child_quantity_total
            node['has_children'] = len(node_children) > 0
            items.append(node)
        return items

    return build(None, sibling_totals_for(None))


def _sales_report_flat_options(nodes, prefix=''):
    out = []
    for n in nodes:
        label = f"{prefix}{n['name']}"
        out.append({'id': n['id'], 'label': label})
        out.extend(_sales_report_flat_options(n.get('children') or [], prefix + '— '))
    return out



def _sales_report_root_options(nodes, model_groups=None):
    """Opzioni pulite per assegnare le singole voci ai gruppi principali."""
    model_norms = {_sales_name_norm((g or {}).get('name') or '') for g in (model_groups or [])}
    out = []
    for n in nodes or []:
        name = n.get('name') or ''
        if model_norms and _sales_name_norm(name) not in model_norms:
            continue
        out.append({'id': n.get('id'), 'label': name})
    return out


def _sales_report_descendant_ids(cur, group_id: int):
    """Restituisce id del gruppo e di tutti i figli, per cancellazioni sicure."""
    ph = _ph()
    ids = [int(group_id)]
    cursor = 0
    while cursor < len(ids):
        children = _dict_rows(cur, f"SELECT id FROM sales_report_groups WHERE parent_id={ph}", (ids[cursor],))
        for child in children:
            cid = int(child.get('id') or 0)
            if cid and cid not in ids:
                ids.append(cid)
        cursor += 1
    return ids


def _sales_report_model_group_for_name(cur, store: str, name: str):
    ph = _ph()
    norm = _sales_name_norm(name)
    if not norm:
        return None
    row = cur.execute(
        f"SELECT id, name, sort_order FROM sales_report_group_models WHERE store={ph} AND name_norm={ph}",
        (store, norm),
    ).fetchone()
    return dict(row) if row else None


def _sales_report_root_name_for_group_id(cur, group_id: int):
    """Dato un parent_id, risale fino al gruppo principale e ne ritorna il nome."""
    ph = _ph()
    row = cur.execute(f"SELECT id, name, parent_id FROM sales_report_groups WHERE id={ph}", (int(group_id),)).fetchone()
    if not row:
        return ''
    row = dict(row)
    guard = 0
    while row.get('parent_id') is not None and guard < 25:
        parent = cur.execute(f"SELECT id, name, parent_id FROM sales_report_groups WHERE id={ph}", (int(row['parent_id']),)).fetchone()
        if not parent:
            break
        row = dict(parent)
        guard += 1
    return (row.get('name') or '').strip()


def _rename_sales_report_root_group_everywhere(cur, store: str, old_name: str, new_name: str, username: str = 'system'):
    """Rinomina un gruppo principale fisso in tutti i mesi dello stesso negozio."""
    ph = _ph()
    old_norm = _sales_name_norm(old_name)
    clean = (new_name or '').strip()
    if not old_norm or not clean:
        return
    model_row = _sales_report_model_group_for_name(cur, store, old_name)
    sort_order = int((model_row or {}).get('sort_order') or 0) if model_row else None
    _delete_sales_report_model_group(cur, store, old_name)
    _upsert_sales_report_model_group(cur, store, clean, username=username, sort_order=sort_order, is_active=1)
    cur.execute(
        f"UPDATE sales_report_name_rules SET target_group_name={ph}, is_deleted=0 WHERE store={ph} AND LOWER(TRIM(COALESCE(target_group_name,'')))={ph}",
        (clean, store, old_norm),
    )
    cur.execute(
        f"""
        UPDATE sales_report_groups
        SET name={ph},
            base_name=CASE
                WHEN COALESCE(base_name,'')='' OR LOWER(TRIM(base_name))={ph} THEN {ph}
                ELSE base_name
            END
        WHERE period_id IN (SELECT id FROM sales_report_periods WHERE store={ph})
          AND parent_id IS NULL
          AND LOWER(TRIM(name))={ph}
        """,
        (clean, old_norm, clean, store, old_norm),
    )


def _delete_sales_report_root_group_everywhere(cur, store: str, group_name: str):
    """Elimina davvero un gruppo principale fisso e i suoi sottogruppi da tutti i mesi."""
    ph = _ph()
    norm = _sales_name_norm(group_name)
    if not norm:
        return 0
    _delete_sales_report_model_group(cur, store, group_name)
    # Le regole che puntavano a questo gruppo non devono più bloccare o ricreare il vecchio gruppo:
    # le voci torneranno fuori dai gruppi finché non le riassegni.
    cur.execute(
        f"UPDATE sales_report_name_rules SET target_group_name={ph}, is_deleted=0 WHERE store={ph} AND LOWER(TRIM(COALESCE(target_group_name,'')))={ph}",
        ('', store, norm),
    )
    deleted = 0
    periods = _dict_rows(cur, f"SELECT id FROM sales_report_periods WHERE store={ph}", (store,))
    for pr in periods:
        roots = _dict_rows(
            cur,
            f"SELECT id FROM sales_report_groups WHERE period_id={ph} AND parent_id IS NULL AND LOWER(TRIM(name))={ph}",
            (int(pr['id']), norm),
        )
        for root in roots:
            ids = _sales_report_descendant_ids(cur, int(root['id']))
            if not ids:
                continue
            placeholders = ','.join([ph] * len(ids))
            cur.execute(f"DELETE FROM sales_report_groups WHERE id IN ({placeholders})", tuple(ids))
            deleted += len(ids)
    return deleted


def _parse_sales_report_upload(filename: str, raw_bytes: bytes):
    """Legge CSV/XLS/XLSX del report vendite e restituisce una lista di voci.
    Ogni voce: {name, quantity, amount}.
    """
    filename = (filename or '').lower()
    rows = []

    def normalize_table(table_rows):
        normalized = []
        header = None
        for raw in table_rows:
            vals = [str(x).strip() if x is not None else '' for x in raw]
            if not any(vals):
                continue
            first = (vals[0] or '').strip().lower()
            if first == 'name':
                header = vals
                continue
            if header is None and len(vals) >= 3 and (vals[0].strip().lower() == 'name' or vals[2].strip().lower() == 'total'):
                header = vals
                continue
            if first == 'total':
                continue
            normalized.append(vals)
        if not normalized:
            return []
        out = []
        for vals in normalized:
            name = (vals[0] if len(vals) > 0 else '').strip()
            if not name or name.lower() == 'name':
                continue
            quantity = _safe_amount(vals[2] if len(vals) > 2 else 0, 0.0)
            amount = 0.0
            preferred = [v for v in vals[3:] if str(v).strip() != '']
            if preferred:
                amount = _safe_amount(preferred[-1], 0.0)
                if amount <= 0:
                    for cand in reversed(preferred):
                        amount = _safe_amount(cand, 0.0)
                        if amount > 0:
                            break
            out.append({'name': name, 'quantity': quantity, 'amount': amount})
        return [r for r in out if r['name']]

    if filename.endswith('.csv'):
        text_data = raw_bytes.decode('utf-8-sig', errors='ignore')
        reader = csv.reader(io.StringIO(text_data))
        return normalize_table(list(reader))

    if filename.endswith('.xlsx') or filename.endswith('.xls'):
        try:
            import pandas as pd
            try:
                df = pd.read_excel(io.BytesIO(raw_bytes), header=None)
            except Exception:
                df = pd.read_html(io.BytesIO(raw_bytes))[0]
            return normalize_table(df.fillna('').values.tolist())
        except Exception as e:
            raise ValueError(f'File Excel non leggibile: {e}')

    raise ValueError('Formato non supportato. Usa CSV oppure Excel.')


def _group_cash_rows_by_date(rows, methods):
    ordered_methods = list(methods or [])
    grouped = {}
    for r in rows:
        store = r.get('store') or ''
        ds = r.get('flow_date') or ''
        key = (ds, store)
        item = grouped.setdefault(key, {
            'flow_date': ds,
            'store': store,
            'methods': {m: {'amount': 0.0, 'entries': []} for m in ordered_methods},
            'notes': [],
            'created_by': [],
            'row_total': 0.0,
        })
        method = (r.get('payment_method') or '').strip()
        if method and method not in item['methods']:
            item['methods'][method] = {'amount': 0.0, 'entries': []}
            if method not in ordered_methods:
                ordered_methods.append(method)
        if method:
            cell = item['methods'][method]
            amount = float(r.get('amount') or 0)
            cell['amount'] += amount
            cell['entries'].append({'id': r.get('id'), 'amount': amount})
            item['row_total'] += amount
        notes = (r.get('notes') or '').strip()
        if notes and notes not in item['notes']:
            item['notes'].append(notes)
        created = (r.get('created_by') or '').strip()
        if created and created not in item['created_by']:
            item['created_by'].append(created)
    result = sorted(grouped.values(), key=lambda x: (x['flow_date'], x['store']), reverse=True)
    for item in result:
        item['notes_text'] = ' | '.join(item['notes']) if item['notes'] else '-'
        item['created_by_text'] = ', '.join(item['created_by']) if item['created_by'] else '-'
        for m in ordered_methods:
            item['methods'].setdefault(m, {'amount': 0.0, 'entries': []})
    return ordered_methods, result



@app.get("/workspace", response_class=HTMLResponse)
def workspace_home(request: Request):
    user = require_login(request)
    if not user:
        return RedirectResponse("/", status_code=HTTP_303_SEE_OTHER)
    return RedirectResponse("/select-area", status_code=HTTP_303_SEE_OTHER)


@app.get("/gestionale", response_class=HTMLResponse)


def gestionale_home(request: Request, period_type: str = 'month', anchor_date: str = '', month_key: str = '', nav: str = '', deleted_month: str = '', deleted_entries: int = -1, deleted_expenses: int = -1):
    user = require_login(request)
    if not user:
        return RedirectResponse("/", status_code=HTTP_303_SEE_OTHER)

    brand = _current_store_scope(request, user)
    active_store = request.session.get("active_store") if is_admin(request) else None
    can_view_finance = can_view_management_finance(request, user)
    store = brand
    period_type, selected_month, anchor_day = _resolve_period_state(period_type, month_key, anchor_date, nav)
    anchor_s = anchor_day.isoformat()
    ph = _ph()
    stats = {
        "fatture": 0,
        "chiusure": 0,
        "spese": 0,
        "ordini_aperti": 0,
        "coda_ordini": 0,
    }
    recent_docs = []

    with connect() as conn:
        cur = conn.cursor()
        if store == "ALL":
            stats["fatture"] = _fetch_one_int(cur, "SELECT COUNT(*) FROM invoices_docs", ())
            stats["chiusure"] = _fetch_one_int(cur, "SELECT COUNT(*) FROM closures", ())
            stats["spese"] = _fetch_one_int(cur, "SELECT COUNT(*) FROM secondary_expenses", ())
            stats["ordini_aperti"] = _fetch_one_int(cur, "SELECT COUNT(*) FROM orders WHERE status='in_corso'", ())
            stats["coda_ordini"] = _fetch_one_int(cur, "SELECT COUNT(*) FROM order_queue", ())
            sql = """
                SELECT 'Fattura' AS tipo, supplier AS descrizione, doc_date AS data_doc, uploaded_by, ts
                FROM invoices_docs
                UNION ALL
                SELECT 'Chiusura' AS tipo, '' AS descrizione, closure_date AS data_doc, uploaded_by, ts
                FROM closures
                UNION ALL
                SELECT 'Spesa secondaria' AS tipo, '' AS descrizione, expense_date AS data_doc, uploaded_by, ts
                FROM secondary_expenses
                ORDER BY ts DESC
                LIMIT 8
            """
            recent_docs = [dict(r) for r in cur.execute(sql).fetchall()]
        else:
            stats["fatture"] = _fetch_one_int(cur, f"SELECT COUNT(*) FROM invoices_docs WHERE store={ph}", (store,))
            stats["chiusure"] = _fetch_one_int(cur, f"SELECT COUNT(*) FROM closures WHERE store={ph}", (store,))
            stats["spese"] = _fetch_one_int(cur, f"SELECT COUNT(*) FROM secondary_expenses WHERE store={ph}", (store,))
            stats["ordini_aperti"] = _fetch_one_int(cur, f"SELECT COUNT(*) FROM orders WHERE store={ph} AND status='in_corso'", (store,))
            stats["coda_ordini"] = _fetch_one_int(cur, f"SELECT COUNT(*) FROM order_queue WHERE store={ph}", (store,))
            sql = f"""
                SELECT * FROM (
                    SELECT 'Fattura' AS tipo, supplier AS descrizione, doc_date AS data_doc, uploaded_by, ts
                    FROM invoices_docs WHERE store={ph}
                    UNION ALL
                    SELECT 'Chiusura' AS tipo, '' AS descrizione, closure_date AS data_doc, uploaded_by, ts
                    FROM closures WHERE store={ph}
                    UNION ALL
                    SELECT 'Spesa secondaria' AS tipo, '' AS descrizione, expense_date AS data_doc, uploaded_by, ts
                    FROM secondary_expenses WHERE store={ph}
                ) t
                ORDER BY ts DESC
                LIMIT 8
            """
            recent_docs = [dict(r) for r in cur.execute(sql, (store, store, store)).fetchall()]

        compare, totals, recent_entries, recent_expenses, period_meta, insights = _build_cash_dashboard(cur, store, period_type, anchor_s)
        period_chart = _build_scope_period_chart(cur, store, period_type, anchor_s)
        period_rows = period_chart.get('rows', [])
        chart_scale = period_chart.get('scale', {'ticks': [], 'max': 1.0})
        expense_overview = _build_expense_overview(cur, store, period_type, anchor_s)
        month_options = _month_options_for_scope(cur, store, selected_month)
        sales_store = store if store != 'ALL' else (active_store or 'spinza')
        sales_overview = _sales_month_overview(cur, sales_store, selected_month)
        month_summaries = _cash_month_summaries(cur, store) if can_view_finance else []

    prev_anchor = _shift_anchor_date(period_type, anchor_day, -1).isoformat()
    next_anchor = _shift_anchor_date(period_type, anchor_day, 1).isoformat()
    nav_prev_url = _query_url('/gestionale', period_type=period_type, month_key=_month_key_from_value(prev_anchor), anchor_date=prev_anchor)
    nav_next_url = _query_url('/gestionale', period_type=period_type, month_key=_month_key_from_value(next_anchor), anchor_date=next_anchor)

    summary_labels = {
        'current': _scope_period_label(period_type, previous=False),
        'previous': _scope_period_label(period_type, previous=True),
    }

    return render(
        "gestionale_home.html",
        user=user,
        brand=brand,
        active_store=active_store,
        stats=stats,
        recent_docs=recent_docs,
        compare=compare,
        totals=totals,
        recent_entries=recent_entries,
        recent_expenses=recent_expenses,
        period_meta=period_meta,
        can_view_finance=can_view_finance,
        insights=insights,
        month_options=month_options,
        selected_month=selected_month,
        period_rows=period_rows,
        chart_scale=chart_scale,
        expense_overview=expense_overview,
        sales_overview=sales_overview,
        nav_prev_url=nav_prev_url,
        nav_next_url=nav_next_url,
        summary_labels=summary_labels,
        month_summaries=month_summaries,
        deleted_month=deleted_month,
        deleted_entries=deleted_entries,
        deleted_expenses=deleted_expenses,
    )



@app.get("/gestionale/storico-mensile", response_class=HTMLResponse)
def gestionale_month_timeline(request: Request, from_month: str = '', to_month: str = ''):
    user = require_login(request)
    if not user:
        return RedirectResponse("/", status_code=HTTP_303_SEE_OTHER)
    if not can_view_management_finance(request, user):
        return RedirectResponse("/gestionale", status_code=HTTP_303_SEE_OTHER)

    brand = _current_store_scope(request, user)
    active_store = request.session.get("active_store") if is_admin(request) else None
    with connect() as conn:
        cur = conn.cursor()
        timeline = _cash_month_timeline(cur, brand, from_month, to_month)

    return render(
        "gestionale_month_timeline.html",
        user=user,
        brand=brand,
        active_store=active_store,
        timeline=timeline,
        scope_label='Tutti i negozi' if brand == 'ALL' else _store_label(brand),
    )


@app.post("/gestionale/mese/elimina")
async def gestionale_delete_month(request: Request):
    user = require_login(request)
    if not user:
        return RedirectResponse("/", status_code=HTTP_303_SEE_OTHER)
    if not can_view_management_finance(request, user):
        return RedirectResponse("/gestionale", status_code=HTTP_303_SEE_OTHER)

    form = await request.form()
    raw_month = str(form.get('month_key') or '').strip()
    return_to = _safe_next_url(str(form.get('return_to') or '/gestionale'), '/gestionale')
    if not re.match(r'^\d{4}-\d{2}$', raw_month):
        return RedirectResponse(return_to, status_code=HTTP_303_SEE_OTHER)
    month_key = _month_key_from_value(raw_month)
    brand = _current_store_scope(request, user)
    start_s = month_key + '-01'
    end_s = _next_month_start(month_key).isoformat()
    where_sql, params = _cash_scope_where(brand)
    ph = _ph()

    with connect() as conn:
        cur = conn.cursor()
        stats = _cash_month_delete_stats(cur, brand, month_key)
        cur.execute(f"DELETE FROM cash_entries WHERE {where_sql} AND flow_date >= {ph} AND flow_date < {ph}", params + (start_s, end_s))
        cur.execute(f"DELETE FROM cash_expenses WHERE {where_sql} AND flow_date >= {ph} AND flow_date < {ph}", params + (start_s, end_s))
        log_store = brand if brand != 'ALL' else (request.session.get('active_store') or user.get('store') or 'spinza')
        _log(
            cur,
            store=log_store,
            username=user['username'],
            action='DELETE',
            category='CASSA',
            name=(
                f"Eliminato mese {month_key}: "
                f"{stats.get('income_count', 0)} entrate (€ {float(stats.get('income_total') or 0):.2f}) + "
                f"{stats.get('expense_count', 0)} uscite (€ {float(stats.get('expense_total') or 0):.2f})"
            ),
            delta=-(float(stats.get('income_total') or 0)) + float(stats.get('expense_total') or 0),
        )

    sep = '&' if '?' in return_to else '?'
    return RedirectResponse(
        f"{return_to}{sep}deleted_month={quote_plus(month_key)}&deleted_entries={int(stats.get('income_count') or 0)}&deleted_expenses={int(stats.get('expense_count') or 0)}",
        status_code=HTTP_303_SEE_OTHER,
    )


@app.get("/gestionale/dashboard", response_class=HTMLResponse)
def gestionale_dashboard(request: Request, period_type: str = 'month', anchor_date: str = '', month_key: str = ''):
    return RedirectResponse(_query_url('/gestionale', period_type=period_type, anchor_date=anchor_date, month_key=month_key), status_code=HTTP_303_SEE_OTHER)


@app.get("/gestionale/report-vendite", response_class=HTMLResponse)


def sales_report_page(request: Request, month_key: str = '', store: str = '', import_ok: int = 0, import_error: str = ''):
    user = require_login(request)
    if not user:
        return RedirectResponse("/", status_code=HTTP_303_SEE_OTHER)
    if not can_view_management_finance(request, user):
        return RedirectResponse("/gestionale", status_code=HTTP_303_SEE_OTHER)

    brand = _current_store_scope(request, user)
    active_store = request.session.get("active_store") if is_admin(request) else None
    selected_store = (store or (brand if brand != 'ALL' else (active_store or 'spinza'))).strip()
    if selected_store not in STORES:
        selected_store = brand if brand != 'ALL' else 'spinza'
    selected_month = _month_key_from_value(month_key)

    with connect() as conn:
        cur = conn.cursor()
        period_id = _ensure_sales_report_period(cur, selected_store, selected_month, user.get('username') or 'system')
        periods = _sales_report_periods(cur, brand)
        period_row = cur.execute(f"SELECT id, store, month_key, label FROM sales_report_periods WHERE id={_ph()}", (period_id,)).fetchone()
        period = dict(period_row) if period_row else {'id': period_id, 'store': selected_store, 'month_key': selected_month, 'label': _month_label(selected_month)}
        tree = _sales_report_tree(cur, period_id)
        prev_month_key = _previous_month_key(selected_month)
        prev_period_row = cur.execute(f"SELECT id FROM sales_report_periods WHERE store={_ph()} AND month_key={_ph()}", (selected_store, prev_month_key)).fetchone()
        previous_tree = _sales_report_tree(cur, int(prev_period_row['id'])) if prev_period_row else []
        previous_map = _sales_tree_value_map(previous_tree)
        tree = _annotate_sales_tree_growth(tree, previous_map)
        top_amount = sum(float(x.get('total_amount') or 0) for x in tree)
        top_qty = sum(float(x.get('total_quantity') or 0) for x in tree)
        prev_amount = sum(float(x.get('total_amount') or 0) for x in previous_tree)
        prev_qty = sum(float(x.get('total_quantity') or 0) for x in previous_tree)
        top_group = max(tree, key=lambda x: float(x.get('total_amount') or 0), default=None)
        leaf_count = 0
        uncategorized_count = 0
        uncategorized_amount = 0.0
        uncategorized_quantity = 0.0

        def walk(nodes, depth=0):
            nonlocal leaf_count, uncategorized_count, uncategorized_amount, uncategorized_quantity
            for node in nodes or []:
                children = node.get('children') or []
                if children:
                    walk(children, depth + 1)
                else:
                    leaf_count += 1
                    if depth == 0:
                        uncategorized_count += 1
                        uncategorized_amount += float(node.get('total_amount') or 0)
                        uncategorized_quantity += float(node.get('total_quantity') or 0)

        walk(tree)
        parent_options = _sales_report_flat_options(tree)
        model_groups = _sales_report_model_groups(cur, selected_store)
        root_parent_options = _sales_report_root_options(tree, model_groups)
        month_options = _month_options_for_scope(cur, selected_store, selected_month)
        summary = {
            'amount': top_amount,
            'quantity': top_qty,
            'previous_amount': prev_amount,
            'previous_quantity': prev_qty,
            'amount_growth_pct': _pct_change_value(top_amount, prev_amount),
            'quantity_growth_pct': _pct_change_value(top_qty, prev_qty),
            'top_group_name': (top_group.get('name') if top_group else 'Nessun gruppo'),
            'top_group_amount': float(top_group.get('total_amount') or 0) if top_group else 0.0,
            'top_group_share_pct': round((float(top_group.get('total_amount') or 0) / top_amount) * 100.0, 1) if top_group and top_amount > 0 else 0.0,
            'leaf_count': leaf_count,
            'uncategorized_count': uncategorized_count,
            'uncategorized_amount': uncategorized_amount,
            'uncategorized_quantity': uncategorized_quantity,
        }

    period['store_label'] = _store_label(period.get('store'))
    return render(
        'sales_report.html',
        user=user,
        brand=brand,
        active_store=active_store,
        stores=STORES,
        selected_store=selected_store,
        selected_month=selected_month,
        selected_month_label=_month_label(selected_month),
        period=period,
        periods=periods,
        month_options=month_options,
        parent_options=parent_options,
        root_parent_options=root_parent_options,
        model_groups=model_groups,
        tree=tree,
        chart_tree_json=json.dumps(tree),
        totals={'amount': top_amount, 'quantity': top_qty},
        summary=summary,
        import_ok=import_ok,
        import_error=import_error,
    )



@app.post("/gestionale/report-vendite/import")
async def sales_report_import_file(request: Request, upload_file: UploadFile = File(...)):
    user = require_login(request)
    if not user:
        return RedirectResponse('/', status_code=HTTP_303_SEE_OTHER)
    if not can_view_management_finance(request, user):
        return RedirectResponse('/gestionale', status_code=HTTP_303_SEE_OTHER)

    form = await request.form()
    month_key = _month_key_from_value(str(form.get('month_key') or ''))
    store = str(form.get('store') or '').strip()
    if store not in STORES:
        store = _current_store_scope(request, user)
        if store == 'ALL':
            store = request.session.get('active_store') or 'spinza'
    if not is_admin(request):
        store = user.get('store') or store

    parent_raw = str(form.get('parent_id') or '').strip()
    parent_id = int(parent_raw) if parent_raw.isdigit() else None
    filename = upload_file.filename or 'report.csv'
    raw = await upload_file.read()
    if not raw:
        return RedirectResponse(f'/gestionale/report-vendite?store={store}&month_key={month_key}&import_error=File+vuoto', status_code=HTTP_303_SEE_OTHER)

    try:
        rows = _parse_sales_report_upload(filename, raw)
    except Exception as e:
        msg = str(e).replace(' ', '+')
        return RedirectResponse(f'/gestionale/report-vendite?store={store}&month_key={month_key}&import_error={msg}', status_code=HTTP_303_SEE_OTHER)

    inserted = 0
    with connect() as conn:
        cur = conn.cursor()
        period_id = _ensure_sales_report_period(cur, store, month_key, user.get('username') or 'system')
        if parent_id:
            parent_exists = _fetch_one_int(cur, f'SELECT COUNT(*) FROM sales_report_groups WHERE id={_ph()} AND period_id={_ph()}', (parent_id, period_id))
            if not parent_exists:
                parent_id = None
        _ensure_sales_report_groups_from_models(cur, period_id, store, user.get('username') or 'system')
        for row in rows:
            name = str(row.get('name') or '').strip()
            if not name:
                continue
            amount = float(row.get('amount') or 0)
            quantity = float(row.get('quantity') or 0)
            target_parent = parent_id
            target_name = name
            if target_parent is None:
                rule = _sales_report_rule_for_name(cur, store, name) or {}
                if int(rule.get('is_deleted') or 0):
                    # Vecchie versioni salvavano "eliminato per sempre": ora non blocchiamo più la ricreazione/importazione.
                    _upsert_sales_report_rule(cur, store, name, user.get('username') or 'system', target_group_name='', target_name='', is_deleted=0)
                    rule = {}
                target_name = (rule.get('target_name') or '').strip() or name
                target_group_name = (rule.get('target_group_name') or '').strip()
                if target_group_name:
                    target_parent = _find_root_group_id_by_name(cur, period_id, target_group_name)
            if target_parent is None:
                next_sort = _fetch_one_int(cur, f'SELECT COALESCE(MAX(sort_order),0) FROM sales_report_groups WHERE period_id={_ph()} AND parent_id IS NULL', (period_id,))
                cur.execute(
                    f'INSERT INTO sales_report_groups(period_id, name, base_name, amount, quantity, sort_order, created_by) VALUES({_ph()},{_ph()},{_ph()},{_ph()},{_ph()},{_ph()},{_ph()})',
                    (period_id, target_name, name, amount, quantity, int(next_sort) + 10, user.get('username') or 'system')
                )
            else:
                next_sort = _fetch_one_int(cur, f'SELECT COALESCE(MAX(sort_order),0) FROM sales_report_groups WHERE period_id={_ph()} AND parent_id={_ph()}', (period_id, target_parent))
                cur.execute(
                    f'INSERT INTO sales_report_groups(period_id, parent_id, name, base_name, amount, quantity, sort_order, created_by) VALUES({_ph()},{_ph()},{_ph()},{_ph()},{_ph()},{_ph()},{_ph()},{_ph()})',
                    (period_id, target_parent, target_name, name, amount, quantity, int(next_sort) + 10, user.get('username') or 'system')
                )
            inserted += 1
        _log(cur, store=store, username=user['username'], action='IMPORT', category='REPORT_VENDITE', name=f'Import report vendite {month_key}', delta=0)
        conn.commit()

    return RedirectResponse(f'/gestionale/report-vendite?store={store}&month_key={month_key}&import_ok={inserted}', status_code=HTTP_303_SEE_OTHER)


@app.post("/gestionale/report-vendite/clear-month")
async def sales_report_clear_month(request: Request):
    user = require_login(request)
    if not user:
        return RedirectResponse('/', status_code=HTTP_303_SEE_OTHER)
    if not can_view_management_finance(request, user):
        return RedirectResponse('/gestionale', status_code=HTTP_303_SEE_OTHER)

    form = await request.form()
    month_key = _month_key_from_value(str(form.get('month_key') or ''))
    store = str(form.get('store') or '').strip()
    if store not in STORES:
        store = _current_store_scope(request, user)
        if store == 'ALL':
            store = request.session.get('active_store') or 'spinza'
    if not is_admin(request):
        store = user.get('store') or store

    with connect() as conn:
        cur = conn.cursor()
        period_id = _ensure_sales_report_period(cur, store, month_key, user.get('username') or 'system')
        _ensure_sales_report_groups_from_models(cur, period_id, store, user.get('username') or 'system')
        root_rows = _dict_rows(cur, f"SELECT id FROM sales_report_groups WHERE period_id={_ph()} AND parent_id IS NULL", (period_id,))
        root_ids = [int(r['id']) for r in root_rows]
        if root_ids:
            placeholders = ','.join([_ph()] * len(root_ids))
            cur.execute(
                f"DELETE FROM sales_report_groups WHERE period_id={_ph()} AND parent_id IS NOT NULL AND id NOT IN ({placeholders})",
                (period_id, *root_ids),
            )
        else:
            cur.execute(f"DELETE FROM sales_report_groups WHERE period_id={_ph()}", (period_id,))
            _ensure_sales_report_groups_from_models(cur, period_id, store, user.get('username') or 'system')
        cur.execute(f"UPDATE sales_report_groups SET amount=0, quantity=0 WHERE period_id={_ph()} AND parent_id IS NULL", (period_id,))
        _log(cur, store=store, username=user['username'], action='DELETE', category='REPORT_VENDITE', name=f'Svuotato report vendite {month_key}', delta=0)
        conn.commit()

    return RedirectResponse(f'/gestionale/report-vendite?store={store}&month_key={month_key}', status_code=HTTP_303_SEE_OTHER)



@app.post("/gestionale/report-vendite/gruppo-fisso/{model_id}/rename")
async def sales_report_rename_model_group(request: Request, model_id: int):
    user = require_login(request)
    if not user:
        return RedirectResponse("/", status_code=HTTP_303_SEE_OTHER)
    if not can_view_management_finance(request, user):
        return RedirectResponse("/gestionale", status_code=HTTP_303_SEE_OTHER)

    form = await request.form()
    new_name = str(form.get('name') or '').strip()
    month_key = _month_key_from_value(str(form.get('month_key') or ''))
    store = str(form.get('store') or '').strip()
    ph = _ph()
    brand = _current_store_scope(request, user)

    with connect() as conn:
        cur = conn.cursor()
        row = cur.execute(f"SELECT id, store, name FROM sales_report_group_models WHERE id={ph}", (int(model_id),)).fetchone()
        if not row:
            return RedirectResponse(f"/gestionale/report-vendite?store={store}&month_key={month_key}", status_code=HTTP_303_SEE_OTHER)
        row = dict(row)
        real_store = row.get('store') or store or 'spinza'
        if brand != 'ALL' and real_store != brand:
            return RedirectResponse("/gestionale/report-vendite", status_code=HTTP_303_SEE_OTHER)
        if new_name:
            _rename_sales_report_root_group_everywhere(cur, real_store, row.get('name') or '', new_name, user.get('username') or 'system')
            _log(cur, store=real_store, username=user['username'], action='UPDATE', category='REPORT_VENDITE', name=f"Rinominato gruppo fisso {row.get('name','')} -> {new_name}", delta=0)
            conn.commit()

    return RedirectResponse(f"/gestionale/report-vendite?store={real_store}&month_key={month_key}", status_code=HTTP_303_SEE_OTHER)


@app.post("/gestionale/report-vendite/gruppo-fisso/{model_id}/delete")
async def sales_report_delete_model_group(request: Request, model_id: int):
    user = require_login(request)
    if not user:
        return RedirectResponse("/", status_code=HTTP_303_SEE_OTHER)
    if not can_view_management_finance(request, user):
        return RedirectResponse("/gestionale", status_code=HTTP_303_SEE_OTHER)

    form = await request.form()
    month_key = _month_key_from_value(str(form.get('month_key') or ''))
    store = str(form.get('store') or '').strip()
    ph = _ph()
    brand = _current_store_scope(request, user)

    with connect() as conn:
        cur = conn.cursor()
        row = cur.execute(f"SELECT id, store, name FROM sales_report_group_models WHERE id={ph}", (int(model_id),)).fetchone()
        if not row:
            return RedirectResponse(f"/gestionale/report-vendite?store={store}&month_key={month_key}", status_code=HTTP_303_SEE_OTHER)
        row = dict(row)
        real_store = row.get('store') or store or 'spinza'
        if brand != 'ALL' and real_store != brand:
            return RedirectResponse("/gestionale/report-vendite", status_code=HTTP_303_SEE_OTHER)
        _delete_sales_report_root_group_everywhere(cur, real_store, row.get('name') or '')
        _log(cur, store=real_store, username=user['username'], action='DELETE', category='REPORT_VENDITE', name=f"Eliminato gruppo fisso {row.get('name','')}", delta=0)
        conn.commit()

    return RedirectResponse(f"/gestionale/report-vendite?store={real_store}&month_key={month_key}", status_code=HTTP_303_SEE_OTHER)


@app.post("/gestionale/report-vendite/voce")
async def sales_report_add_group(request: Request):
    user = require_login(request)
    if not user:
        return RedirectResponse("/", status_code=HTTP_303_SEE_OTHER)
    if not can_view_management_finance(request, user):
        return RedirectResponse("/gestionale", status_code=HTTP_303_SEE_OTHER)

    form = await request.form()
    month_key = _month_key_from_value(str(form.get('month_key') or ''))
    store = str(form.get('store') or '').strip()
    if store not in STORES:
        store = _current_store_scope(request, user)
        if store == 'ALL':
            store = request.session.get('active_store') or 'spinza'
    if not is_admin(request):
        store = user.get('store') or store

    name = str(form.get('name') or '').strip()
    parent_raw = str(form.get('parent_id') or '').strip()
    amount = _safe_amount(form.get('amount'), 0.0)
    quantity = _safe_amount(form.get('quantity'), 0.0)
    persist_model = str(form.get('persist_model') or '1').strip() in ('1', 'true', 'on', 'yes')
    if not name:
        return RedirectResponse(f"/gestionale/report-vendite?store={store}&month_key={month_key}", status_code=HTTP_303_SEE_OTHER)
    parent_id = int(parent_raw) if parent_raw.isdigit() else None

    with connect() as conn:
        cur = conn.cursor()
        period_id = _ensure_sales_report_period(cur, store, month_key, user.get('username') or 'system')
        next_sort = _fetch_one_int(cur, f"SELECT COALESCE(MAX(sort_order),0) FROM sales_report_groups WHERE period_id={_ph()} AND " + (f"parent_id={_ph()}" if parent_id else "parent_id IS NULL"), ((period_id, parent_id) if parent_id else (period_id,)))
        if parent_id:
            parent_exists = _fetch_one_int(cur, f"SELECT COUNT(*) FROM sales_report_groups WHERE id={_ph()} AND period_id={_ph()}", (parent_id, period_id))
            if not parent_exists:
                parent_id = None
        cols = f"period_id, parent_id, name, base_name, amount, quantity, sort_order, created_by" if parent_id is not None else f"period_id, name, base_name, amount, quantity, sort_order, created_by"
        placeholders = f"{_ph()},{_ph()},{_ph()},{_ph()},{_ph()},{_ph()},{_ph()},{_ph()}" if parent_id is not None else f"{_ph()},{_ph()},{_ph()},{_ph()},{_ph()},{_ph()},{_ph()}"
        params = (period_id, parent_id, name, name, amount, quantity, int(next_sort)+10, user.get('username') or 'system') if parent_id is not None else (period_id, name, name, amount, quantity, int(next_sort)+10, user.get('username') or 'system')
        cur.execute(f"INSERT INTO sales_report_groups({cols}) VALUES({placeholders})", params)
        # Se in passato questa voce era stata cancellata con una regola permanente, permetti di ricrearla.
        old_rule = _sales_report_rule_for_name(cur, store, name)
        if old_rule and int(old_rule.get('is_deleted') or 0):
            _upsert_sales_report_rule(cur, store, name, user.get('username') or 'system', target_group_name='', target_name='', is_deleted=0)
        if parent_id is None and persist_model:
            _upsert_sales_report_model_group(cur, store, name, username=user.get('username') or 'system', sort_order=int(next_sort)+10, is_active=1)
        _log(cur, store=store, username=user['username'], action='CREATE', category='REPORT_VENDITE', name=f"Report vendite {month_key} - {name}", delta=float(amount or 0))
        conn.commit()

    return RedirectResponse(f"/gestionale/report-vendite?store={store}&month_key={month_key}", status_code=HTTP_303_SEE_OTHER)



@app.post("/gestionale/report-vendite/voce/{group_id}/move")
async def sales_report_move_group(request: Request, group_id: int):
    user = require_login(request)
    if not user:
        return RedirectResponse("/", status_code=HTTP_303_SEE_OTHER)
    if not can_view_management_finance(request, user):
        return RedirectResponse("/gestionale", status_code=HTTP_303_SEE_OTHER)

    form = await request.form()
    target_raw = str(form.get('parent_id') or '').strip()
    target_parent = int(target_raw) if target_raw.isdigit() else None
    month_key = _month_key_from_value(str(form.get('month_key') or ''))
    store = str(form.get('store') or '').strip()
    brand = _current_store_scope(request, user)
    ph = _ph()

    with connect() as conn:
        cur = conn.cursor()
        row = cur.execute(
            f"SELECT g.id, g.parent_id, g.name, g.base_name, p.id AS period_id, p.store, p.month_key FROM sales_report_groups g JOIN sales_report_periods p ON p.id=g.period_id WHERE g.id={ph}",
            (group_id,),
        ).fetchone()
        if not row:
            return RedirectResponse("/gestionale/report-vendite", status_code=HTTP_303_SEE_OTHER)
        row = dict(row)
        if brand != 'ALL' and row.get('store') != brand:
            return RedirectResponse("/gestionale/report-vendite", status_code=HTTP_303_SEE_OTHER)

        period_id = int(row['period_id'])
        real_store = row.get('store') or 'spinza'
        if store not in STORES:
            store = real_store
        if not month_key:
            month_key = row.get('month_key') or _today_str()[:7]

        if target_parent == group_id:
            target_parent = None

        descendants = set(_sales_report_descendant_ids(cur, group_id))
        if target_parent in descendants:
            target_parent = None
        if target_parent is not None:
            valid = _fetch_one_int(cur, f"SELECT COUNT(*) FROM sales_report_groups WHERE id={ph} AND period_id={ph}", (target_parent, period_id))
            if not valid:
                target_parent = None

        is_leaf = _fetch_one_int(cur, f"SELECT COUNT(*) FROM sales_report_groups WHERE parent_id={ph}", (group_id,)) == 0
        is_root = row.get('parent_id') is None
        fixed_root = is_root and _sales_report_model_group_for_name(cur, real_store, row.get('name') or '') is not None
        root_group = is_root and (fixed_root or not is_leaf)
        base_name = (row.get('base_name') or row.get('name') or '').strip()

        if is_leaf and not root_group and base_name:
            # Voce singola importata: cambia il gruppo di appartenenza e salva la regola per i prossimi import.
            target_group_name = _sales_report_root_name_for_group_id(cur, target_parent) if target_parent is not None else ''
            _upsert_sales_report_rule(
                cur,
                real_store,
                base_name,
                user.get('username') or 'system',
                target_group_name=target_group_name,
                target_name=(row.get('name') or '').strip(),
                is_deleted=0,
            )
            _sales_report_apply_rules_to_existing_rows(cur, real_store, base_name, user.get('username') or 'system')
            # Se l'utente ha scelto un sottolivello preciso, mantienilo almeno nel mese aperto.
            cur.execute(f"UPDATE sales_report_groups SET parent_id={ph} WHERE id={ph}", (target_parent, group_id))
        else:
            # Gruppo principale o ramo: spostamento diretto.
            cur.execute(f"UPDATE sales_report_groups SET parent_id={ph} WHERE id={ph}", (target_parent, group_id))
            if fixed_root and target_parent is not None:
                _delete_sales_report_model_group(cur, real_store, row.get('name') or '')
            elif (not is_root) and target_parent is None and not is_leaf:
                next_sort_model = _fetch_one_int(cur, f"SELECT COALESCE(MAX(sort_order),0) FROM sales_report_group_models WHERE store={ph}", (real_store,)) + 10
                _upsert_sales_report_model_group(cur, real_store, row.get('name') or '', username=user.get('username') or 'system', sort_order=next_sort_model, is_active=1)

        _log(cur, store=real_store, username=user['username'], action='UPDATE', category='REPORT_VENDITE', name=f"Spostata voce report {row.get('name','')}", delta=0)
        conn.commit()

    return RedirectResponse(f"/gestionale/report-vendite?store={store}&month_key={month_key}", status_code=HTTP_303_SEE_OTHER)


@app.post("/gestionale/report-vendite/voce/{group_id}/rename")
async def sales_report_rename_group(request: Request, group_id: int):
    user = require_login(request)
    if not user:
        return RedirectResponse("/", status_code=HTTP_303_SEE_OTHER)
    if not can_view_management_finance(request, user):
        return RedirectResponse("/gestionale", status_code=HTTP_303_SEE_OTHER)

    form = await request.form()
    new_name = str(form.get('name') or '').strip()
    if not new_name:
        return RedirectResponse("/gestionale/report-vendite", status_code=HTTP_303_SEE_OTHER)
    ph = _ph()
    brand = _current_store_scope(request, user)
    with connect() as conn:
        cur = conn.cursor()
        row = cur.execute(
            f"SELECT g.id, g.parent_id, g.name, g.base_name, p.store, p.month_key FROM sales_report_groups g JOIN sales_report_periods p ON p.id=g.period_id WHERE g.id={ph}",
            (group_id,),
        ).fetchone()
        if not row:
            return RedirectResponse("/gestionale/report-vendite", status_code=HTTP_303_SEE_OTHER)
        row = dict(row)
        real_store = row.get('store') or 'spinza'
        if brand != 'ALL' and real_store != brand:
            return RedirectResponse("/gestionale/report-vendite", status_code=HTTP_303_SEE_OTHER)

        is_leaf = _fetch_one_int(cur, f"SELECT COUNT(*) FROM sales_report_groups WHERE parent_id={ph}", (group_id,)) == 0
        is_root = row.get('parent_id') is None
        fixed_root = is_root and _sales_report_model_group_for_name(cur, real_store, row.get('name') or '') is not None
        root_group = is_root and (fixed_root or not is_leaf)
        base_name = (row.get('base_name') or row.get('name') or '').strip()

        if fixed_root:
            _rename_sales_report_root_group_everywhere(cur, real_store, row.get('name') or '', new_name, user.get('username') or 'system')
        elif is_leaf and not root_group and base_name:
            rule = _sales_report_rule_for_name(cur, real_store, base_name) or {}
            _upsert_sales_report_rule(
                cur,
                real_store,
                base_name,
                user.get('username') or 'system',
                target_group_name=rule.get('target_group_name') or '',
                target_name=new_name,
                is_deleted=0,
            )
            _sales_report_apply_rules_to_existing_rows(cur, real_store, base_name, user.get('username') or 'system')
        else:
            cur.execute(f"UPDATE sales_report_groups SET name={ph} WHERE id={ph}", (new_name, group_id))

        _log(cur, store=real_store, username=user['username'], action='UPDATE', category='REPORT_VENDITE', name=f"Rinominata voce report {row.get('name','')} -> {new_name}", delta=0)
        conn.commit()
        return RedirectResponse(f"/gestionale/report-vendite?store={real_store}&month_key={row.get('month_key')}", status_code=HTTP_303_SEE_OTHER)


@app.post("/gestionale/report-vendite/voce/{group_id}/delete")
async def sales_report_delete_group(request: Request, group_id: int):
    user = require_login(request)
    if not user:
        return RedirectResponse("/", status_code=HTTP_303_SEE_OTHER)
    if not can_view_management_finance(request, user):
        return RedirectResponse("/gestionale", status_code=HTTP_303_SEE_OTHER)

    form = await request.form()
    remember_delete = str(form.get('remember_delete') or '').strip().lower() in ('1', 'true', 'on', 'yes')
    brand = _current_store_scope(request, user)
    ph = _ph()
    with connect() as conn:
        cur = conn.cursor()
        row = cur.execute(
            f"SELECT g.id, g.parent_id, g.name, g.base_name, g.amount, p.store, p.month_key FROM sales_report_groups g JOIN sales_report_periods p ON p.id=g.period_id WHERE g.id={ph}",
            (group_id,),
        ).fetchone()
        if not row:
            return RedirectResponse("/gestionale/report-vendite", status_code=HTTP_303_SEE_OTHER)
        row = dict(row)
        real_store = row.get('store') or 'spinza'
        if brand != 'ALL' and real_store != brand:
            return RedirectResponse("/gestionale/report-vendite", status_code=HTTP_303_SEE_OTHER)

        is_leaf = _fetch_one_int(cur, f"SELECT COUNT(*) FROM sales_report_groups WHERE parent_id={ph}", (group_id,)) == 0
        is_root = row.get('parent_id') is None
        fixed_root = is_root and _sales_report_model_group_for_name(cur, real_store, row.get('name') or '') is not None
        root_group = is_root and (fixed_root or not is_leaf)
        base_name = ((row.get('base_name') or '') or row.get('name') or '').strip()

        if fixed_root:
            _delete_sales_report_root_group_everywhere(cur, real_store, row.get('name') or '')
        elif root_group or not is_leaf:
            ids = _sales_report_descendant_ids(cur, group_id)
            if ids:
                placeholders = ','.join([ph] * len(ids))
                cur.execute(f"DELETE FROM sales_report_groups WHERE id IN ({placeholders})", tuple(ids))
        else:
            if remember_delete and base_name:
                rule = _sales_report_rule_for_name(cur, real_store, base_name) or {}
                _upsert_sales_report_rule(
                    cur,
                    real_store,
                    base_name,
                    user.get('username') or 'system',
                    target_group_name=rule.get('target_group_name') or '',
                    target_name=rule.get('target_name') or '',
                    is_deleted=1,
                )
                _sales_report_apply_rules_to_existing_rows(cur, real_store, base_name, user.get('username') or 'system')
            else:
                # Eliminazione normale: cancella la riga, ma non blocca la ricreazione/importazione futura.
                cur.execute(f"DELETE FROM sales_report_groups WHERE id={ph}", (group_id,))
                if base_name:
                    old_rule = _sales_report_rule_for_name(cur, real_store, base_name)
                    if old_rule and int(old_rule.get('is_deleted') or 0):
                        _upsert_sales_report_rule(cur, real_store, base_name, user.get('username') or 'system', target_group_name='', target_name='', is_deleted=0)

        _log(cur, store=real_store, username=user['username'], action='DELETE', category='REPORT_VENDITE', name=f"Report vendite eliminato {row.get('month_key','')} - {row.get('name','')}", delta=float(row.get('amount') or 0))
        conn.commit()
        return RedirectResponse(f"/gestionale/report-vendite?store={real_store}&month_key={row.get('month_key')}", status_code=HTTP_303_SEE_OTHER)






def _cash_entries_page_context(request: Request, user: dict, flow_date: str = '', payment_method: str = 'ALL', period_type: str = 'month', anchor_date: str = '', month_key: str = '', nav: str = '', imported_entries: int = 0, imported_expenses: int = 0, import_error: str = '', import_preview=None):
    import_error = unquote_plus(str(import_error or '')).strip()
    brand = _current_store_scope(request, user)
    active_store = request.session.get("active_store") if is_admin(request) else None
    can_edit_management = user.get('role') in ('admin', 'manager', 'staff')
    period_type, selected_month, anchor_day = _resolve_period_state(period_type, month_key, anchor_date, nav)
    anchor_s = anchor_day.isoformat()
    where_sql, params = _cash_scope_where(brand)
    ph = _ph()
    sql = f"SELECT id, flow_date, store, payment_method, amount, notes, created_by FROM cash_entries WHERE {where_sql}"
    qparams = list(params)
    if flow_date:
        sql += f" AND flow_date={ph}"
        qparams.append(flow_date)
    if payment_method and payment_method != 'ALL':
        sql += f" AND payment_method={ph}"
        qparams.append(payment_method)
    sql += " ORDER BY ts DESC, id DESC"
    chart_store = brand if brand != 'ALL' else (active_store or 'spinza')
    with connect() as conn:
        cur = conn.cursor()
        rows = _dict_rows(cur, sql, tuple(qparams))
        payments = [r['payment_method'] for r in _dict_rows(cur, f"SELECT DISTINCT payment_method FROM cash_entries WHERE {where_sql} ORDER BY payment_method ASC", params) if r.get('payment_method')]
        available_payment_methods = _load_cash_payment_methods(cur)
        history_methods, grouped_rows = _group_cash_rows_by_date(rows, available_payment_methods)
        chart_rows = _build_store_period_chart(cur, chart_store, period_type, anchor_s)
        _, page_totals, _, _, period_meta, page_insights = _build_cash_dashboard(cur, chart_store, period_type, anchor_s)
        month_options = _month_options_for_scope(cur, brand, selected_month)
        total_amount = sum(float(r.get('amount') or 0) for r in rows)
        income_breakdown = [dict(x) for x in (page_insights.get('income_breakdown') or [])]
        income_pie_total = float(page_totals.get('income_period') or 0)
        income_pie_style = _build_conic_gradient(income_breakdown, income_pie_total)
    prev_anchor = _shift_anchor_date(period_type, anchor_day, -1).isoformat()
    next_anchor = _shift_anchor_date(period_type, anchor_day, 1).isoformat()
    nav_prev_url = _query_url('/gestionale/incassi', flow_date=flow_date, payment_method=payment_method, period_type=period_type, month_key=_month_key_from_value(prev_anchor), anchor_date=prev_anchor)
    nav_next_url = _query_url('/gestionale/incassi', flow_date=flow_date, payment_method=payment_method, period_type=period_type, month_key=_month_key_from_value(next_anchor), anchor_date=next_anchor)
    return dict(
        user=user, brand=brand, active_store=active_store, stores=STORES,
        can_edit_management=can_edit_management, rows=rows, today=date.today().isoformat(),
        selected_date=flow_date, selected_payment=payment_method, payments=payments,
        total_amount=total_amount, chart_rows=chart_rows, chart_store=chart_store, imported_entries=imported_entries, imported_expenses=imported_expenses, import_error=import_error,
        available_payment_methods=available_payment_methods, history_methods=history_methods, grouped_rows=grouped_rows, selected_period_type=period_type, selected_anchor_date=anchor_s,
        page_period_meta=period_meta, page_totals=page_totals, month_options=month_options, selected_month=selected_month,
        nav_prev_url=nav_prev_url, nav_next_url=nav_next_url, page_insights=page_insights,
        income_breakdown=income_breakdown, income_pie_total=income_pie_total, income_pie_style=income_pie_style,
        import_preview=import_preview,
    )




def _archived_store_detail_context(request: Request, user: dict, store_key: str, imported_entries: int = 0, imported_expenses: int = 0, import_error: str = '', import_preview=None):
    ph = _ph()
    with connect() as conn:
        cur = conn.cursor()
        archived_map = _archived_stores_map(cur)
        store_info = archived_map.get(store_key)
        if not store_info:
            return None
        entries = _dict_rows(cur, f"SELECT id, flow_date, payment_method, amount, notes, created_by, ts FROM cash_entries WHERE store={ph} ORDER BY flow_date DESC, id DESC", (store_key,))
        expenses = _dict_rows(cur, f"SELECT id, flow_date, category, supplier, payment_method, amount, notes, created_by, ts FROM cash_expenses WHERE store={ph} ORDER BY flow_date DESC, id DESC", (store_key,))
        available_payment_methods = _load_cash_payment_methods(cur)
        categories = _expense_category_options()
        by_year = {}
        for row in entries:
            year = str(row.get('flow_date') or '')[:4] or 'Senza anno'
            item = by_year.setdefault(year, {'year': year, 'income': 0.0, 'expense': 0.0, 'net': 0.0, 'entries_count': 0, 'expenses_count': 0})
            item['income'] += float(row.get('amount') or 0)
            item['entries_count'] += 1
        for row in expenses:
            year = str(row.get('flow_date') or '')[:4] or 'Senza anno'
            item = by_year.setdefault(year, {'year': year, 'income': 0.0, 'expense': 0.0, 'net': 0.0, 'entries_count': 0, 'expenses_count': 0})
            item['expense'] += float(row.get('amount') or 0)
            item['expenses_count'] += 1
        for item in by_year.values():
            item['net'] = item['income'] - item['expense']
        totals = {
            'income': round(sum(float(x.get('amount') or 0) for x in entries), 2),
            'expense': round(sum(float(x.get('amount') or 0) for x in expenses), 2),
            'entries_count': len(entries),
            'expenses_count': len(expenses),
        }
        totals['net'] = round(totals['income'] - totals['expense'], 2)
    return dict(
        user=user,
        active_store=request.session.get('active_store') if is_admin(request) else None,
        store_key=store_key,
        store=store_info,
        entries=entries,
        expenses=expenses,
        totals=totals,
        by_year=sorted(by_year.values(), key=lambda x: x.get('year') or '', reverse=True),
        available_payment_methods=available_payment_methods,
        categories=categories,
        today=date.today().isoformat(),
        imported_entries=imported_entries,
        imported_expenses=imported_expenses,
        import_error=import_error,
        import_preview=import_preview,
    )


@app.get("/gestionale/archiviati", response_class=HTMLResponse)
def archived_stores_page(request: Request, created: int = 0, error: str = ''):
    user = require_login(request)
    if not user:
        return RedirectResponse("/", status_code=HTTP_303_SEE_OTHER)
    if not can_view_management_finance(request, user):
        return RedirectResponse("/gestionale", status_code=HTTP_303_SEE_OTHER)
    ph = _ph()
    with connect() as conn:
        cur = conn.cursor()
        archived_rows = list(_archived_stores_map(cur).values())
        for row in archived_rows:
            key = row.get('store_key') or ''
            income = _fetch_one_float(cur, f"SELECT COALESCE(SUM(amount),0) FROM cash_entries WHERE store={ph}", (key,))
            expense = _fetch_one_float(cur, f"SELECT COALESCE(SUM(amount),0) FROM cash_expenses WHERE store={ph}", (key,))
            row['income_total'] = income
            row['expense_total'] = expense
            row['net_total'] = income - expense
            row['entries_count'] = _fetch_one_int(cur, f"SELECT COUNT(*) FROM cash_entries WHERE store={ph}", (key,))
            row['expenses_count'] = _fetch_one_int(cur, f"SELECT COUNT(*) FROM cash_expenses WHERE store={ph}", (key,))
    return render(
        "archived_stores.html",
        user=user,
        active_store=request.session.get('active_store') if is_admin(request) else None,
        stores=STORES,
        archived_stores=archived_rows,
        created=created,
        error=unquote_plus(error or ''),
    )


@app.post("/gestionale/archiviati")
async def archived_store_create(request: Request):
    user = require_login(request)
    if not user:
        return RedirectResponse("/", status_code=HTTP_303_SEE_OTHER)
    if not can_view_management_finance(request, user):
        return RedirectResponse("/gestionale", status_code=HTTP_303_SEE_OTHER)
    form = await request.form()
    name = str(form.get('name') or '').strip()
    opened_at = str(form.get('opened_at') or '').strip() or None
    closed_at = str(form.get('closed_at') or '').strip() or None
    notes = str(form.get('notes') or '').strip()
    if not name:
        return RedirectResponse('/gestionale/archiviati?error=' + quote_plus('Scrivi il nome del negozio archiviato.'), status_code=HTTP_303_SEE_OTHER)
    store_key = _archived_store_slug(name)
    ph = _ph()
    with connect() as conn:
        cur = conn.cursor()
        cur.execute(
            f"INSERT INTO archived_stores(store_key, name, opened_at, closed_at, notes, created_by) VALUES({ph},{ph},{ph},{ph},{ph},{ph})",
            (store_key, name, opened_at, closed_at, notes, user.get('username') or 'system'),
        )
    return RedirectResponse(f'/gestionale/archiviati/{store_key}?created=1', status_code=HTTP_303_SEE_OTHER)


@app.get("/gestionale/archiviati/{store_key}", response_class=HTMLResponse)
def archived_store_detail(request: Request, store_key: str, created: int = 0, imported_entries: int = 0, imported_expenses: int = 0, import_error: str = ''):
    user = require_login(request)
    if not user:
        return RedirectResponse("/", status_code=HTTP_303_SEE_OTHER)
    if not can_view_management_finance(request, user):
        return RedirectResponse("/gestionale", status_code=HTTP_303_SEE_OTHER)
    ctx = _archived_store_detail_context(request, user, store_key, imported_entries=imported_entries, imported_expenses=imported_expenses, import_error=unquote_plus(import_error or ''))
    if not ctx:
        return RedirectResponse('/gestionale/archiviati?error=' + quote_plus('Negozio archiviato non trovato.'), status_code=HTTP_303_SEE_OTHER)
    ctx['created'] = created
    return render("archived_store_detail.html", **ctx)


@app.post("/gestionale/archiviati/{store_key}/incasso")
async def archived_store_add_income(request: Request, store_key: str):
    user = require_login(request)
    if not user:
        return RedirectResponse("/", status_code=HTTP_303_SEE_OTHER)
    if not can_view_management_finance(request, user):
        return RedirectResponse("/gestionale", status_code=HTTP_303_SEE_OTHER)
    with connect() as conn:
        cur = conn.cursor()
        if not _is_archived_store_key(store_key, cur):
            return RedirectResponse('/gestionale/archiviati?error=' + quote_plus('Negozio archiviato non trovato.'), status_code=HTTP_303_SEE_OTHER)
        form = await request.form()
        flow_date = str(form.get('flow_date') or '').strip() or date.today().isoformat()
        notes = str(form.get('notes') or '').strip()
        method_amounts = []
        base_names = list(form.getlist('method_name'))
        base_amounts = list(form.getlist('amount_value'))
        for idx, method_name in enumerate(base_names):
            clean_method = str(method_name or '').strip()
            amount = _safe_amount(base_amounts[idx] if idx < len(base_amounts) else 0, 0.0)
            if clean_method and amount > 0:
                method_amounts.append((clean_method, amount))
        extra_names = list(form.getlist('custom_method_name'))
        extra_amounts = list(form.getlist('custom_method_amount'))
        for idx, name in enumerate(extra_names):
            clean_method = str(name or '').strip()
            amount = _safe_amount(extra_amounts[idx] if idx < len(extra_amounts) else 0, 0.0)
            if clean_method and amount > 0:
                method_amounts.append((clean_method, amount))
        for payment_method, amount in method_amounts:
            _ensure_payment_method(cur, payment_method, user['username'])
            _insert_cash_entry_compat(cur, store_key, flow_date, payment_method, amount, notes, user['username'])
            _log(cur, store=store_key, username=user['username'], action='CREATE', category='CASSA ARCHIVIATA', name=f"Incasso storico {flow_date} - {payment_method}", delta=float(amount or 0))
    return RedirectResponse(f'/gestionale/archiviati/{store_key}', status_code=HTTP_303_SEE_OTHER)


@app.post("/gestionale/archiviati/{store_key}/uscita")
async def archived_store_add_expense(request: Request, store_key: str):
    user = require_login(request)
    if not user:
        return RedirectResponse("/", status_code=HTTP_303_SEE_OTHER)
    if not can_view_management_finance(request, user):
        return RedirectResponse("/gestionale", status_code=HTTP_303_SEE_OTHER)
    form = await request.form()
    flow_date = str(form.get('flow_date') or '').strip() or date.today().isoformat()
    category = str(form.get('category') or '').strip()
    supplier = str(form.get('supplier') or '').strip()
    payment_method = str(form.get('payment_method') or '').strip()
    amount_value = _safe_amount(form.get('amount'), 0.0)
    notes = str(form.get('notes') or '').strip()
    if amount_value <= 0:
        return RedirectResponse(f'/gestionale/archiviati/{store_key}', status_code=HTTP_303_SEE_OTHER)
    category_clean = category or _auto_expense_category('import txt', supplier, notes) or 'Spese secondarie'
    ph = _ph()
    with connect() as conn:
        cur = conn.cursor()
        if not _is_archived_store_key(store_key, cur):
            return RedirectResponse('/gestionale/archiviati?error=' + quote_plus('Negozio archiviato non trovato.'), status_code=HTTP_303_SEE_OTHER)
        cur.execute(
            f"INSERT INTO cash_expenses(store, flow_date, category, supplier, payment_method, amount, notes, created_by) VALUES({ph},{ph},{ph},{ph},{ph},{ph},{ph},{ph})",
            (store_key, flow_date, category_clean, supplier, payment_method, amount_value, notes, user['username']),
        )
        _log(cur, store=store_key, username=user['username'], action='CREATE', category='USCITE ARCHIVIATE', name=f"Uscita storica {flow_date} - {category_clean}", delta=-amount_value)
    return RedirectResponse(f'/gestionale/archiviati/{store_key}', status_code=HTTP_303_SEE_OTHER)


@app.post("/gestionale/archiviati/{store_key}/import-txt")
async def archived_store_import_txt(request: Request, store_key: str):
    user = require_login(request)
    if not user:
        return RedirectResponse("/", status_code=HTTP_303_SEE_OTHER)
    if not can_view_management_finance(request, user):
        return RedirectResponse('/gestionale', status_code=HTTP_303_SEE_OTHER)
    with connect() as conn:
        cur = conn.cursor()
        if not _is_archived_store_key(store_key, cur):
            return RedirectResponse('/gestionale/archiviati?error=' + quote_plus('Negozio archiviato non trovato.'), status_code=HTTP_303_SEE_OTHER)

    form = await request.form()
    upload = form.get('import_file') or form.get('upload_file')
    pasted_text = str(form.get('import_text') or '').strip()
    include_expenses = _is_truthy(form.get('import_expenses'))
    replace_existing_dates = _is_truthy(form.get('replace_existing_dates'))
    confirm_import = _is_truthy(form.get('confirm_import'))
    expense_category_overrides = {}
    if confirm_import:
        posted_expense_keys = list(form.getlist('expense_key'))
        posted_expense_categories = list(form.getlist('expense_category'))
        allowed_categories = {_normalize_signature(x): x for x in _expense_category_options()}
        for idx, key in enumerate(posted_expense_keys):
            key = str(key or '').strip()
            category = str(posted_expense_categories[idx] if idx < len(posted_expense_categories) else '').strip()
            if not key or not category:
                continue
            expense_category_overrides[key] = allowed_categories.get(_normalize_signature(category), category[:80])

    text_parts = []
    if getattr(upload, 'filename', ''):
        try:
            raw_bytes = await upload.read()
            uploaded_text = _decode_uploaded_text(raw_bytes)
            if uploaded_text.strip():
                text_parts.append(uploaded_text)
        except Exception as e:
            msg = quote_plus(f'File non leggibile: {e}')
            return RedirectResponse(f'/gestionale/archiviati/{store_key}?import_error={msg}', status_code=HTTP_303_SEE_OTHER)
    if pasted_text.strip():
        text_parts.append(pasted_text)
    raw_text = '\n'.join(text_parts)
    if not raw_text.strip():
        msg = quote_plus('Carica un file TXT/CSV oppure incolla il testo da importare.')
        return RedirectResponse(f'/gestionale/archiviati/{store_key}?import_error={msg}', status_code=HTTP_303_SEE_OTHER)

    try:
        blocks = _parse_import_txt_blocks(raw_text, store_key)
        # In un negozio archiviato il testo importato deve finire sempre in quel negozio,
        # anche se dentro le note compare Spinza/Reburger o Camaldoli.
        for block in blocks:
            block['store'] = store_key
    except Exception as e:
        msg = quote_plus(f'Errore durante la lettura del testo: {e}')
        return RedirectResponse(f'/gestionale/archiviati/{store_key}?import_error={msg}', status_code=HTTP_303_SEE_OTHER)

    if not blocks:
        msg = quote_plus('Non ho trovato giornate valide nel testo. Controlla che ci siano date e righe tipo Pos 249€ o Cash 55€.')
        return RedirectResponse(f'/gestionale/archiviati/{store_key}?import_error={msg}', status_code=HTTP_303_SEE_OTHER)

    if not confirm_import:
        preview = _build_import_preview(blocks, raw_text, store_key, include_expenses, replace_existing_dates)
        ctx = _archived_store_detail_context(request, user, store_key, import_preview=preview)
        if not ctx:
            return RedirectResponse('/gestionale/archiviati', status_code=HTTP_303_SEE_OTHER)
        return render("archived_store_detail.html", **ctx)

    imported_entries = 0
    imported_expenses = 0
    try:
        ph = _ph()
        with connect() as conn:
            cur = conn.cursor()
            if replace_existing_dates:
                unique_days = sorted({str(b.get('date') or '') for b in blocks if b.get('date')})
                for flow_date in unique_days:
                    cur.execute(f"DELETE FROM cash_entries WHERE store={ph} AND flow_date={ph}", (store_key, flow_date))
                    if include_expenses:
                        cur.execute(f"DELETE FROM cash_expenses WHERE store={ph} AND flow_date={ph}", (store_key, flow_date))
            for block in blocks:
                flow_date = str(block.get('date') or '').strip()
                if not flow_date:
                    continue
                block_notes = _join_unique_notes(block.get('notes') or [])
                for income in block.get('incomes') or []:
                    payment_method = str(income.get('payment_method') or '').strip().lower()
                    amount = _safe_amount(income.get('amount'), 0.0)
                    if not payment_method or amount <= 0:
                        continue
                    raw_line = str(income.get('raw') or '').strip()
                    entry_notes = _join_unique_notes([block_notes, raw_line if ('(' in raw_line or ' x ' in raw_line.lower()) else ''])
                    _ensure_payment_method(cur, payment_method, user['username'])
                    _insert_cash_entry_compat(cur, store_key, flow_date, payment_method, amount, entry_notes, user['username'])
                    _log(cur, store=store_key, username=user['username'], action='CREATE', category='CASSA ARCHIVIATA', name=f"Import storico {flow_date} - {payment_method}", delta=float(amount or 0))
                    imported_entries += 1
                if include_expenses:
                    for expense_idx, expense in enumerate(block.get('expenses') or []):
                        amount = _safe_amount(expense.get('amount'), 0.0)
                        if amount <= 0:
                            continue
                        raw_line = str(expense.get('raw') or '').strip()
                        supplier = str(expense.get('name') or raw_line or 'Import TXT').strip()[:120]
                        override_key = _import_expense_override_key(block, expense_idx, store_key)
                        selected_category = expense_category_overrides.get(override_key)
                        auto_category = selected_category or _auto_expense_category('import txt', supplier, raw_line)
                        expense_notes = _join_unique_notes([raw_line, block_notes])
                        _insert_cash_expense_compat(cur, store_key, flow_date, auto_category, supplier, '', amount, expense_notes, user['username'])
                        _log(cur, store=store_key, username=user['username'], action='CREATE', category='USCITE ARCHIVIATE', name=f"Import storico {flow_date} - {supplier}", delta=-amount)
                        imported_expenses += 1
    except Exception as e:
        msg = quote_plus(f'Import fallito: {e}')
        return RedirectResponse(f'/gestionale/archiviati/{store_key}?import_error={msg}', status_code=HTTP_303_SEE_OTHER)

    if imported_entries == 0 and imported_expenses == 0:
        msg = quote_plus('Testo letto, ma non ho trovato importi validi da salvare.')
        return RedirectResponse(f'/gestionale/archiviati/{store_key}?import_error={msg}', status_code=HTTP_303_SEE_OTHER)
    return RedirectResponse(f'/gestionale/archiviati/{store_key}?imported_entries={imported_entries}&imported_expenses={imported_expenses}', status_code=HTTP_303_SEE_OTHER)


@app.post("/gestionale/archiviati/{store_key}/incassi/{entry_id}/delete")
def archived_store_delete_income(request: Request, store_key: str, entry_id: int):
    user = require_login(request)
    if not user:
        return RedirectResponse("/", status_code=HTTP_303_SEE_OTHER)
    if not can_view_management_finance(request, user):
        return RedirectResponse('/gestionale', status_code=HTTP_303_SEE_OTHER)
    ph = _ph()
    with connect() as conn:
        cur = conn.cursor()
        if _is_archived_store_key(store_key, cur):
            cur.execute(f"DELETE FROM cash_entries WHERE id={ph} AND store={ph}", (entry_id, store_key))
    return RedirectResponse(f'/gestionale/archiviati/{store_key}', status_code=HTTP_303_SEE_OTHER)


@app.post("/gestionale/archiviati/{store_key}/uscite/{expense_id}/delete")
def archived_store_delete_expense(request: Request, store_key: str, expense_id: int):
    user = require_login(request)
    if not user:
        return RedirectResponse("/", status_code=HTTP_303_SEE_OTHER)
    if not can_view_management_finance(request, user):
        return RedirectResponse('/gestionale', status_code=HTTP_303_SEE_OTHER)
    ph = _ph()
    with connect() as conn:
        cur = conn.cursor()
        if _is_archived_store_key(store_key, cur):
            cur.execute(f"DELETE FROM cash_expenses WHERE id={ph} AND store={ph}", (expense_id, store_key))
    return RedirectResponse(f'/gestionale/archiviati/{store_key}', status_code=HTTP_303_SEE_OTHER)


@app.get("/gestionale/panoramica-totale", response_class=HTMLResponse)
def total_overview_page(request: Request, year: str = 'ALL', store: str = 'ALL'):
    user = require_login(request)
    if not user:
        return RedirectResponse("/", status_code=HTTP_303_SEE_OTHER)
    if not can_view_management_finance(request, user):
        return RedirectResponse("/gestionale", status_code=HTTP_303_SEE_OTHER)
    ph = _ph()
    year_expr = "SUBSTR(CAST(flow_date AS TEXT),1,4)"
    with connect() as conn:
        cur = conn.cursor()
        archived_map = _archived_stores_map(cur)
        store_labels = _all_management_stores(cur, include_archived=True)
        valid_store_keys = set(store_labels.keys())
        selected_store = store if store in valid_store_keys else 'ALL'
        year_rows = []
        for table_name in ('cash_entries', 'cash_expenses'):
            year_rows += _dict_rows(cur, f"SELECT DISTINCT {year_expr} AS year FROM {table_name} WHERE flow_date IS NOT NULL", ())
        years = sorted({str(r.get('year') or '').strip() for r in year_rows if str(r.get('year') or '').strip()}, reverse=True)
        selected_year = year if year in years else 'ALL'

        def _where_for(prefix_params=False):
            clauses = []
            params = []
            if selected_store != 'ALL':
                clauses.append(f"store={ph}")
                params.append(selected_store)
            if selected_year != 'ALL':
                clauses.append(f"{year_expr}={ph}")
                params.append(selected_year)
            return ('WHERE ' + ' AND '.join(clauses)) if clauses else '', tuple(params)

        where_sql, params = _where_for()
        total_income = _fetch_one_float(cur, f"SELECT COALESCE(SUM(amount),0) FROM cash_entries {where_sql}", params)
        total_expense = _fetch_one_float(cur, f"SELECT COALESCE(SUM(amount),0) FROM cash_expenses {where_sql}", params)
        totals = {
            'income': total_income,
            'expense': total_expense,
            'net': total_income - total_expense,
            'entries_count': _fetch_one_int(cur, f"SELECT COUNT(*) FROM cash_entries {where_sql}", params),
            'expenses_count': _fetch_one_int(cur, f"SELECT COUNT(*) FROM cash_expenses {where_sql}", params),
        }

        income_by_year = _dict_rows(cur, f"SELECT {year_expr} AS year, COALESCE(SUM(amount),0) AS income FROM cash_entries {where_sql} GROUP BY {year_expr}", params)
        expense_by_year = _dict_rows(cur, f"SELECT {year_expr} AS year, COALESCE(SUM(amount),0) AS expense FROM cash_expenses {where_sql} GROUP BY {year_expr}", params)
        year_map = {}
        for r in income_by_year:
            y = str(r.get('year') or 'Senza anno')
            year_map.setdefault(y, {'year': y, 'income': 0.0, 'expense': 0.0, 'net': 0.0})['income'] += float(r.get('income') or 0)
        for r in expense_by_year:
            y = str(r.get('year') or 'Senza anno')
            year_map.setdefault(y, {'year': y, 'income': 0.0, 'expense': 0.0, 'net': 0.0})['expense'] += float(r.get('expense') or 0)
        for r in year_map.values():
            r['net'] = r['income'] - r['expense']
        by_year = sorted(year_map.values(), key=lambda x: x.get('year') or '', reverse=True)

        income_by_store = _dict_rows(cur, f"SELECT store, COALESCE(SUM(amount),0) AS income, COUNT(*) AS entries_count FROM cash_entries {where_sql} GROUP BY store", params)
        expense_by_store = _dict_rows(cur, f"SELECT store, COALESCE(SUM(amount),0) AS expense, COUNT(*) AS expenses_count FROM cash_expenses {where_sql} GROUP BY store", params)
        store_map = {}
        for r in income_by_store:
            key = str(r.get('store') or '')
            item = store_map.setdefault(key, {'store': key, 'label': store_labels.get(key) or _store_label(key), 'kind': _store_kind(key, archived_map), 'income': 0.0, 'expense': 0.0, 'net': 0.0, 'entries_count': 0, 'expenses_count': 0})
            item['income'] += float(r.get('income') or 0)
            item['entries_count'] += int(r.get('entries_count') or 0)
        for r in expense_by_store:
            key = str(r.get('store') or '')
            item = store_map.setdefault(key, {'store': key, 'label': store_labels.get(key) or _store_label(key), 'kind': _store_kind(key, archived_map), 'income': 0.0, 'expense': 0.0, 'net': 0.0, 'entries_count': 0, 'expenses_count': 0})
            item['expense'] += float(r.get('expense') or 0)
            item['expenses_count'] += int(r.get('expenses_count') or 0)
        for item in store_map.values():
            item['net'] = item['income'] - item['expense']
            item['years'] = set()
        # Anni presenti per negozio, senza filtri di importo.
        for table_name in ('cash_entries', 'cash_expenses'):
            rows = _dict_rows(cur, f"SELECT store, {year_expr} AS year FROM {table_name} {where_sql} GROUP BY store, {year_expr}", params)
            for r in rows:
                key = str(r.get('store') or '')
                if key in store_map and r.get('year'):
                    store_map[key].setdefault('years', set()).add(str(r.get('year')))
        by_store = sorted(store_map.values(), key=lambda x: (0 if x.get('kind') == 'attivo' else 1, x.get('label') or ''))
        for item in by_store:
            item['years_text'] = ', '.join(sorted(item.get('years') or [], reverse=True)) or '—'

        active_income = sum(x['income'] for x in by_store if x.get('kind') == 'attivo')
        active_expense = sum(x['expense'] for x in by_store if x.get('kind') == 'attivo')
        archived_income = sum(x['income'] for x in by_store if x.get('kind') == 'archiviato')
        archived_expense = sum(x['expense'] for x in by_store if x.get('kind') == 'archiviato')
        split_totals = {
            'active_income': active_income,
            'active_expense': active_expense,
            'active_net': active_income - active_expense,
            'archived_income': archived_income,
            'archived_expense': archived_expense,
            'archived_net': archived_income - archived_expense,
        }

    return render(
        "total_overview.html",
        user=user,
        active_store=request.session.get('active_store') if is_admin(request) else None,
        totals=totals,
        split_totals=split_totals,
        by_year=by_year,
        by_store=by_store,
        years=years,
        store_options=store_labels,
        selected_year=selected_year,
        selected_store=selected_store,
    )

@app.get("/gestionale/incassi", response_class=HTMLResponse)
def cash_entries_page(request: Request, flow_date: str = '', payment_method: str = 'ALL', period_type: str = 'month', anchor_date: str = '', month_key: str = '', nav: str = '', imported_entries: int = 0, imported_expenses: int = 0, import_error: str = ''):
    user = require_login(request)
    if not user:
        return RedirectResponse("/", status_code=HTTP_303_SEE_OTHER)
    if not can_view_management_finance(request, user):
        return RedirectResponse("/gestionale", status_code=HTTP_303_SEE_OTHER)
    return render(
        "cashflow_entries.html",
        **_cash_entries_page_context(
            request, user,
            flow_date=flow_date,
            payment_method=payment_method,
            period_type=period_type,
            anchor_date=anchor_date,
            month_key=month_key,
            nav=nav,
            imported_entries=imported_entries,
            imported_expenses=imported_expenses,
            import_error=import_error,
        )
    )


@app.post("/gestionale/incassi")
async def cash_entries_create(request: Request):
    user = require_login(request)
    if not user:
        return RedirectResponse("/", status_code=HTTP_303_SEE_OTHER)
    if not can_view_management_finance(request, user):
        return RedirectResponse("/gestionale", status_code=HTTP_303_SEE_OTHER)
    form = await request.form()
    flow_date = str(form.get('flow_date') or '').strip() or date.today().isoformat()
    store = str(form.get('store') or '').strip()
    notes = str(form.get('notes') or '').strip()
    if store not in STORES:
        store = user.get('store') or 'spinza'
    if not is_admin(request):
        store = user.get('store') or store

    method_amounts = []
    base_names = list(form.getlist('method_name'))
    base_amounts = list(form.getlist('amount_value'))
    for idx, method_name in enumerate(base_names):
        clean_method = ((method_name or '').strip())
        amount = _safe_amount(base_amounts[idx] if idx < len(base_amounts) else 0, 0.0)
        if clean_method and amount > 0:
            method_amounts.append((clean_method, amount))

    extra_names = list(form.getlist('custom_method_name'))
    extra_amounts = list(form.getlist('custom_method_amount'))
    for idx, name in enumerate(extra_names):
        name = (name or '').strip()
        amount = _safe_amount(extra_amounts[idx] if idx < len(extra_amounts) else 0, 0.0)
        if name and amount > 0:
            method_amounts.append((name, amount))

    # Campi vuoti = 0. Se non c'è nessun importo valido, torniamo alla pagina senza errore.
    if not method_amounts:
        return RedirectResponse('/gestionale/incassi', status_code=HTTP_303_SEE_OTHER)

    with connect() as conn:
        cur = conn.cursor()
        for payment_method, amount in method_amounts:
            _ensure_payment_method(cur, payment_method, user['username'])
            _insert_cash_entry_compat(cur, store, flow_date, payment_method, amount, notes, user['username'])
            _log(cur, store=store, username=user['username'], action='CREATE', category='CASSA', name=f"Incasso {flow_date} - {payment_method}", delta=float(amount or 0))
    return RedirectResponse('/gestionale/incassi', status_code=HTTP_303_SEE_OTHER)

    ph = _ph()
    with connect() as conn:
        cur = conn.cursor()
        for payment_method, amount in method_amounts:
            _ensure_payment_method(cur, payment_method, user['username'])
            cur.execute(
                f"INSERT INTO cash_entries(store, flow_date, payment_method, amount, orders_count, notes, created_by) VALUES({ph},{ph},{ph},{ph},0,{ph},{ph})",
                (store, flow_date, payment_method, amount, notes, user['username']),
            )
            _log(cur, store=store, username=user['username'], action='CREATE', category='CASSA', name=f"Incasso {flow_date} - {payment_method}", delta=float(amount or 0))
    return RedirectResponse('/gestionale/incassi', status_code=HTTP_303_SEE_OTHER)


@app.post("/gestionale/incassi/importa-file")
@app.post("/gestionale/incassi/import-txt")
async def cash_entries_import_txt(request: Request):
    user = require_login(request)
    if not user:
        return RedirectResponse("/", status_code=HTTP_303_SEE_OTHER)
    if not can_view_management_finance(request, user):
        return RedirectResponse('/gestionale', status_code=HTTP_303_SEE_OTHER)

    form = await request.form()
    # Compatibile sia con il nuovo form (import_file/fallback_store) sia con il vecchio form (upload_file/store).
    upload = form.get('import_file') or form.get('upload_file')
    pasted_text = str(form.get('import_text') or '').strip()
    include_expenses = _is_truthy(form.get('import_expenses'))
    replace_existing_dates = _is_truthy(form.get('replace_existing_dates'))
    confirm_import = _is_truthy(form.get('confirm_import'))
    expense_category_overrides = {}
    if confirm_import:
        posted_expense_keys = list(form.getlist('expense_key'))
        posted_expense_categories = list(form.getlist('expense_category'))
        allowed_categories = {_normalize_signature(x): x for x in _expense_category_options()}
        for idx, key in enumerate(posted_expense_keys):
            key = str(key or '').strip()
            category = str(posted_expense_categories[idx] if idx < len(posted_expense_categories) else '').strip()
            if not key or not category:
                continue
            normalized = _normalize_signature(category)
            expense_category_overrides[key] = allowed_categories.get(normalized, category[:80])

    fallback_store = str(form.get('fallback_store') or form.get('store') or '').strip()
    if fallback_store not in STORES:
        fallback_store = (request.session.get('active_store') if is_admin(request) else user.get('store')) or 'spinza'
    if fallback_store not in STORES:
        fallback_store = 'spinza'

    text_parts = []
    filename = ''
    if getattr(upload, 'filename', ''):
        try:
            raw_bytes = await upload.read()
            uploaded_text = _decode_uploaded_text(raw_bytes)
            if uploaded_text.strip():
                text_parts.append(uploaded_text)
            filename = str(getattr(upload, 'filename', '') or '').strip()
        except Exception as e:
            msg = quote_plus(f'File non leggibile: {e}')
            return RedirectResponse(f'/gestionale/incassi?import_error={msg}', status_code=HTTP_303_SEE_OTHER)
    if pasted_text.strip():
        text_parts.append(pasted_text)

    raw_text = '\n'.join(text_parts)
    if not raw_text.strip():
        msg = quote_plus('Carica un file TXT/CSV oppure incolla il testo da importare.')
        return RedirectResponse(f'/gestionale/incassi?import_error={msg}', status_code=HTTP_303_SEE_OTHER)

    try:
        blocks = _parse_import_txt_blocks(raw_text, fallback_store)
    except Exception as e:
        msg = quote_plus(f'Errore durante la lettura del file: {e}')
        return RedirectResponse(f'/gestionale/incassi?import_error={msg}', status_code=HTTP_303_SEE_OTHER)

    if not blocks:
        msg = quote_plus('Non ho trovato giornate valide nel file. Controlla che ci siano date tipo 01/12/25 e righe come Pos 249€ o Cash 55€.')
        return RedirectResponse(f'/gestionale/incassi?import_error={msg}', status_code=HTTP_303_SEE_OTHER)

    if not is_admin(request):
        forced_store = user.get('store') or fallback_store
        for block in blocks:
            block['store'] = forced_store

    if not confirm_import:
        preview = _build_import_preview(blocks, raw_text, fallback_store, include_expenses, replace_existing_dates)
        first_date = str((blocks[0] or {}).get('date') or '') if blocks else ''
        return render(
            "cashflow_entries.html",
            **_cash_entries_page_context(
                request, user,
                period_type='month',
                anchor_date=first_date,
                month_key=_month_key_from_value(first_date) if first_date else '',
                import_preview=preview,
            )
        )

    imported_entries = 0
    imported_expenses = 0
    try:
        with connect() as conn:
            cur = conn.cursor()
            ph = _ph()

            if replace_existing_dates:
                unique_days = sorted({((b.get('store') or fallback_store), str(b.get('date') or '')) for b in blocks if b.get('date')})
                for store_key, flow_date in unique_days:
                    cur.execute(f"DELETE FROM cash_entries WHERE store={ph} AND flow_date={ph}", (store_key, flow_date))
                    if include_expenses:
                        cur.execute(f"DELETE FROM cash_expenses WHERE store={ph} AND flow_date={ph}", (store_key, flow_date))

            for block in blocks:
                flow_date = str(block.get('date') or '').strip()
                store_key = str(block.get('store') or fallback_store).strip()
                if not flow_date:
                    continue
                if store_key not in STORES:
                    store_key = fallback_store

                block_notes = _join_unique_notes(block.get('notes') or [])
                incomes = block.get('incomes') or []
                expenses = block.get('expenses') or []

                for income in incomes:
                    payment_method = str(income.get('payment_method') or '').strip().lower()
                    amount = _safe_amount(income.get('amount'), 0.0)
                    if not payment_method or amount <= 0:
                        continue
                    raw_line = str(income.get('raw') or '').strip()
                    entry_notes = _join_unique_notes([block_notes, raw_line if ('(' in raw_line or ' x ' in raw_line.lower()) else ''])
                    _ensure_payment_method(cur, payment_method, user['username'])
                    _insert_cash_entry_compat(cur, store_key, flow_date, payment_method, amount, entry_notes, user['username'])
                    _log(cur, store=store_key, username=user['username'], action='CREATE', category='CASSA', name=f"Import TXT {flow_date} - {payment_method}", delta=float(amount or 0))
                    imported_entries += 1

                if include_expenses:
                    for expense_idx, expense in enumerate(expenses):
                        amount = _safe_amount(expense.get('amount'), 0.0)
                        if amount <= 0:
                            continue
                        raw_line = str(expense.get('raw') or '').strip()
                        supplier = str(expense.get('name') or raw_line or 'Import TXT').strip()[:120]
                        expense_notes = _join_unique_notes([raw_line, block_notes])
                        override_key = _import_expense_override_key(block, expense_idx, fallback_store)
                        selected_category = expense_category_overrides.get(override_key)
                        auto_category = selected_category or _auto_expense_category('import txt', supplier, raw_line)
                        _insert_cash_expense_compat(cur, store_key, flow_date, auto_category, supplier, '', amount, expense_notes, user['username'])
                        _log(cur, store=store_key, username=user['username'], action='CREATE', category='USCITE', name=f"Import TXT {flow_date} - {supplier}", delta=float(amount or 0))
                        imported_expenses += 1
    except Exception as e:
        msg = quote_plus(f'Import fallito: {e}')
        return RedirectResponse(f'/gestionale/incassi?import_error={msg}', status_code=HTTP_303_SEE_OTHER)

    if imported_entries == 0 and imported_expenses == 0:
        msg = quote_plus('File letto, ma non ho trovato importi validi da salvare.')
        return RedirectResponse(f'/gestionale/incassi?import_error={msg}', status_code=HTTP_303_SEE_OTHER)

    query = f'/gestionale/incassi?imported_entries={imported_entries}&imported_expenses={imported_expenses}'
    if filename and blocks:
        query += f'&anchor_date={quote_plus(str(blocks[-1].get("date") or ""))}'
    return RedirectResponse(query, status_code=HTTP_303_SEE_OTHER)


@app.post("/gestionale/uscite/ricategorizza")
def cash_expenses_recategorize(request: Request):
    user = require_login(request)
    if not user:
        return RedirectResponse("/", status_code=HTTP_303_SEE_OTHER)
    if not can_view_management_finance(request, user):
        return RedirectResponse("/gestionale", status_code=HTTP_303_SEE_OTHER)
    brand = _current_store_scope(request, user)
    fixed = _recategorize_existing_cash_expenses(brand)
    return RedirectResponse(f"/gestionale/uscite?recategorized_count={fixed}", status_code=HTTP_303_SEE_OTHER)


@app.get("/gestionale/uscite", response_class=HTMLResponse)


def cash_expenses_page(request: Request, flow_date: str = "", category: str = "ALL", period_type: str = 'month', anchor_date: str = '', month_key: str = '', nav: str = '', recategorized_count: int = -1):
    user = require_login(request)
    if not user:
        return RedirectResponse("/", status_code=HTTP_303_SEE_OTHER)
    if not can_view_management_finance(request, user):
        return RedirectResponse("/gestionale", status_code=HTTP_303_SEE_OTHER)
    brand = _current_store_scope(request, user)
    active_store = request.session.get("active_store") if is_admin(request) else None
    can_edit_management = user.get('role') in ('admin', 'manager', 'staff')
    period_type, selected_month, anchor_day = _resolve_period_state(period_type, month_key, anchor_date, nav)
    anchor_s = anchor_day.isoformat()
    where_sql, params = _cash_scope_where(brand)
    ph = _ph()
    sql = f"SELECT id, flow_date, store, category, supplier, payment_method, amount, notes, created_by FROM cash_expenses WHERE {where_sql}"
    qparams = list(params)
    if flow_date:
        sql += f" AND flow_date={ph}"
        qparams.append(flow_date)
    if category and category != 'ALL':
        sql += f" AND category={ph}"
        qparams.append(category)
    sql += " ORDER BY ts DESC, id DESC"
    chart_store = brand if brand != 'ALL' else (active_store or 'spinza')
    with connect() as conn:
        cur = conn.cursor()
        rows = _dict_rows(cur, sql, tuple(qparams))
        categories = [r['category'] for r in _dict_rows(cur, f"SELECT DISTINCT category FROM cash_expenses WHERE {where_sql} ORDER BY category ASC", params) if r.get('category')]
        chart_rows = _build_store_period_chart(cur, chart_store, period_type, anchor_s)
        _, page_totals, _, _, period_meta, page_insights = _build_cash_dashboard(cur, chart_store, period_type, anchor_s)
        month_options = _month_options_for_scope(cur, brand, selected_month)
        total_amount = sum(float(r.get('amount') or 0) for r in rows)
        expense_overview = _build_expense_overview(cur, chart_store, period_type, anchor_s)
    prev_anchor = _shift_anchor_date(period_type, anchor_day, -1).isoformat()
    next_anchor = _shift_anchor_date(period_type, anchor_day, 1).isoformat()
    nav_prev_url = _query_url('/gestionale/uscite', flow_date=flow_date, category=category, period_type=period_type, month_key=_month_key_from_value(prev_anchor), anchor_date=prev_anchor)
    nav_next_url = _query_url('/gestionale/uscite', flow_date=flow_date, category=category, period_type=period_type, month_key=_month_key_from_value(next_anchor), anchor_date=next_anchor)
    return render(
        "cashflow_expenses.html",
        user=user, brand=brand, active_store=active_store, stores=STORES,
        can_edit_management=can_edit_management, rows=rows, today=date.today().isoformat(),
        selected_date=flow_date, selected_category=category, categories=categories,
        total_amount=total_amount, chart_rows=chart_rows, chart_store=chart_store,
        selected_period_type=period_type, selected_anchor_date=anchor_s, page_period_meta=period_meta, page_totals=page_totals,
        month_options=month_options, selected_month=selected_month, nav_prev_url=nav_prev_url, nav_next_url=nav_next_url,
        page_insights=page_insights, expense_overview=expense_overview, recategorized_count=recategorized_count,
    )



@app.post("/gestionale/uscite")
def cash_expenses_create(
    request: Request,
    flow_date: str = Form(...),
    store: str = Form(...),
    category: str = Form(""),
    supplier: str = Form(""),
    payment_method: str = Form(""),
    amount: str = Form(''),
    notes: str = Form(""),
):
    user = require_login(request)
    if not user:
        return RedirectResponse("/", status_code=HTTP_303_SEE_OTHER)
    if not can_view_management_finance(request, user):
        return RedirectResponse("/gestionale", status_code=HTTP_303_SEE_OTHER)
    if store not in STORES:
        store = user.get('store') or 'spinza'
    if not is_admin(request):
        store = user.get('store') or store
    amount_value = _safe_amount(amount)
    if amount_value <= 0:
        return RedirectResponse('/gestionale/uscite', status_code=HTTP_303_SEE_OTHER)
    category_clean = (category or '').strip()
    supplier_clean = (supplier or '').strip()
    notes_clean = (notes or '').strip()
    auto_category = _auto_expense_category(category_clean, supplier_clean, notes_clean)
    # Se la categoria è vuota/generica o le parole forti indicano chiaramente altro,
    # salvo direttamente la divisione giusta. Così non resta tutto in "Varie".
    if (not category_clean) or _should_auto_replace_expense_category(category_clean, supplier_clean, notes_clean):
        category_clean = auto_category or 'Spese secondarie'
    ph = _ph()
    with connect() as conn:
        cur = conn.cursor()
        cur.execute(
            f"INSERT INTO cash_expenses(store, flow_date, category, supplier, payment_method, amount, notes, created_by) VALUES({ph},{ph},{ph},{ph},{ph},{ph},{ph},{ph})",
            (store, flow_date, category_clean, supplier_clean, (payment_method or '').strip(), amount_value, notes_clean, user['username']),
        )
        _log(cur, store=store, username=user['username'], action='CREATE', category='CASSA', name=f"Uscita {flow_date} - {category_clean}", delta=-amount_value)
    return RedirectResponse('/gestionale/uscite', status_code=HTTP_303_SEE_OTHER)

@app.post("/gestionale/incassi/{entry_id}/delete")
def cash_entries_delete(request: Request, entry_id: int):
    user = require_login(request)
    if not user:
        return RedirectResponse("/", status_code=HTTP_303_SEE_OTHER)
    if not can_view_management_finance(request, user):
        return RedirectResponse('/gestionale', status_code=HTTP_303_SEE_OTHER)

    brand = _current_store_scope(request, user)
    where_sql, params = _cash_scope_where(brand)
    ph = _ph()
    with connect() as conn:
        cur = conn.cursor()
        row = cur.execute(
            f"SELECT id, store, flow_date, payment_method, amount FROM cash_entries WHERE id={ph} AND {where_sql}",
            (entry_id, *params),
        ).fetchone()
        if row:
            row = dict(row)
            cur.execute(f"DELETE FROM cash_entries WHERE id={ph}", (entry_id,))
            _log(
                cur,
                store=row.get('store') or (user.get('store') or 'spinza'),
                username=user['username'],
                action='DELETE',
                category='CASSA',
                name=f"Incasso eliminato {row.get('flow_date', '')} - {row.get('payment_method', '')}",
                delta=-float(row.get('amount') or 0),
            )
    return RedirectResponse('/gestionale/incassi', status_code=HTTP_303_SEE_OTHER)


@app.post("/gestionale/payment-methods/delete")
async def cash_payment_method_delete(request: Request):
    user = require_login(request)
    if not user:
        return RedirectResponse("/", status_code=HTTP_303_SEE_OTHER)
    if not can_view_management_finance(request, user):
        return RedirectResponse("/gestionale", status_code=HTTP_303_SEE_OTHER)
    form = await request.form()
    clean_name = str(form.get('method_name') or '').strip()
    if not clean_name:
        return RedirectResponse('/gestionale/incassi', status_code=HTTP_303_SEE_OTHER)
    if clean_name.lower() in {'contanti', 'pos', 'deliveroo', 'glovo', 'just eat'}:
        return RedirectResponse('/gestionale/incassi', status_code=HTTP_303_SEE_OTHER)
    ph = _ph()
    with connect() as conn:
        cur = conn.cursor()
        brand = _current_store_scope(request, user)
        cur.execute(f"DELETE FROM cash_payment_methods WHERE LOWER(name)=LOWER({ph})", (clean_name,))
        _log(cur, store=(brand if brand != 'ALL' else (request.session.get('active_store') or 'spinza')), username=user['username'], action='DELETE', category='CASSA', name=f"Metodo pagamento rimosso {clean_name}", delta=0)
        conn.commit()
    return RedirectResponse('/gestionale/incassi', status_code=HTTP_303_SEE_OTHER)



@app.post("/gestionale/uscite/{expense_id}/sposta-categoria")
async def cash_expenses_move_category(request: Request, expense_id: int):
    user = require_login(request)
    if not user:
        return RedirectResponse("/", status_code=HTTP_303_SEE_OTHER)
    if not can_view_management_finance(request, user):
        return RedirectResponse('/gestionale', status_code=HTTP_303_SEE_OTHER)

    form = await request.form()
    new_category = str(form.get('new_category') or '').strip()
    return_to = _safe_next_url(str(form.get('return_to') or request.headers.get('referer') or ''), '/gestionale')
    if not new_category:
        return RedirectResponse(return_to, status_code=HTTP_303_SEE_OTHER)

    brand = _current_store_scope(request, user)
    where_sql, params = _cash_scope_where(brand)
    ph = _ph()
    with connect() as conn:
        cur = conn.cursor()
        row = cur.execute(
            f"SELECT id, store, flow_date, category, supplier, notes, amount FROM cash_expenses WHERE id={ph} AND {where_sql}",
            (expense_id, *params),
        ).fetchone()
        if row:
            row = dict(row)
            old_category = row.get('category') or ''
            cur.execute(f"UPDATE cash_expenses SET category={ph} WHERE id={ph}", (new_category, expense_id))
            # Impara dalla correzione: se sposti Metro/Sogegross/Prinz ecc., aggiorna anche le vecchie voci simili.
            pattern_norm = _save_learned_expense_rule(
                cur,
                store=(brand if brand != 'ALL' else 'ALL'),
                supplier=str(row.get('supplier') or ''),
                notes=str(row.get('notes') or ''),
                category=new_category,
                username=user.get('username') or 'system',
            )
            similar_count = _apply_category_to_similar_expenses(cur, brand=brand, pattern_norm=pattern_norm, new_category=new_category)
            _log(
                cur,
                store=row.get('store') or (user.get('store') or 'spinza'),
                username=user['username'],
                action='UPDATE',
                category='USCITE',
                name=f"Spostata categoria uscita {row.get('flow_date','')} - {row.get('supplier','')} da {old_category} a {new_category}. Regola appresa: {pattern_norm}. Simili aggiornate: {similar_count}",
                delta=0,
            )
            conn.commit()
    return RedirectResponse(return_to, status_code=HTTP_303_SEE_OTHER)


@app.post("/gestionale/uscite/{expense_id}/delete")
def cash_expenses_delete(request: Request, expense_id: int):
    user = require_login(request)
    if not user:
        return RedirectResponse("/", status_code=HTTP_303_SEE_OTHER)
    if not can_view_management_finance(request, user):
        return RedirectResponse('/gestionale', status_code=HTTP_303_SEE_OTHER)

    brand = _current_store_scope(request, user)
    where_sql, params = _cash_scope_where(brand)
    ph = _ph()
    with connect() as conn:
        cur = conn.cursor()
        row = cur.execute(
            f"SELECT id, store, flow_date, category, amount FROM cash_expenses WHERE id={ph} AND {where_sql}",
            (expense_id, *params),
        ).fetchone()
        if row:
            row = dict(row)
            cur.execute(f"DELETE FROM cash_expenses WHERE id={ph}", (expense_id,))
            _log(
                cur,
                store=row.get('store') or (user.get('store') or 'spinza'),
                username=user['username'],
                action='DELETE',
                category='CASSA',
                name=f"Uscita eliminata {row.get('flow_date', '')} - {row.get('category', '')}",
                delta=float(row.get('amount') or 0),
            )
    return RedirectResponse('/gestionale/uscite', status_code=HTTP_303_SEE_OTHER)


@app.get("/inventario-home", response_class=HTMLResponse)
def inventario_home(request: Request):
    user = require_login(request)
    if not user:
        return RedirectResponse("/", status_code=HTTP_303_SEE_OTHER)
    return RedirectResponse("/select-area", status_code=HTTP_303_SEE_OTHER)

@app.get("/register", response_class=HTMLResponse)
def register_get(request: Request):
    store = get_selected_store(request)
    if not store or store not in STORES:
        return RedirectResponse("/select-store", status_code=HTTP_303_SEE_OTHER)
    return render("register.html", error=None, ok=False, user=None, store=store, store_label=STORES[store], brand=store)

@app.post("/register", response_class=HTMLResponse)
def register_post(
    request: Request,
    username: str = Form(...),
    password: str = Form(...),
    confirm_password: str = Form(...),
):
    username = username.strip()
    store = get_selected_store(request)
    if not store or store not in STORES:
        return RedirectResponse("/select-store", status_code=HTTP_303_SEE_OTHER)

    if not username:
        return render("register.html", error="Username non valido.", ok=False, user=None, store=store, store_label=STORES[store], brand=store)
    if password != confirm_password:
        return render("register.html", error="Le password non coincidono.", ok=False, user=None, store=store, store_label=STORES[store], brand=store)
    if len(password) < 4:
        return render("register.html", error="Password troppo corta.", ok=False, user=None, store=store, store_label=STORES[store], brand=store)

    salt, h = make_password(password)
    ph = _ph()

    with connect() as conn:
        cur = conn.cursor()
        exists = cur.execute(
            f"SELECT 1 FROM users WHERE username={ph} AND store={ph}",
            (username, store),
        ).fetchone()
        if exists:
            return render("register.html", error="Username già esistente.", ok=False, user=None, store=store, store_label=STORES[store], brand=store)

        cur.execute(
            f"INSERT INTO users(store, username, role, pw_salt, pw_hash, legacy_sha256) VALUES({ph},{ph},'staff',{ph},{ph},NULL)",
            (store, username, salt, h),
        )

    return render("register.html", error=None, ok=True, user=None, store=store, store_label=STORES[store], brand=store)


# =========================
# ADMIN LOGIN / ADMIN PANEL
# =========================
@app.get("/admin-login", response_class=HTMLResponse)
def admin_login_get(request: Request):
    return render("admin_login.html", error=None, user=None)

@app.post("/admin-login", response_class=HTMLResponse)
def admin_login_post(request: Request, username: str = Form(...), password: str = Form(...)):
    username = username.strip()
    ph = _ph()

    with connect() as conn:
        cur = conn.cursor()
        row = cur.execute(
            f"SELECT * FROM users WHERE username={ph} AND role='admin'",
            (username,),
        ).fetchone()

        if not row:
            return render("admin_login.html", error="Credenziali admin non valide.", user=None)

        ok = False
        if row.get("pw_salt") and row.get("pw_hash"):
            ok = verify_password(password, row["pw_salt"], row["pw_hash"])
        elif row.get("legacy_sha256"):
            ok = (legacy_sha256(password) == row["legacy_sha256"])
            if ok:
                salt, h = make_password(password)
                cur.execute(
                    f"UPDATE users SET pw_salt={ph}, pw_hash={ph}, legacy_sha256=NULL WHERE id={ph}",
                    (salt, h, row["id"]),
                )

        if not ok:
            return render("admin_login.html", error="Credenziali admin non valide.", user=None)

        request.session["user"] = {"id": row["id"], "username": row["username"], "role": row["role"]}

    return RedirectResponse("/admin", status_code=HTTP_303_SEE_OTHER)

@app.get("/admin", response_class=HTMLResponse)
def admin_page(request: Request, store: str = ""):
    user = require_login(request)
    if not user:
        return RedirectResponse("/", status_code=HTTP_303_SEE_OTHER)
    if not is_admin(request):
        return RedirectResponse("/inventario", status_code=HTTP_303_SEE_OTHER)

    store = (store or "").strip()
    if store in STORES:
        request.session["admin_store"] = store

    with connect() as conn:
        cur = conn.cursor()
        users = cur.execute(
            "SELECT id, store, username, role, last_seen FROM users ORDER BY role DESC, store, username"
        ).fetchall()

    return _render_admin(request, user=user, users=users)

@app.post("/admin/users/add", response_class=HTMLResponse)
def admin_add_user(
    request: Request,
    username: str = Form(...),
    password: str = Form(...),
    confirm_password: str = Form(...),
    role: str = Form("staff"),
):
    user = require_login(request)
    if not user:
        return RedirectResponse("/", status_code=HTTP_303_SEE_OTHER)
    if not is_admin(request):
        return RedirectResponse("/inventario", status_code=HTTP_303_SEE_OTHER)

    username = (username or "").strip()
    role = (role or "staff").strip().lower()
    if role not in ("staff", "admin"):
        role = "staff"

    if not username:
        return RedirectResponse("/admin", status_code=HTTP_303_SEE_OTHER)
    if password != confirm_password:
        return _admin_users_render_error(request, user, "Le password non coincidono.")
    if len(password) < 4:
        return _admin_users_render_error(request, user, "Password troppo corta.")

    # Gli account staff sono legati all'inventario selezionato.
    # Gli account admin invece entrano dal login admin e hanno accesso completo.
    store = (request.session.get("admin_store") or "spinza")
    if store not in STORES:
        store = "spinza"
    if role == "admin":
        store = "spinza"

    salt, h = make_password(password)
    ph = _ph()

    with connect() as conn:
        cur = conn.cursor()

        if role == "admin":
            exists = cur.execute(
                f"SELECT 1 FROM users WHERE username={ph}",
                (username,),
            ).fetchone()
            if exists:
                users = cur.execute("SELECT id, store, username, role, last_seen FROM users ORDER BY role DESC, store, username").fetchall()
                return _render_admin(request, user=user, users=users, error="Username già esistente. Per gli admin usa uno username unico.")
        else:
            exists = cur.execute(
                f"SELECT 1 FROM users WHERE username={ph} AND store={ph}",
                (username, store),
            ).fetchone()
            if exists:
                users = cur.execute("SELECT id, store, username, role, last_seen FROM users ORDER BY role DESC, store, username").fetchall()
                return _render_admin(request, user=user, users=users, error="Username già esistente in questo inventario.")

        cur.execute(
            f"INSERT INTO users(store, username, role, pw_salt, pw_hash, legacy_sha256) VALUES({ph},{ph},{ph},{ph},{ph},NULL)",
            (store, username, role, salt, h),
        )
        users = cur.execute("SELECT id, store, username, role, last_seen FROM users ORDER BY role DESC, store, username").fetchall()

    if role == "admin":
        return _render_admin(request, user=user, users=users, msg=f"Account admin '{username}' creato. Può entrare da /admin-login.")
    return _render_admin(request, user=user, users=users, msg=f"Dipendente '{username}' creato per {STORES.get(store, store)}.")

@app.post("/admin/users/{user_id}/username", response_class=HTMLResponse)
def admin_change_username(
    request: Request,
    user_id: int,
    new_username: str = Form(...),
):
    user = require_login(request)
    if not user:
        return RedirectResponse("/", status_code=HTTP_303_SEE_OTHER)
    if not is_admin(request):
        return RedirectResponse("/inventario", status_code=HTTP_303_SEE_OTHER)

    new_username = (new_username or "").strip()
    if not new_username:
        return _admin_users_render_error(request, user, "Username non valido.")

    ph = _ph()
    with connect() as conn:
        cur = conn.cursor()
        target = cur.execute(
            f"SELECT id, store, username, role FROM users WHERE id={ph}",
            (int(user_id),),
        ).fetchone()
        if not target:
            return _admin_users_render_error(request, user, "Utente non trovato.")
        if target["role"] == "admin":
            return _admin_users_render_error(request, user, "Lo username admin si cambia dal Profilo.")

        exists = cur.execute(
            f"SELECT 1 FROM users WHERE store={ph} AND username={ph} AND id<>{ph}",
            (target["store"], new_username, int(user_id)),
        ).fetchone()
        if exists:
            return _admin_users_render_error(request, user, "Username già esistente in questo negozio.")

        cur.execute(
            f"UPDATE users SET username={ph} WHERE id={ph}",
            (new_username, int(user_id)),
        )

        users = cur.execute("SELECT id, store, username, role, last_seen FROM users ORDER BY role DESC, store, username").fetchall()

    return _render_admin(
        request,
        user=user,
        users=users,
        msg=f"Username aggiornato: '{target['username']}' → '{new_username}' ({STORES.get(target['store'], target['store'])}).",
    )

@app.post("/admin/users/{user_id}/password", response_class=HTMLResponse)
def admin_change_password(
    request: Request,
    user_id: int,
    new_password: str = Form(...),
    confirm_password: str = Form(...),
):
    user = require_login(request)
    if not user:
        return RedirectResponse("/", status_code=HTTP_303_SEE_OTHER)
    if not is_admin(request):
        return RedirectResponse("/inventario", status_code=HTTP_303_SEE_OTHER)

    if new_password != confirm_password:
        return _admin_users_render_error(request, user, "Le password non coincidono.")

    salt, h = make_password(new_password)
    ph = _ph()

    with connect() as conn:
        cur = conn.cursor()
        target = cur.execute(
            f"SELECT id, store, username, role FROM users WHERE id={ph}",
            (int(user_id),),
        ).fetchone()
        if not target:
            return _admin_users_render_error(request, user, "Utente non trovato.")

        cur.execute(
            f"UPDATE users SET pw_salt={ph}, pw_hash={ph}, legacy_sha256=NULL WHERE id={ph}",
            (salt, h, int(user_id)),
        )
        users = cur.execute("SELECT id, store, username, role, last_seen FROM users ORDER BY role DESC, store, username").fetchall()

    return _render_admin(
        request,
        user=user,
        users=users,
        msg=f"Password aggiornata per '{target['username']}' ({STORES.get(target['store'], target['store'])}).",
    )

@app.post("/admin/users/{user_id}/delete", response_class=HTMLResponse)
def admin_delete_user(request: Request, user_id: int):
    user = require_login(request)
    if not user:
        return RedirectResponse("/", status_code=HTTP_303_SEE_OTHER)
    if not is_admin(request):
        return RedirectResponse("/inventario", status_code=HTTP_303_SEE_OTHER)

    msg = None
    ph = _ph()
    with connect() as conn:
        cur = conn.cursor()
        target = cur.execute(
            f"SELECT id, store, username, role FROM users WHERE id={ph}",
            (int(user_id),),
        ).fetchone()
        if target and target["role"] != "admin":
            cur.execute(f"DELETE FROM users WHERE id={ph}", (int(user_id),))
            msg = f"Utente '{target['username']}' eliminato."
        users = cur.execute("SELECT id, store, username, role, last_seen FROM users ORDER BY role DESC, store, username").fetchall()

    return _render_admin(request, user=user, users=users, msg=msg)


@app.get("/logout")
def logout(request: Request):
    request.session.clear()
    return RedirectResponse("/", status_code=HTTP_303_SEE_OTHER)

@app.post("/set-active-store")
def set_active_store(request: Request, store: str = Form(...), next_url: str = Form("/inventario")):
    user = require_login(request)
    if not user:
        return RedirectResponse("/", status_code=HTTP_303_SEE_OTHER)
    if not is_admin(request):
        return RedirectResponse("/inventario", status_code=HTTP_303_SEE_OTHER)

    store = (store or "").strip()
    if store != "ALL" and store not in STORES:
        store = "spinza"
    request.session["active_store"] = store

    # sicurezza: evita redirect esterni
    if not next_url or not next_url.startswith("/"):
        next_url = "/inventario"
    return RedirectResponse(next_url, status_code=HTTP_303_SEE_OTHER)



# =========================
# AREA SELECTION (BIBITE / PRODOTTI)
# =========================
AREAS = {
    "bibite": "Bibite (Sala)",
    "prodotti": "Prodotti (Cucina)",
}

@app.get("/select-area", response_class=HTMLResponse)
def select_area_get(request: Request, area: str = ""):
    user = require_login(request)
    if not user:
        return RedirectResponse("/", status_code=HTTP_303_SEE_OTHER)

    area = (area or "").strip()
    if area in AREAS:
        set_selected_area(request, area)
        return RedirectResponse("/inventario", status_code=HTTP_303_SEE_OTHER)

    # brand: store selezionato (admin usa active_store se presente)
    brand = (request.session.get("active_store") if is_admin(request) else user.get("store")) or "spinza"
    if brand not in STORES:
        brand = "spinza"
    return render("select_area.html", user=user, areas=AREAS, brand=brand)


# =========================
# PROFILE (admin can change own username/password)
# =========================
@app.get("/profile", response_class=HTMLResponse)
def profile_get(request: Request):
    user = require_login(request)
    if not user:
        return RedirectResponse("/", status_code=HTTP_303_SEE_OTHER)

    brand = (request.session.get("active_store") if is_admin(request) else user.get("store")) or "spinza"
    if brand not in STORES:
        brand = "spinza"

    return render("profile.html", user=user, msg=None, error=None, brand=brand)

@app.post("/profile/password", response_class=HTMLResponse)
def profile_change_password(
    request: Request,
    current_password: str = Form(...),
    new_password: str = Form(...),
    confirm_password: str = Form(...),
):
    user = require_login(request)
    if not user:
        return RedirectResponse("/", status_code=HTTP_303_SEE_OTHER)

    brand = (request.session.get("active_store") if is_admin(request) else user.get("store")) or "spinza"
    if brand not in STORES:
        brand = "spinza"

    if new_password != confirm_password:
        return render("profile.html", user=user, msg=None, error="Le password non coincidono.", brand=brand)
    if len(new_password) < 4:
        return render("profile.html", user=user, msg=None, error="Password troppo corta.", brand=brand)

    ph = _ph()
    with connect() as conn:
        cur = conn.cursor()
        if user.get("role") == "admin" and user.get("id"):
            row = cur.execute(f"SELECT * FROM users WHERE id={ph}", (int(user["id"]),)).fetchone()
        else:
            row = cur.execute(
                f"SELECT * FROM users WHERE username={ph} AND store={ph}",
                (user["username"], user.get("store")),
            ).fetchone()

        if not row:
            request.session.clear()
            return RedirectResponse("/", status_code=HTTP_303_SEE_OTHER)

        ok = False
        if row.get("pw_salt") and row.get("pw_hash"):
            ok = verify_password(current_password, row["pw_salt"], row["pw_hash"])
        elif row.get("legacy_sha256"):
            ok = (legacy_sha256(current_password) == row["legacy_sha256"])

        if not ok:
            return render("profile.html", user=user, msg=None, error="Password attuale errata.", brand=brand)

        salt, h = make_password(new_password)
        cur.execute(
            f"UPDATE users SET pw_salt={ph}, pw_hash={ph}, legacy_sha256=NULL WHERE id={ph}",
            (salt, h, int(row["id"])),
        )

    return render("profile.html", user=user, msg="Password aggiornata.", error=None, brand=brand)

@app.post("/profile/username", response_class=HTMLResponse)
def profile_change_username(request: Request, new_username: str = Form(...)):
    user = require_login(request)
    if not user:
        return RedirectResponse("/", status_code=HTTP_303_SEE_OTHER)
    if not is_admin(request):
        return RedirectResponse("/profile", status_code=HTTP_303_SEE_OTHER)

    brand = (request.session.get("active_store") if is_admin(request) else user.get("store")) or "spinza"
    if brand not in STORES:
        brand = "spinza"

    new_username = (new_username or "").strip()
    if not new_username:
        return render("profile.html", user=user, msg=None, error="Username non valido.", brand=brand)

    ph = _ph()
    with connect() as conn:
        cur = conn.cursor()
        exists = cur.execute(
            f"SELECT 1 FROM users WHERE role='admin' AND username={ph} AND id<>{ph}",
            (new_username, int(user.get("id", -1))),
        ).fetchone()
        if exists:
            return render("profile.html", user=user, msg=None, error="Username admin già esistente.", brand=brand)

        cur.execute(
            f"UPDATE users SET username={ph} WHERE id={ph}",
            (new_username, int(user.get("id"))),
        )

    request.session["user"]["username"] = new_username
    return render("profile.html", user=request.session["user"], msg="Username aggiornato.", error=None, brand=brand)



# =========================
# NOVITÀ E AGGIORNAMENTI
# =========================
@app.get("/novita")
def novita_alias():
    """Alias per comodità: mantiene compatibilità con chi cerca /novita."""
    return RedirectResponse("/updates", status_code=HTTP_303_SEE_OTHER)


@app.get("/updates", response_class=HTMLResponse)
def updates_page(request: Request):
    user = require_login(request)
    if not user:
        return RedirectResponse("/", status_code=HTTP_303_SEE_OTHER)

    ph = _ph()
    with connect() as conn:
        cur = conn.cursor()
        rows = cur.execute(
            "SELECT id, day, message, created_by, ts FROM updates ORDER BY day DESC, ts ASC, id ASC"
        ).fetchall()

    # Normalizza righe tra Postgres (dict_row) e SQLite (sqlite3.Row)
    # In SQLite le righe non hanno `.get()`, quindi convertiamo tutto a dict.
    rows = [dict(r) for r in rows]

    grouped = []
    by_day = {}
    for r in rows:
        day = str(r.get("day") or "")[:10]
        if day not in by_day:
            by_day[day] = {"day": day, "items": []}
            grouped.append(by_day[day])
        by_day[day]["items"].append(r)

    active_store = request.session.get("active_store") if is_admin(request) else None
    brand = (active_store or user.get("store") or "spinza") if is_admin(request) else (user.get("store") or "spinza")
    return render("updates.html", user=user, grouped=grouped, brand=brand, active_store=active_store)


@app.post("/updates/post")
async def updates_post(request: Request):
    user = require_login(request)
    if not user:
        return RedirectResponse("/", status_code=HTTP_303_SEE_OTHER)
    if not is_admin(request):
        return RedirectResponse("/updates", status_code=HTTP_303_SEE_OTHER)

    form = await request.form()
    msg = (form.get("message") or "").strip()
    if not msg:
        return RedirectResponse("/updates", status_code=HTTP_303_SEE_OTHER)

    day = _today_str()
    now = _now()
    ph = _ph()
    with connect() as conn:
        cur = conn.cursor()
        cur.execute(
            f"INSERT INTO updates(day, message, created_by, ts) VALUES({ph},{ph},{ph},{now})",
            (day, msg, user["username"]),
        )

        # log (solo per traccia admin, store = active_store o store utente)
        st = request.session.get("active_store") if is_admin(request) and request.session.get("active_store") else (user.get("store") or "spinza")
        _log(
            cur,
            store=st,
            username=user["username"],
            action="UPDATE_POST",
            category="NOVITA",
            name=msg[:120],
            delta=0.0,
        )

    return RedirectResponse("/updates", status_code=HTTP_303_SEE_OTHER)


@app.post("/updates/delete/{update_id}")
async def updates_delete(request: Request, update_id: int):
    """Elimina un messaggio dalla bacheca Novità (solo admin)."""
    user = require_login(request)
    if not user:
        return RedirectResponse("/", status_code=HTTP_303_SEE_OTHER)
    if not is_admin(request):
        return RedirectResponse("/updates", status_code=HTTP_303_SEE_OTHER)

    ph = _ph()
    with connect() as conn:
        cur = conn.cursor()

        # Recupera il messaggio prima di eliminarlo (per log)
        row = cur.execute(
            f"SELECT message FROM updates WHERE id={ph}",
            (int(update_id),),
        ).fetchone()
        msg_preview = (dict(row).get("message") if row is not None else "")
        if msg_preview is None:
            msg_preview = ""

        cur.execute(
            f"DELETE FROM updates WHERE id={ph}",
            (int(update_id),),
        )

        st = request.session.get("active_store") if request.session.get("active_store") else (user.get("store") or "spinza")
        _log(
            cur,
            store=st,
            username=user["username"],
            action="UPDATE_DELETE",
            category="NOVITA",
            name=str(msg_preview)[:120],
            delta=0.0,
        )

    return RedirectResponse("/updates", status_code=HTTP_303_SEE_OTHER)

# =========================
# INVENTORY
# =========================
@app.get("/inventario", response_class=HTMLResponse)
def inventario(request: Request, q: str = "", cat: str = "ALL", loc: str = "ALL", only_low: int = 0):
    user = require_login(request)
    if not user:
        return RedirectResponse("/", status_code=HTTP_303_SEE_OTHER)

    area = get_selected_area(request)
    # UX: se non è stata ancora scelta la sezione, imposta un default.
    # Questo evita che l'admin (o un utente con sessione nuova) resti bloccato su redirect continui.
    if not area or area not in AREAS:
        area = "prodotti"
        set_selected_area(request, area)

    admin = is_admin(request)
    if admin:
        active_store = (request.query_params.get("store") or request.session.get("active_store") or "spinza").strip()
        if active_store not in STORES and active_store != "ALL":
            active_store = "spinza"
        request.session["active_store"] = active_store
    else:
        active_store = user.get("store") or get_selected_store(request) or "spinza"
        set_selected_store(request, active_store)

    q = (q or "").strip().lower()
    cat = (cat or "ALL").strip()
    loc = (loc or "ALL").strip()
    ph = _ph()

    with connect() as conn:
        cur = conn.cursor()

        # --- categorie ---
        cats_sql = "SELECT DISTINCT category FROM products WHERE area=" + ph
        cats_params = [area]
        if active_store != "ALL":
            cats_sql += f" AND store={ph}"
            cats_params.append(active_store)
        cats_sql += " ORDER BY category"
        cats = [r["category"] for r in cur.execute(cats_sql, tuple(cats_params)).fetchall()]

        # --- posizioni (per filtro) ---
        locations_sql = "SELECT DISTINCT location FROM products WHERE area=" + ph
        loc_params = [area]
        if active_store != "ALL":
            locations_sql += f" AND store={ph}"
            loc_params.append(active_store)
        locations_sql += " AND location IS NOT NULL AND TRIM(location)<>'' ORDER BY location"
        try:
            locations = [r["location"] for r in cur.execute(locations_sql, tuple(loc_params)).fetchall()]
        except Exception:
            locations = []

        # --- Selezione "chiavi prodotto" in base ai filtri, poi fetch di TUTTE le posizioni per quelle chiavi ---
        key_sql = "SELECT DISTINCT store, area, category, name FROM products WHERE 1=1"
        key_params = []
        key_sql += f" AND area={ph}"
        key_params.append(area)

        if active_store != "ALL":
            key_sql += f" AND store={ph}"
            key_params.append(active_store)

        if cat != "ALL":
            key_sql += f" AND category={ph}"
            key_params.append(cat)

        if loc != "ALL":
            # filtro per posizione: seleziona i prodotti che esistono in quella posizione,
            # ma poi mostriamo tutte le altre posizioni del prodotto.
            key_sql += f" AND location={ph}"
            key_params.append(loc)

        if q:
            key_sql += f" AND (lower(name) LIKE {ph} OR lower(category) LIKE {ph} OR lower(location) LIKE {ph})"
            key_params.extend([f"%{q}%", f"%{q}%", f"%{q}%"])

        if only_low:
            key_sql += " AND qty <= min_qty"

        key_sql += " ORDER BY category, name"

        keys = cur.execute(key_sql, tuple(key_params)).fetchall()

        if not keys:
            rows = []
        else:
            # costruisci WHERE (store,area,category,name) IN (...)
            # compatibile anche con SQLite (senza tuple IN): usa OR.
            clauses = []
            params = []
            for k in keys:
                clauses.append(f"(store={ph} AND area={ph} AND category={ph} AND name={ph})")
                params.extend([k["store"], k["area"], k["category"], k["name"]])
            rows = cur.execute(
                "SELECT * FROM products WHERE " + " OR ".join(clauses) + " ORDER BY category, name, location",
                tuple(params),
            ).fetchall()

    # --- Raggruppa per prodotto (stesso store+area+category+name), con posizioni affiancate e totale ---
    def _gkey(r):
        return (r["store"], r.get("area") or area, r["category"], r["name"])

    grouped = {}
    for r in rows:
        k = _gkey(r)
        grouped.setdefault(k, []).append(r)

    groups = []
    for (store_k, area_k, cat_k, name_k), lst in grouped.items():
        # ordina posizioni
        lst_sorted = sorted(lst, key=lambda x: (str(x.get("location") or "").lower(), int(x.get("id") or 0)))
        unit = ""
        category_color = ""
        # prendi unit non vuota se esiste
        for rr in lst_sorted:
            if (rr.get("unit") or "").strip():
                unit = (rr.get("unit") or "").strip()
                break
        for rr in lst_sorted:
            if (rr.get("category_color") or "").strip():
                category_color = _clean_category_color(rr.get("category_color"))
                break
        if not category_color:
            category_color = _default_category_color(cat_k)
        positions = []
        total = 0.0
        min_total = 0.0
        missing_total = 0.0
        any_low = False
        for rr in lst_sorted:
            qty = float(rr.get("qty") or 0)
            mn = float(rr.get("min_qty") or 0)
            missing = float(rr.get("missing_qty") or 0)
            low = qty <= mn
            total += qty
            min_total += mn
            missing_total += missing
            any_low = any_low or low
            positions.append({
                "id": rr.get("id"),
                "store": rr.get("store"),
                "category": rr.get("category"),
                "name": rr.get("name"),
                "category_color": _clean_category_color(rr.get("category_color") or category_color),
                "area": rr.get("area") or area_k,
                "location": (rr.get("location") or "").strip() or "MAGAZZINO",
                "unit": unit,
                "qty": qty,
                "min_qty": mn,
                "low": low,
                "missing_qty": missing,
                "missing_order_date": rr.get("missing_order_date"),
                "missing_delivery_date": rr.get("missing_delivery_date"),
            })
        # Interfaccia nuova: una sola riga per prodotto, senza più gestione posizioni.
        # Se nel DB esistono vecchie posizioni multiple, qui vengono comunque mostrate come totale unico.
        groups.append({
            "store": store_k,
            "area": area_k,
            "category": cat_k,
            "name": name_k,
            "category_color": category_color,
            "unit": unit,
            "positions": positions,
            "primary": positions[0] if positions else None,
            "total": total,
            "min_total": min_total,
            "missing_total": missing_total,
            "any_low": any_low,
            "disp": _compute_display_totals(total, unit),
        })

    # mantieni ordine categoria/nome
    groups.sort(key=lambda g: (str(g["category"]).lower(), str(g["name"]).lower()))

    return render(
        "inventario.html",
        user=user,
        area=area,
        areas=AREAS,
        groups=groups,
        cats=cats,
        locations=locations,
        q=q,
        cat=cat,
        loc=loc,
        only_low=only_low,
        admin=admin,
        stores=STORES,
        active_store=active_store,
        # can_edit: permette Set / +/- anche se l'admin sta guardando "ALL" (passiamo lo store del prodotto nel form)
        can_edit=True,
        # can_bulk_edit: azioni che richiedono un inventario singolo (add/import/export)
        can_bulk_edit=(active_store != "ALL"),
        brand=active_store if active_store != "ALL" else "spinza",
    )


def _collapse_product_rows_for_single_position(cur, *, row, effective_store, new_qty=None,
                                               new_category=None, new_name=None,
                                               new_unit=None, new_min_qty=None, new_category_color=None):
    """Compatta eventuali vecchie posizioni multiple in una sola riga prodotto.

    La nuova UI non usa più posizioni: ogni prodotto resta unico con location MAGAZZINO.
    Se esistono righe vecchie tipo SALA/MAGAZZINO, il totale viene preservato e le righe extra rimosse.
    """
    ph = _ph()
    now = _now()
    area = (row.get("area") or "prodotti")
    old_category = (row.get("category") or "").strip()
    old_name = (row.get("name") or "").strip()
    base_id = int(row.get("id"))

    siblings = cur.execute(
        f"SELECT * FROM products WHERE store={ph} AND area={ph} AND category={ph} AND name={ph} ORDER BY id",
        (effective_store, area, old_category, old_name),
    ).fetchall()

    total_qty = sum(float(r.get("qty") or 0) for r in siblings) if siblings else float(row.get("qty") or 0)
    total_min = sum(float(r.get("min_qty") or 0) for r in siblings) if siblings else float(row.get("min_qty") or 0)
    total_missing = sum(float(r.get("missing_qty") or 0) for r in siblings) if siblings else float(row.get("missing_qty") or 0)

    final_qty = float(total_qty if new_qty is None else new_qty)
    if final_qty < 0:
        final_qty = 0.0
    final_category = (new_category if new_category is not None else old_category).strip()
    final_name = (new_name if new_name is not None else old_name).strip()
    final_unit = (new_unit if new_unit is not None else (row.get("unit") or "")).strip()
    final_min = float(total_min if new_min_qty is None else new_min_qty)
    final_category_color = _clean_category_color(new_category_color if new_category_color is not None else (row.get("category_color") or _default_category_color(final_category)))

    # Prima elimina le altre posizioni: evita conflitti con l'indice unico su MAGAZZINO.
    cur.execute(
        f"DELETE FROM products WHERE store={ph} AND area={ph} AND category={ph} AND name={ph} AND id<>{ph}",
        (effective_store, area, old_category, old_name, base_id),
    )
    cur.execute(
        f"""UPDATE products
            SET category={ph}, name={ph}, location={ph}, unit={ph}, qty={ph}, min_qty={ph}, missing_qty={ph}, category_color={ph}, updated_at={now}
            WHERE id={ph} AND store={ph}""",
        (final_category, final_name, "MAGAZZINO", final_unit, final_qty, final_min, total_missing, final_category_color, base_id, effective_store),
    )
    try:
        cur.execute(
            f"UPDATE products SET category_color={ph} WHERE store={ph} AND area={ph} AND category={ph}",
            (final_category_color, effective_store, area, final_category),
        )
    except Exception:
        pass
    return {
        "qty": final_qty,
        "min_qty": final_min,
        "category": final_category,
        "name": final_name,
        "category_color": final_category_color,
        "unit": final_unit,
    }


@app.post("/items/{item_id}/delta")
def item_delta(request: Request, item_id: int, delta: float = Form(...), store: str = Form(""), next_url: str = Form("/inventario")):
    user = require_login(request)
    if not user:
        return RedirectResponse("/", status_code=HTTP_303_SEE_OTHER)

    admin = is_admin(request)
    active_store = (request.session.get("active_store") if admin else user.get("store"))
    effective_store = active_store
    # Se l'admin sta guardando "ALL", lo store arriva dal form del prodotto.
    if admin and active_store == "ALL":
        store = (store or "").strip()
        if store in STORES:
            effective_store = store
    if not effective_store or effective_store == "ALL":
        return RedirectResponse("/inventario", status_code=HTTP_303_SEE_OTHER)

    ph = _ph()
    now = _now()

    area = get_selected_area(request) or "prodotti"
    if area not in AREAS:
        area = "prodotti"
    area = get_selected_area(request) or "prodotti"
    if area not in AREAS:
        area = "prodotti"

    with connect() as conn:
        cur = conn.cursor()
        row = cur.execute(
            f"SELECT * FROM products WHERE id={ph} AND store={ph}",
            (int(item_id), effective_store),
        ).fetchone()
        if not row:
            return RedirectResponse("/inventario", status_code=HTTP_303_SEE_OTHER)

        # La UI ora è a posizione unica: se trova vecchie righe SALA/MAGAZZINO,
        # usa il totale complessivo e compatta tutto in una sola riga.
        area_row = (row.get("area") or "prodotti")
        siblings = cur.execute(
            f"SELECT qty FROM products WHERE store={ph} AND area={ph} AND category={ph} AND name={ph}",
            (effective_store, area_row, row["category"], row["name"]),
        ).fetchall()
        current_total = sum(float(r.get("qty") or 0) for r in siblings) if siblings else float(row["qty"] or 0)
        new_qty = current_total + float(delta)
        info = _collapse_product_rows_for_single_position(cur, row=row, effective_store=effective_store, new_qty=new_qty)
        cur.execute(
            f"INSERT INTO logs(ts, store, username, action, category, name, delta) VALUES({now},{ph},{ph},{ph},{ph},{ph},{ph})",
            (effective_store, user["username"], "DELTA", info["category"], info["name"], float(delta)),
        )

    return RedirectResponse(_safe_next_url(next_url, "/inventario"), status_code=HTTP_303_SEE_OTHER)

@app.post("/items/{item_id}/set")
def item_set(request: Request, item_id: int, qty: float = Form(...), store: str = Form(""), next_url: str = Form("/inventario")):
    user = require_login(request)
    if not user:
        return RedirectResponse("/", status_code=HTTP_303_SEE_OTHER)

    admin = is_admin(request)
    active_store = (request.session.get("active_store") if admin else user.get("store"))
    effective_store = active_store
    if admin and active_store == "ALL":
        store = (store or "").strip()
        if store in STORES:
            effective_store = store
    if not effective_store or effective_store == "ALL":
        return RedirectResponse("/inventario", status_code=HTTP_303_SEE_OTHER)

    ph = _ph()
    now = _now()

    with connect() as conn:
        cur = conn.cursor()
        row = cur.execute(
            f"SELECT * FROM products WHERE id={ph} AND store={ph}",
            (int(item_id), effective_store),
        ).fetchone()
        if not row:
            return RedirectResponse("/inventario", status_code=HTTP_303_SEE_OTHER)

        info = _collapse_product_rows_for_single_position(cur, row=row, effective_store=effective_store, new_qty=float(qty))
        cur.execute(
            f"INSERT INTO logs(ts, store, username, action, category, name, delta) VALUES({now},{ph},{ph},{ph},{ph},{ph},{ph})",
            (effective_store, user["username"], "SET", info["category"], info["name"], float(qty)),
        )

    return RedirectResponse(_safe_next_url(next_url, "/inventario"), status_code=HTTP_303_SEE_OTHER)


@app.post("/items/transfer")
def item_transfer(
    request: Request,
    category: str = Form(...),
    name: str = Form(...),
    from_location: str = Form(...),
    to_location: str = Form(...),
    qty: float = Form(...),
    store: str = Form(""),
    next_url: str = Form("/inventario"),
):
    """Sposta quantità tra due posizioni dello stesso prodotto.

    Implementato in modo robusto senza duplicare 'prodotti' diversi:
    stessa (store, area, category, name), location diversa.
    """
    user = require_login(request)
    if not user:
        return RedirectResponse("/", status_code=HTTP_303_SEE_OTHER)

    admin = is_admin(request)
    active_store = (request.session.get("active_store") if admin else user.get("store"))
    effective_store = active_store
    if admin and active_store == "ALL":
        store = (store or "").strip()
        if store in STORES:
            effective_store = store
    if not effective_store or effective_store == "ALL":
        return RedirectResponse("/inventario", status_code=HTTP_303_SEE_OTHER)

    area = get_selected_area(request) or "prodotti"
    if area not in AREAS:
        area = "prodotti"

    category = (category or "").strip()
    name = (name or "").strip()
    from_location = (from_location or "").strip() or "MAGAZZINO"
    to_location = (to_location or "").strip() or "MAGAZZINO"

    try:
        qty = float(qty)
    except Exception:
        qty = 0.0
    if qty <= 0 or from_location == to_location:
        return RedirectResponse(_safe_next_url(next_url, "/inventario"), status_code=HTTP_303_SEE_OTHER)

    ph = _ph()
    now = _now()

    with connect() as conn:
        cur = conn.cursor()

        src = cur.execute(
            f"SELECT * FROM products WHERE store={ph} AND area={ph} AND category={ph} AND name={ph} AND location={ph}",
            (effective_store, area, category, name, from_location),
        ).fetchone()
        if not src:
            return RedirectResponse(_safe_next_url(next_url, "/inventario"), status_code=HTTP_303_SEE_OTHER)

        src_qty = float(src.get("qty") or 0)
        move_qty = min(src_qty, qty)
        if move_qty <= 0:
            return RedirectResponse(_safe_next_url(next_url, "/inventario"), status_code=HTTP_303_SEE_OTHER)

        dst = cur.execute(
            f"SELECT * FROM products WHERE store={ph} AND area={ph} AND category={ph} AND name={ph} AND location={ph}",
            (effective_store, area, category, name, to_location),
        ).fetchone()

        # decrementa sorgente
        cur.execute(
            f"UPDATE products SET qty={ph}, updated_at={now} WHERE id={ph}",
            (float(src_qty - move_qty), int(src.get("id"))),
        )

        if dst:
            dst_qty = float(dst.get("qty") or 0)
            cur.execute(
                f"UPDATE products SET qty={ph}, updated_at={now} WHERE id={ph}",
                (float(dst_qty + move_qty), int(dst.get("id"))),
            )
        else:
            # crea la riga per la nuova posizione (minimo a 0 di default; modificabile dall'edit)
            unit = (src.get("unit") or "").strip()
            cur.execute(
                f"""INSERT INTO products(store, category, name, area, location, unit, qty, min_qty, updated_at)
                    VALUES({ph},{ph},{ph},{ph},{ph},{ph},{ph},{ph},{now})
                    ON CONFLICT(store, area, category, name, location)
                    DO UPDATE SET qty=excluded.qty, updated_at={now}""",
                (effective_store, category, name, area, to_location, unit, float(move_qty), 0.0),
            )

        # log (chiaro e auditabile)
        _log(
            cur,
            store=effective_store,
            username=user["username"],
            action="MOVE",
            category=category,
            name=f"{name} | {from_location} → {to_location}",
            delta=float(move_qty),
        )

    return RedirectResponse(_safe_next_url(next_url, "/inventario"), status_code=HTTP_303_SEE_OTHER)

@app.post("/items/add")
def item_add(
    request: Request,
    category: str = Form(...),
    name: str = Form(...),
    location: str = Form("MAGAZZINO"),
    unit: str = Form(""),
    category_color: str = Form(""),
    qty: float = Form(0),
    min_qty: float = Form(0),
    next_url: str = Form("/inventario"),
):
    user = require_login(request)
    if not user:
        return RedirectResponse("/", status_code=HTTP_303_SEE_OTHER)

    admin = is_admin(request)
    active_store = (request.session.get("active_store") if admin else user.get("store"))
    if not active_store or active_store == "ALL":
        return RedirectResponse("/inventario", status_code=HTTP_303_SEE_OTHER)

    category = category.strip()
    name = name.strip()

    ph = _ph()
    now = _now()

    # area corrente (bibite / prodotti) => salva correttamente e evita 500
    area = get_selected_area(request) or "prodotti"
    if area not in AREAS:
        area = "prodotti"

    location = (location or "MAGAZZINO").strip()

    unit = (unit or "").strip()
    category_color = _clean_category_color(category_color or _default_category_color(category))

    with connect() as conn:
        cur = conn.cursor()
        cur.execute(
            f"""INSERT INTO products(store, category, name, area, location, unit, qty, min_qty, category_color, updated_at)
                VALUES({ph},{ph},{ph},{ph},{ph},{ph},{ph},{ph},{ph},{now})
                ON CONFLICT(store, area, category, name, location)
                DO UPDATE SET unit=excluded.unit, qty=excluded.qty, min_qty=excluded.min_qty, category_color=excluded.category_color, updated_at={now}""",
            (active_store, category, name, area, location, unit, float(qty), float(min_qty), category_color),
        )
        try:
            cur.execute(
                f"UPDATE products SET category_color={ph} WHERE store={ph} AND area={ph} AND category={ph}",
                (category_color, active_store, area, category),
            )
        except Exception:
            pass
        cur.execute(
            f"INSERT INTO logs(ts, store, username, action, category, name, delta) VALUES({now},{ph},{ph},{ph},{ph},{ph},{ph})",
            (active_store, user["username"], "ADD", category, name, float(qty)),
        )

    return RedirectResponse(_safe_next_url(next_url, "/inventario"), status_code=HTTP_303_SEE_OTHER)

@app.post("/items/{item_id}/edit")
def item_edit(
    request: Request,
    item_id: int,
    store: str = Form(""),
    category: str = Form(...),
    name: str = Form(...),
    location: str = Form("MAGAZZINO"),
    unit: str = Form(""),
    category_color: str = Form(""),
    min_qty: float = Form(0),
    next_url: str = Form("/inventario"),
):
    user = require_login(request)
    if not user:
        return RedirectResponse("/", status_code=HTTP_303_SEE_OTHER)

    admin = is_admin(request)
    active_store = (request.session.get("active_store") if admin else user.get("store"))
    effective_store = active_store
    if admin and active_store == "ALL":
        store = (store or "").strip()
        if store in STORES:
            effective_store = store
    if not effective_store or effective_store == "ALL":
        return RedirectResponse("/inventario", status_code=HTTP_303_SEE_OTHER)

    category = category.strip()
    name = name.strip()
    location = (location or "MAGAZZINO").strip()

    unit = (unit or "").strip()
    category_color = _clean_category_color(category_color or _default_category_color(category))

    ph = _ph()
    now = _now()

    with connect() as conn:
        cur = conn.cursor()
        row = cur.execute(
            f"SELECT * FROM products WHERE id={ph} AND store={ph}",
            (int(item_id), effective_store),
        ).fetchone()
        if not row:
            return RedirectResponse("/inventario", status_code=HTTP_303_SEE_OTHER)

        info = _collapse_product_rows_for_single_position(
            cur,
            row=row,
            effective_store=effective_store,
            new_category=category,
            new_name=name,
            new_unit=unit,
            new_min_qty=float(min_qty),
            new_category_color=category_color,
        )
        cur.execute(
            f"INSERT INTO logs(ts, store, username, action, category, name, delta) VALUES({now},{ph},{ph},{ph},{ph},{ph},{ph})",
            (effective_store, user["username"], "EDIT", info["category"], info["name"], float(min_qty)),
        )

    return RedirectResponse(_safe_next_url(next_url, "/inventario"), status_code=HTTP_303_SEE_OTHER)

@app.post("/items/{item_id}/delete")
def item_delete(request: Request, item_id: int, store: str = Form(""), next_url: str = Form("/inventario")):
    user = require_login(request)
    if not user:
        return RedirectResponse("/", status_code=HTTP_303_SEE_OTHER)

    admin = is_admin(request)
    active_store = (request.session.get("active_store") if admin else user.get("store"))
    effective_store = active_store
    if admin and active_store == "ALL":
        store = (store or "").strip()
        if store in STORES:
            effective_store = store
    if not effective_store or effective_store == "ALL":
        return RedirectResponse("/inventario", status_code=HTTP_303_SEE_OTHER)

    ph = _ph()
    now = _now()

    with connect() as conn:
        cur = conn.cursor()
        row = cur.execute(
            f"SELECT * FROM products WHERE id={ph} AND store={ph}",
            (int(item_id), effective_store),
        ).fetchone()
        if row:
            area_row = (row.get("area") or "prodotti")
            cur.execute(
                f"DELETE FROM products WHERE store={ph} AND area={ph} AND category={ph} AND name={ph}",
                (effective_store, area_row, row["category"], row["name"]),
            )
            cur.execute(
                f"INSERT INTO logs(ts, store, username, action, category, name, delta) VALUES({now},{ph},{ph},{ph},{ph},{ph},{ph})",
                (effective_store, user["username"], "DELETE", row["category"], row["name"], 0.0),
            )

    return RedirectResponse(_safe_next_url(next_url, "/inventario"), status_code=HTTP_303_SEE_OTHER)


# =========================
# CSV EXPORT / IMPORT
# =========================
@app.get("/export.csv")
def export_csv(request: Request):
    user = require_login(request)
    if not user:
        return RedirectResponse("/", status_code=HTTP_303_SEE_OTHER)

    admin = is_admin(request)
    active_store = (request.session.get("active_store") if admin else user.get("store"))
    if not active_store or active_store == "ALL":
        return RedirectResponse("/inventario", status_code=HTTP_303_SEE_OTHER)

    ph = _ph()
    with connect() as conn:
        cur = conn.cursor()
        rows = cur.execute(
            f"SELECT area, category, name, location, unit, qty, min_qty FROM products WHERE store={ph} ORDER BY area, category, name, location",
            (active_store,),
        ).fetchall()

    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["area", "category", "name", "location", "unit", "qty", "min_qty"])
    for r in rows:
        w.writerow([r["area"], r["category"], r["name"], r["location"], r.get("unit") or "", r["qty"], r["min_qty"]])

    return PlainTextResponse(
        buf.getvalue(),
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": f"attachment; filename=inventory_{active_store}.csv"},
    )

@app.post("/import.csv")
async def import_csv(request: Request, file: UploadFile = File(...)):
    user = require_login(request)
    if not user:
        return RedirectResponse("/", status_code=HTTP_303_SEE_OTHER)

    admin = is_admin(request)
    active_store = (request.session.get("active_store") if admin else user.get("store"))
    if not active_store or active_store == "ALL":
        return RedirectResponse("/inventario", status_code=HTTP_303_SEE_OTHER)

    content = (await file.read()).decode("utf-8", errors="ignore")
    buf = io.StringIO(content)
    reader = csv.DictReader(buf)

    ph = _ph()
    now = _now()
    count = 0
    area = get_selected_area(request) or "prodotti"
    if area not in AREAS:
        area = "prodotti"

    with connect() as conn:
        cur = conn.cursor()
        for row in reader:
            cat = (row.get("category") or row.get("Categoria") or "").strip()
            name = (row.get("name") or row.get("Prodotto") or "").strip()
            if not cat or not name:
                continue
            try:
                qty = float(row.get("qty") or row.get("Q.tà") or 0)
            except Exception:
                qty = 0.0
            try:
                min_qty = float(row.get("min_qty") or row.get("Minimo") or 0)
            except Exception:
                min_qty = 0.0

            cur.execute(
                f"""INSERT INTO products(store, category, name, area, location, qty, min_qty, updated_at)
                    VALUES({ph},{ph},{ph},{ph},{ph},{ph},{ph},{now})
                    ON CONFLICT(store, area, category, name, location)
                    DO UPDATE SET qty=excluded.qty, min_qty=excluded.min_qty, updated_at={now}""",
                (active_store, cat, name, area, "MAGAZZINO", qty, min_qty),
            )
            count += 1

        cur.execute(
            f"INSERT INTO logs(ts, store, username, action, category, name, delta) VALUES({now},{ph},{ph},{ph},{ph},{ph},{ph})",
            (active_store, user["username"], "IMPORT", "CSV", file.filename or "upload", float(count)),
        )

    return RedirectResponse("/inventario", status_code=HTTP_303_SEE_OTHER)


def _normalize_active_store_for_admin(request: Request) -> str:
    """Ritorna lo store attivo per l'admin (può essere anche ALL)."""
    active_store = (request.session.get("active_store") or "spinza").strip()
    if active_store != "ALL" and active_store not in STORES:
        active_store = "spinza"
        request.session["active_store"] = active_store
    return active_store


def _logs_sql_date_filter(using_pg: bool, field: str = "ts"):
    """Ritorna la condizione SQL e la funzione di conversione per filtrare per giorno (YYYY-MM-DD)."""
    if using_pg:
        return f"DATE({field}) = {_ph()}"
    # sqlite: ts è una stringa 'YYYY-MM-DD HH:MM:SS'
    return f"substr({field}, 1, 10) = {_ph()}"


def _fetch_logs(
    *,
    store: str,
    limit: int,
    section: str,
    q_user: str = "",
    q_day: str = "",
):
    """Legge i log filtrando per sezione, utente e giorno.

    section: 'inventory' | 'orders' | 'docs'
    q_day: 'YYYY-MM-DD'
    """
    ph = _ph()
    using_pg = using_postgres()

    # condizioni sezione
    order_cond = "(UPPER(action) LIKE 'ORDER_%%' OR UPPER(category) IN ('ORDINI','ORDER'))"
    doc_cond = "(UPPER(action) LIKE 'DOC_%%' OR UPPER(category) IN ('CHIUSURE','FATTURE','SPESE'))"
    if section == "orders":
        section_cond = order_cond
    elif section == "docs":
        section_cond = doc_cond
    else:
        section_cond = f"(NOT {order_cond} AND NOT {doc_cond})"

    where = [section_cond]
    params = []

    if store != "ALL":
        where.append(f"store={ph}")
        params.append(store)

    q_user = (q_user or "").strip()
    if q_user:
        # ricerca parziale sullo username
        if using_pg:
            where.append(f"username ILIKE {ph}")
            params.append(f"%{q_user}%")
        else:
            where.append(f"UPPER(username) LIKE {ph}")
            params.append(f"%{q_user.upper()}%")

    q_day = (q_day or "").strip()
    if q_day:
        where.append(_logs_sql_date_filter(using_pg, "ts"))
        params.append(q_day)

    where_sql = " AND ".join(where) if where else "1=1"

    # In alcuni ambienti (soprattutto Postgres) il parametro bindato dentro LIMIT
    # può generare errori (500). Usiamo quindi un LIMIT inserito come intero già
    # validato per evitare Internal Server Error.
    safe_limit = int(limit)

    with connect() as conn:
        cur = conn.cursor()
        rows = cur.execute(
            f"SELECT * FROM logs WHERE {where_sql} ORDER BY id DESC LIMIT {safe_limit}",
            tuple(params),
        ).fetchall()
    return rows


# =========================
# LOGS (admin only) - 3 pagine + ricerca
# =========================
@app.get("/logs", response_class=HTMLResponse)
def logs_home(request: Request):
    user = require_login(request)
    if not user:
        return RedirectResponse("/", status_code=HTTP_303_SEE_OTHER)
    if not is_admin(request):
        return RedirectResponse("/inventario", status_code=HTTP_303_SEE_OTHER)

    active_store = _normalize_active_store_for_admin(request)
    brand = "ALL" if active_store == "ALL" else active_store
    return render(
        "logs_home.html",
        user=user,
        stores=STORES,
        brand=brand,
        active_store=active_store,
    )


@app.get("/logs/inventario", response_class=HTMLResponse)
def logs_inventory_page(request: Request, limit: int = 500, q_user: str = "", q_day: str = ""):
    user = require_login(request)
    if not user:
        return RedirectResponse("/", status_code=HTTP_303_SEE_OTHER)
    if not is_admin(request):
        return RedirectResponse("/inventario", status_code=HTTP_303_SEE_OTHER)

    active_store = _normalize_active_store_for_admin(request)
    rows = _fetch_logs(store=active_store, limit=limit, section="inventory", q_user=q_user, q_day=q_day)
    brand = "ALL" if active_store == "ALL" else active_store
    return render(
        "logs_inventory.html",
        user=user,
        rows=rows,
        limit=limit,
        q_user=q_user,
        q_day=q_day,
        stores=STORES,
        brand=brand,
        active_store=active_store,
    )


@app.get("/logs/ordini", response_class=HTMLResponse)
def logs_orders_page(request: Request, limit: int = 500, q_user: str = "", q_day: str = ""):
    user = require_login(request)
    if not user:
        return RedirectResponse("/", status_code=HTTP_303_SEE_OTHER)
    if not is_admin(request):
        return RedirectResponse("/inventario", status_code=HTTP_303_SEE_OTHER)

    active_store = _normalize_active_store_for_admin(request)
    rows = _fetch_logs(store=active_store, limit=limit, section="orders", q_user=q_user, q_day=q_day)
    brand = "ALL" if active_store == "ALL" else active_store
    return render(
        "logs_orders.html",
        user=user,
        rows=rows,
        limit=limit,
        q_user=q_user,
        q_day=q_day,
        stores=STORES,
        brand=brand,
        active_store=active_store,
    )


@app.get("/logs/documenti", response_class=HTMLResponse)
def logs_docs_page(request: Request, limit: int = 500, q_user: str = "", q_day: str = ""):
    user = require_login(request)
    if not user:
        return RedirectResponse("/", status_code=HTTP_303_SEE_OTHER)
    if not is_admin(request):
        return RedirectResponse("/inventario", status_code=HTTP_303_SEE_OTHER)

    active_store = _normalize_active_store_for_admin(request)
    rows = _fetch_logs(store=active_store, limit=limit, section="docs", q_user=q_user, q_day=q_day)
    brand = "ALL" if active_store == "ALL" else active_store
    return render(
        "logs_docs.html",
        user=user,
        rows=rows,
        limit=limit,
        q_user=q_user,
        q_day=q_day,
        stores=STORES,
        brand=brand,
        active_store=active_store,
    )


# =========================
# CHIUSURE (foto) & FATTURE (documenti)
# =========================
def _effective_store(request: Request, user: dict) -> str:
    """Store usato per LEGGERE dati nelle pagine.

    - utenti normali: sempre il loro store
    - admin: lo store selezionato dal selettore (può essere anche 'ALL')
    """
    if is_admin(request):
        s = (request.session.get("active_store") or "spinza").strip()
        if s == "ALL":
            return "ALL"
        return s if s in STORES else "spinza"
    return user.get("store") or "spinza"


@app.get("/chiusure", response_class=HTMLResponse)
def closures_page(request: Request, q: str = ""):
    user = require_login(request)
    if not user:
        return RedirectResponse("/", status_code=HTTP_303_SEE_OTHER)

    store = _effective_store(request, user)
    brand = "ALL" if store == "ALL" else store

    q = (q or "").strip()
    ph = _ph()

    with connect() as conn:
        cur = conn.cursor()
        if store == "ALL":
            sql = "SELECT id, store, closure_date, uploaded_by, ts, filename, content_type FROM closures WHERE 1=1"
            params = []
        else:
            sql = f"SELECT id, store, closure_date, uploaded_by, ts, filename, content_type FROM closures WHERE store={ph}"
            params = [store]

        if q:
            sql += f" AND CAST(closure_date AS TEXT) LIKE {ph}"
            params.append(f"%{q}%")

        sql += " ORDER BY store, closure_date DESC, id DESC" if store == "ALL" else " ORDER BY closure_date DESC, id DESC"
        rows = cur.execute(sql, tuple(params)).fetchall()

    active_store = request.session.get("active_store") if is_admin(request) else None
    return render("closures.html", user=user, rows=rows, q=q, stores=STORES, brand=brand, active_store=active_store)


@app.post("/chiusure/upload")
async def closures_upload(request: Request, closure_date: str = Form(...), files: list[UploadFile] = File(...)):
    user = require_login(request)
    if not user:
        return RedirectResponse("/", status_code=HTTP_303_SEE_OTHER)

    store = _effective_store(request, user)

    pdfs: list[bytes] = []
    base_name = "chiusura"
    for f in files:
        raw = await f.read()
        filename_in = f.filename or base_name
        content_type_in = f.content_type or "application/octet-stream"
        pdf_bytes, _, _ = ensure_pdf(raw, filename_in, content_type_in)
        pdfs.append(pdf_bytes)

    # Unisco tutto in un unico PDF (multi-pagina / multi-file)
    if not pdfs:
        return RedirectResponse("/chiusure", status_code=HTTP_303_SEE_OTHER)

    content = merge_pdfs(pdfs) if len(pdfs) > 1 else pdfs[0]
    first_name = (files[0].filename if files and files[0].filename else base_name) or base_name
    filename = first_name if first_name.lower().endswith(".pdf") else f"{first_name}.pdf"
    content_type = "application/pdf"

    # parse data
    try:
        _ = date.fromisoformat(closure_date)
    except Exception:
        return RedirectResponse("/chiusure", status_code=HTTP_303_SEE_OTHER)

    ph = _ph()
    now = _now()
    with connect() as conn:
        cur = conn.cursor()
        cur.execute(
            f"INSERT INTO closures(store, closure_date, uploaded_by, ts, filename, content_type, data) VALUES({ph},{ph},{ph},{now},{ph},{ph},{ph})",
            (store, closure_date, user["username"], filename, content_type, content),
        )
        _log(
            cur,
            store=store,
            username=user["username"],
            action="DOC_UPLOAD",
            category="CHIUSURE",
            name=f"Chiusura {closure_date} ({filename})",
            delta=0.0,
        )

    return RedirectResponse("/chiusure", status_code=HTTP_303_SEE_OTHER)


@app.get("/chiusure/{doc_id}")
def closures_download(request: Request, doc_id: int):
    user = require_login(request)
    if not user:
        return RedirectResponse("/", status_code=HTTP_303_SEE_OTHER)

    # Solo admin può aprire/visualizzare il file
    if not is_admin(request):
        return PlainTextResponse("Solo admin", status_code=403)

    store = _effective_store(request, user)
    ph = _ph()

    with connect() as conn:
        cur = conn.cursor()
        if store == "ALL":
            row = cur.execute(
                f"SELECT filename, content_type, data FROM closures WHERE id={ph}",
                (int(doc_id),),
            ).fetchone()
        else:
            row = cur.execute(
                f"SELECT filename, content_type, data FROM closures WHERE id={ph} AND store={ph}",
                (int(doc_id), store),
            ).fetchone()

    if not row:
        return PlainTextResponse("Not found", status_code=404)

    from fastapi.responses import Response
    headers = {"Content-Disposition": f"inline; filename=\"{row.get('filename') or 'chiusura'}\""}
    return Response(content=row["data"], media_type=row.get("content_type") or "application/octet-stream", headers=headers)

@app.post("/chiusure/{doc_id}/delete")
def closures_delete(request: Request, doc_id: int):
    user = require_login(request)
    if not user:
        return RedirectResponse("/", status_code=HTTP_303_SEE_OTHER)

    store = _effective_store(request, user)
    ph = _ph()
    now = _now()

    with connect() as conn:
        cur = conn.cursor()

        if store == "ALL" and is_admin(request):
            info = cur.execute(
                f"SELECT store, closure_date, filename FROM closures WHERE id={ph}",
                (int(doc_id),),
            ).fetchone()
        else:
            info = cur.execute(
                f"SELECT store, closure_date, filename FROM closures WHERE id={ph} AND store={ph}",
                (int(doc_id), store),
            ).fetchone()

        if info:
            if store == "ALL" and is_admin(request):
                cur.execute(f"DELETE FROM closures WHERE id={ph}", (int(doc_id),))
                store_for_log = info["store"]
            else:
                cur.execute(f"DELETE FROM closures WHERE id={ph} AND store={ph}", (int(doc_id), store))
                store_for_log = store

            cur.execute(
                f"INSERT INTO logs(ts, store, username, action, category, name, delta) VALUES({now},{ph},{ph},{ph},{ph},{ph},{ph})",
                (store_for_log, user["username"], "DELETE", "CHIUSURE", str(info.get("closure_date") or ""), 0.0),
            )

    return RedirectResponse("/chiusure", status_code=HTTP_303_SEE_OTHER)





# =========================
# SPESE SECONDARIE (foto scontrini / spese urgenti)
# =========================
@app.get("/spese-secondarie", response_class=HTMLResponse)
def secondary_expenses_page(request: Request, q: str = ""):
    user = require_login(request)
    if not user:
        return RedirectResponse("/", status_code=HTTP_303_SEE_OTHER)

    store = _effective_store(request, user)
    brand = "ALL" if store == "ALL" else store

    q = (q or "").strip()
    ph = _ph()

    with connect() as conn:
        cur = conn.cursor()
        if store == "ALL":
            sql = "SELECT id, store, expense_date, uploaded_by, ts, filename, content_type FROM secondary_expenses WHERE 1=1"
            params = []
        else:
            sql = f"SELECT id, store, expense_date, uploaded_by, ts, filename, content_type FROM secondary_expenses WHERE store={ph}"
            params = [store]

        if q:
            sql += f" AND CAST(expense_date AS TEXT) LIKE {ph}"
            params.append(f"%{q}%")

        sql += " ORDER BY store, expense_date DESC, id DESC" if store == "ALL" else " ORDER BY expense_date DESC, id DESC"
        rows = cur.execute(sql, tuple(params)).fetchall()

    active_store = request.session.get("active_store") if is_admin(request) else None
    return render("secondary_expenses.html", user=user, rows=rows, q=q, stores=STORES, brand=brand, active_store=active_store)


@app.post("/spese-secondarie/upload")
async def secondary_expenses_upload(request: Request, expense_date: str = Form(...), files: list[UploadFile] = File(...)):
    user = require_login(request)
    if not user:
        return RedirectResponse("/", status_code=HTTP_303_SEE_OTHER)

    store = _effective_store(request, user)

    pdfs: list[bytes] = []
    base_name = "spesa"
    for f in files:
        raw = await f.read()
        filename_in = f.filename or base_name
        content_type_in = f.content_type or "application/octet-stream"
        pdf_bytes, _, _ = ensure_pdf(raw, filename_in, content_type_in)
        pdfs.append(pdf_bytes)

    if not pdfs:
        return RedirectResponse("/spese-secondarie", status_code=HTTP_303_SEE_OTHER)

    content = merge_pdfs(pdfs) if len(pdfs) > 1 else pdfs[0]
    first_name = (files[0].filename if files and files[0].filename else base_name) or base_name
    filename = first_name if first_name.lower().endswith(".pdf") else f"{first_name}.pdf"
    content_type = "application/pdf"

    try:
        _ = date.fromisoformat(expense_date)
    except Exception:
        return RedirectResponse("/spese-secondarie", status_code=HTTP_303_SEE_OTHER)

    ph = _ph()
    now = _now()
    with connect() as conn:
        cur = conn.cursor()
        cur.execute(
            f"INSERT INTO secondary_expenses(store, expense_date, uploaded_by, ts, filename, content_type, data) VALUES({ph},{ph},{ph},{now},{ph},{ph},{ph})",
            (store, expense_date, user["username"], filename, content_type, content),
        )
        _log(
            cur,
            store=store,
            username=user["username"],
            action="DOC_UPLOAD",
            category="SPESE",
            name=f"Spesa {expense_date} ({filename})",
            delta=0.0,
        )

    return RedirectResponse("/spese-secondarie", status_code=HTTP_303_SEE_OTHER)


@app.get("/spese-secondarie/{doc_id}")
def secondary_expenses_download(request: Request, doc_id: int):
    user = require_login(request)
    if not user:
        return RedirectResponse("/", status_code=HTTP_303_SEE_OTHER)

    if not is_admin(request):
        return PlainTextResponse("Solo admin", status_code=403)

    store = _effective_store(request, user)
    ph = _ph()

    with connect() as conn:
        cur = conn.cursor()
        if store == "ALL":
            row = cur.execute(
                f"SELECT filename, content_type, data FROM secondary_expenses WHERE id={ph}",
                (int(doc_id),),
            ).fetchone()
        else:
            row = cur.execute(
                f"SELECT filename, content_type, data FROM secondary_expenses WHERE id={ph} AND store={ph}",
                (int(doc_id), store),
            ).fetchone()

    if not row:
        return PlainTextResponse("Not found", status_code=404)

    from fastapi.responses import Response
    headers = {"Content-Disposition": f"inline; filename=\"{row.get('filename') or 'spesa'}\""}
    return Response(content=row["data"], media_type=row.get("content_type") or "application/octet-stream", headers=headers)


@app.post("/spese-secondarie/{doc_id}/delete")
def secondary_expenses_delete(request: Request, doc_id: int):
    user = require_login(request)
    if not user:
        return RedirectResponse("/", status_code=HTTP_303_SEE_OTHER)

    store = _effective_store(request, user)
    ph = _ph()
    now = _now()

    with connect() as conn:
        cur = conn.cursor()

        if store == "ALL" and is_admin(request):
            info = cur.execute(
                f"SELECT store, expense_date, filename FROM secondary_expenses WHERE id={ph}",
                (int(doc_id),),
            ).fetchone()
        else:
            info = cur.execute(
                f"SELECT store, expense_date, filename FROM secondary_expenses WHERE id={ph} AND store={ph}",
                (int(doc_id), store),
            ).fetchone()

        if info:
            if store == "ALL" and is_admin(request):
                cur.execute(f"DELETE FROM secondary_expenses WHERE id={ph}", (int(doc_id),))
                store_for_log = info["store"]
            else:
                cur.execute(f"DELETE FROM secondary_expenses WHERE id={ph} AND store={ph}", (int(doc_id), store))
                store_for_log = store

            cur.execute(
                f"INSERT INTO logs(ts, store, username, action, category, name, delta) VALUES({now},{ph},{ph},{ph},{ph},{ph},{ph})",
                (store_for_log, user["username"], "DELETE", "SPESE", str(info.get("expense_date") or ""), 0.0),
            )

    return RedirectResponse("/spese-secondarie", status_code=HTTP_303_SEE_OTHER)


# =========================
# LOGISTICA: ORDINI
# =========================
@app.post("/ordini/add")
def orders_add_from_inventory(request: Request, item_id: int = Form(...), next_url: str = Form("/inventario")):
    user = require_login(request)
    if not user:
        return RedirectResponse("/", status_code=HTTP_303_SEE_OTHER)

    store = _effective_store(request, user)
    ph = _ph()
    now = _now()

    with connect() as conn:
        cur = conn.cursor()
        # prendo info prodotto
        row = cur.execute(
            f"SELECT id, category, name, qty, min_qty FROM products WHERE id={ph} AND store={ph}",
            (int(item_id), store),
        ).fetchone()
        if not row:
            return RedirectResponse("/inventario", status_code=HTTP_303_SEE_OTHER)

        # qty suggerita: almeno 1, altrimenti la differenza col minimo
        try:
            suggested = float(row.get("min_qty") or 0) - float(row.get("qty") or 0)
        except Exception:
            suggested = 1.0
        if suggested <= 0:
            suggested = 1.0

        # evita doppioni: se già in coda, non reinserire
        existing = cur.execute(
            f"SELECT id FROM order_queue WHERE store={ph} AND product_id={ph}",
            (store, int(item_id)),
        ).fetchone()
        if not existing:
            cur.execute(
                f"INSERT INTO order_queue(store, product_id, category, name, qty_to_order, added_by, ts) VALUES({ph},{ph},{ph},{ph},{ph},{ph},{now})",
                (store, int(item_id), row["category"], row["name"], suggested, user["username"]),
            )
            # LOG: richiesta ordine (in arrivo)
            _log(
                cur,
                store=store,
                username=user["username"],
                action="ORDER_IN_ARRIVO",
                category=row["category"],
                name=row["name"],
                delta=float(suggested),
            )

    # torna dove eri (inventario con filtri)
    return RedirectResponse(_safe_next_url(next_url, "/inventario"), status_code=HTTP_303_SEE_OTHER)


@app.post("/ordini/add_with_qty")
def orders_add_from_inventory_with_qty(
    request: Request,
    item_id: int = Form(...),
    qty_to_order: float = Form(...),
    next_url: str = Form("/inventario"),
):
    """Aggiunge (o aggiorna) la riga in coda ordini con una quantità scelta dall'utente."""
    user = require_login(request)
    if not user:
        return RedirectResponse("/", status_code=HTTP_303_SEE_OTHER)

    store = _effective_store(request, user)
    if qty_to_order is None:
        return RedirectResponse(_safe_next_url(next_url, "/inventario"), status_code=HTTP_303_SEE_OTHER)

    try:
        qty_to_order = float(qty_to_order)
    except Exception:
        qty_to_order = 1.0
    if qty_to_order <= 0:
        qty_to_order = 1.0

    ph = _ph()
    now = _now()
    with connect() as conn:
        cur = conn.cursor()
        row = cur.execute(
            f"SELECT id, category, name, qty, min_qty FROM products WHERE id={ph} AND store={ph}",
            (int(item_id), store),
        ).fetchone()
        if not row:
            return RedirectResponse(_safe_next_url(next_url, "/inventario"), status_code=HTTP_303_SEE_OTHER)

        existing = cur.execute(
            f"SELECT id FROM order_queue WHERE store={ph} AND product_id={ph}",
            (store, int(item_id)),
        ).fetchone()

        if existing:
            cur.execute(
                f"UPDATE order_queue SET qty_to_order={ph}, added_by={ph}, ts={now} WHERE id={ph}",
                (float(qty_to_order), user["username"], int(existing["id"])),
            )
        else:
            cur.execute(
                f"INSERT INTO order_queue(store, product_id, category, name, qty_to_order, added_by, ts) VALUES({ph},{ph},{ph},{ph},{ph},{ph},{now})",
                (store, int(item_id), row["category"], row["name"], float(qty_to_order), user["username"]),
            )

        _log(
            cur,
            store=store,
            username=user["username"],
            action="ORDER_IN_ARRIVO",
            category=row["category"],
            name=row["name"],
            delta=float(qty_to_order),
        )

    return RedirectResponse(_safe_next_url(next_url, "/inventario"), status_code=HTTP_303_SEE_OTHER)


@app.get("/ordini", response_class=HTMLResponse)
def orders_queue_page(request: Request):
    user = require_login(request)
    if not user:
        return RedirectResponse("/", status_code=HTTP_303_SEE_OTHER)

    store = _effective_store(request, user)
    brand = "ALL" if store == "ALL" else store
    ph = _ph()

    with connect() as conn:
        cur = conn.cursor()
        if store == "ALL":
            # join per mostrare "rimasti" in inventario
            rows = cur.execute(
                """
                SELECT q.id, q.store, q.category, q.name, q.qty_to_order, q.added_by, q.ts,
                       p.qty AS qty_left, p.unit AS unit
                FROM order_queue q
                LEFT JOIN products p ON p.id = q.product_id
                ORDER BY q.store, q.category, q.name
                """,
            ).fetchall()
        else:
            rows = cur.execute(
                f"""
                SELECT q.id, q.store, q.category, q.name, q.qty_to_order, q.added_by, q.ts,
                       p.qty AS qty_left, p.unit AS unit
                FROM order_queue q
                LEFT JOIN products p ON p.id = q.product_id
                WHERE q.store={ph}
                ORDER BY q.category, q.name
                """,
                (store,),
            ).fetchall()

    active_store = request.session.get("active_store") if is_admin(request) else None
    return render("orders.html", user=user, rows=rows, stores=STORES, brand=brand, active_store=active_store)


@app.post("/ordini/queue-update")
def orders_queue_update(request: Request, queue_id: int = Form(...), qty_to_order: float = Form(...)):
    """Aggiorna quantità da ordinare (decisa dall'utente)."""
    user = require_login(request)
    if not user:
        return RedirectResponse("/", status_code=HTTP_303_SEE_OTHER)

    store = _effective_store(request, user)
    ph = _ph()
    now = _now()

    try:
        qty_to_order = float(qty_to_order)
    except Exception:
        qty_to_order = 1.0
    if qty_to_order <= 0:
        qty_to_order = 1.0

    with connect() as conn:
        cur = conn.cursor()
        if store == "ALL" and is_admin(request):
            info = cur.execute(
                f"SELECT store, category, name FROM order_queue WHERE id={ph}",
                (int(queue_id),),
            ).fetchone()
            if info:
                cur.execute(
                    f"UPDATE order_queue SET qty_to_order={ph}, ts={now}, added_by={ph} WHERE id={ph}",
                    (float(qty_to_order), user["username"], int(queue_id)),
                )
                _log(
                    cur,
                    store=info["store"],
                    username=user["username"],
                    action="ORDER_QTY_UPDATE",
                    category=info["category"],
                    name=info["name"],
                    delta=float(qty_to_order),
                )
        else:
            info = cur.execute(
                f"SELECT category, name FROM order_queue WHERE id={ph} AND store={ph}",
                (int(queue_id), store),
            ).fetchone()
            if info:
                cur.execute(
                    f"UPDATE order_queue SET qty_to_order={ph}, ts={now}, added_by={ph} WHERE id={ph} AND store={ph}",
                    (float(qty_to_order), user["username"], int(queue_id), store),
                )
                _log(
                    cur,
                    store=store,
                    username=user["username"],
                    action="ORDER_QTY_UPDATE",
                    category=info["category"],
                    name=info["name"],
                    delta=float(qty_to_order),
                )

    return RedirectResponse("/ordini", status_code=HTTP_303_SEE_OTHER)


@app.post("/ordini/queue-delete")
def orders_queue_delete(request: Request, queue_id: int = Form(...)):
    """Elimina una riga 'ordine da fare' (se inserita per errore)."""
    user = require_login(request)
    if not user:
        return RedirectResponse("/", status_code=HTTP_303_SEE_OTHER)

    store = _effective_store(request, user)
    ph = _ph()
    now = _now()

    with connect() as conn:
        cur = conn.cursor()

        if store == "ALL" and is_admin(request):
            info = cur.execute(
                f"SELECT store, category, name, qty_to_order FROM order_queue WHERE id={ph}",
                (int(queue_id),),
            ).fetchone()
            if info:
                cur.execute(f"DELETE FROM order_queue WHERE id={ph}", (int(queue_id),))
                _log(
                    cur,
                    store=info["store"],
                    username=user["username"],
                    action="ORDER_ELIMINA_DA_FARE",
                    category=info["category"],
                    name=info["name"],
                    delta=float(info["qty_to_order"] or 0),
                )
        else:
            info = cur.execute(
                f"SELECT category, name, qty_to_order FROM order_queue WHERE id={ph} AND store={ph}",
                (int(queue_id), store),
            ).fetchone()
            if info:
                cur.execute(f"DELETE FROM order_queue WHERE id={ph} AND store={ph}", (int(queue_id), store))
                _log(
                    cur,
                    store=store,
                    username=user["username"],
                    action="ORDER_ELIMINA_DA_FARE",
                    category=info["category"],
                    name=info["name"],
                    delta=float(info["qty_to_order"] or 0),
                )

    return RedirectResponse("/ordini", status_code=HTTP_303_SEE_OTHER)


@app.post("/ordini/confirm")
def orders_confirm(request: Request, supplier: str = Form(...), queue_ids: list[int] = Form(None)):
    user = require_login(request)
    if not user:
        return RedirectResponse("/", status_code=HTTP_303_SEE_OTHER)

    supplier = (supplier or "").strip()
    if not supplier:
        return RedirectResponse("/ordini", status_code=HTTP_303_SEE_OTHER)

    store = _effective_store(request, user)
    ph = _ph()
    now = _now()

    # niente selezione => niente
    if not queue_ids:
        return RedirectResponse("/ordini", status_code=HTTP_303_SEE_OTHER)

    with connect() as conn:
        cur = conn.cursor()

        # carico righe selezionate rispettando lo store corrente
        if store == "ALL" and is_admin(request):
            placeholders = ",".join([ph] * len(queue_ids))
            sel = cur.execute(
                f"SELECT id, store, product_id, category, name, qty_to_order FROM order_queue WHERE id IN ({placeholders})",
                tuple(int(x) for x in queue_ids),
            ).fetchall()
            # in ALL, se selezioni righe di store diversi, creo un ordine per ogni store
            by_store = {}
            for r in sel:
                by_store.setdefault(r["store"], []).append(r)
        else:
            placeholders = ",".join([ph] * len(queue_ids))
            sel = cur.execute(
                f"SELECT id, store, product_id, category, name, qty_to_order FROM order_queue WHERE store={ph} AND id IN ({placeholders})",
                (store, *[int(x) for x in queue_ids]),
            ).fetchall()
            by_store = {store: sel}

        for st, items in by_store.items():
            if not items:
                continue
            # crea ordine
            cur.execute(
                f"INSERT INTO orders(store, supplier, status, created_by, ts) VALUES({ph},{ph},{ph},{ph},{now}) RETURNING id" if using_postgres() else
                f"INSERT INTO orders(store, supplier, status, created_by, ts) VALUES({ph},{ph},{ph},{ph},{now})",
                (st, supplier, "in_corso", user["username"]),
            )
            if using_postgres():
                order_id = cur.fetchone()["id"]
            else:
                order_id = cur.lastrowid

            # LOG: ordine confermato (ordinato)
            _log(
                cur,
                store=st,
                username=user["username"],
                action="ORDER_ORDINATO",
                category=supplier,
                name=f"Ordine #{order_id} ({len(items)} righe)",
                delta=float(len(items)),
            )

            # inserisce linee + rimuove dalla coda
            for r in items:
                cur.execute(
                    f"INSERT INTO order_lines(order_id, product_id, category, name, qty) VALUES({ph},{ph},{ph},{ph},{ph})",
                    (int(order_id), int(r["product_id"]), r["category"], r["name"], float(r["qty_to_order"])),
                )
                cur.execute(
                    f"DELETE FROM order_queue WHERE id={ph}",
                    (int(r["id"]),),
                )

    return RedirectResponse("/ordinati", status_code=HTTP_303_SEE_OTHER)


@app.get("/ordinati", response_class=HTMLResponse)
def orders_in_progress_page(request: Request):
    user = require_login(request)
    if not user:
        return RedirectResponse("/", status_code=HTTP_303_SEE_OTHER)

    store = _effective_store(request, user)
    brand = "ALL" if store == "ALL" else store
    ph = _ph()

    with connect() as conn:
        cur = conn.cursor()
        if store == "ALL":
            os_ = cur.execute(
                "SELECT id, store, supplier, status, created_by, ts, kind, from_store, to_store, transfer_id FROM orders WHERE status='in_corso' ORDER BY ts DESC, id DESC",
            ).fetchall()
        else:
            # Mostra anche gli scambi in uscita (creati da questo negozio) così il mittente li vede e può modificarli.
            os_ = cur.execute(
                f"""
                SELECT id, store, supplier, status, created_by, ts, kind, from_store, to_store, transfer_id
                FROM orders
                WHERE status='in_corso'
                  AND (store={ph} OR (kind='transfer' AND from_store={ph}))
                ORDER BY ts DESC, id DESC
                """,
                (store, store),
            ).fetchall()

        orders = []
        for o in os_:
            lines = cur.execute(
                f"SELECT id, product_id, category, name, qty, area, unit FROM order_lines WHERE order_id={ph} ORDER BY category, name",
                (int(o["id"]),),
            ).fetchall()
            od = dict(o)
            # direzione (solo per visualizzazione)
            try:
                if (od.get("kind") or "ordine") == "transfer" and store != "ALL":
                    od["direction"] = "uscita" if od.get("from_store") == store else "entrata"
                else:
                    od["direction"] = "entrata"
            except Exception:
                od["direction"] = "entrata"
            od["lines"] = lines
            orders.append(od)

    active_store = request.session.get("active_store") if is_admin(request) else None
    return render("ordered.html", user=user, orders=orders, stores=STORES, brand=brand, active_store=active_store)


@app.post("/ordinati/arrivato")
async def orders_mark_arrived(request: Request, order_id: int = Form(...)):
    """Conferma arrivo: aggiorna inventario (+ quantità ricevute), gestisce mancanti e chiude l'ordine."""
    user = require_login(request)
    if not user:
        return RedirectResponse("/", status_code=HTTP_303_SEE_OTHER)

    store = _effective_store(request, user)
    ph = _ph()

    delivered_date = _today_str()

    with connect() as conn:
        cur = conn.cursor()

        # carico ordine + store reale
        if store == "ALL" and is_admin(request):
            info = cur.execute(
                f"SELECT store, supplier, ts, kind, from_store, to_store, transfer_id FROM orders WHERE id={ph} AND status='in_corso'",
                (int(order_id),),
            ).fetchone()
        else:
            info = cur.execute(
                f"SELECT store, supplier, ts, kind, from_store, to_store, transfer_id FROM orders WHERE id={ph} AND status='in_corso' AND store={ph}",
                (int(order_id), store),
            ).fetchone()

        if not info:
            return RedirectResponse("/ordinati", status_code=HTTP_303_SEE_OTHER)

        real_store = info["store"]
        supplier = info["supplier"]
        order_ts = str(info.get("ts") or "")
        order_date = order_ts[:10] if len(order_ts) >= 10 else delivered_date

        lines = cur.execute(
            f"SELECT id, product_id, category, name, qty, area, unit FROM order_lines WHERE order_id={ph}",
            (int(order_id),),
        ).fetchall()

        form = await request.form()
        missing_ids = set()
        try:
            missing_ids = {int(x) for x in form.getlist("missing_ids")}
        except Exception:
            missing_ids = set()

        now = _now()

        kind = str(info.get("kind") or "ordine").lower()
        is_transfer = kind in ("transfer", "scambio")
        src_store = info.get("from_store")
        dst_store = info.get("to_store")

        for ln in lines:
            lid = int(ln["id"])
            pid = ln.get("product_id")
            base_qty = float(ln.get("qty") or 0)
            cat = ln.get("category")
            nm = ln.get("name")

            # quantità ricevuta (default = quantità ordinata)
            recv_key = f"received_{lid}"
            recv_raw = form.get(recv_key)
            try:
                received_qty = float(recv_raw) if recv_raw is not None else base_qty
            except Exception:
                received_qty = base_qty
            if received_qty < 0:
                received_qty = 0.0

            is_missing = lid in missing_ids
            # salvo esito riga (storico)
            try:
                cur.execute(
                    f"UPDATE order_lines SET received_qty={ph}, is_missing={ph} WHERE id={ph}",
                    (float(received_qty), 1 if is_missing else 0, int(lid)),
                )
            except Exception:
                # non bloccare la consegna se la migrazione non è ancora stata applicata
                pass
            if is_missing or received_qty == 0:
                if is_transfer:
                    # Scambio: NON reinserisco in "ordini da fare" e non segno mancanti sul prodotto.

                    _log(
                        cur,
                        store=real_store,
                        username=user["username"],
                        action="TRANSFER_MANCANTE",
                        category=cat,
                        name=f"{nm} (scambio da {STORES.get(src_store, src_store)})" if src_store else f"{nm} (scambio)",
                        delta=float(base_qty),
                    )
                else:
                    # NON va in inventario: metto "mancante" + reinserisco in coda ordini
                    if pid:
                        cur.execute(
                            f"UPDATE products SET missing_order_date={ph}, missing_delivery_date={ph}, missing_qty={ph}, updated_at={now} WHERE id={ph} AND store={ph}",
                            (order_date, delivered_date, float(base_qty), int(pid), real_store),
                        )

                    # reinserisci/aggiorna in coda per riordino
                    if pid:
                        ex = cur.execute(
                            f"SELECT id FROM order_queue WHERE store={ph} AND product_id={ph}",
                            (real_store, int(pid)),
                        ).fetchone()
                        if ex:
                            cur.execute(
                                f"UPDATE order_queue SET qty_to_order={ph}, added_by={ph}, ts={now} WHERE id={ph}",
                                (float(base_qty), user["username"], int(ex["id"])),
                            )
                        else:
                            cur.execute(
                                f"INSERT INTO order_queue(store, product_id, category, name, qty_to_order, added_by, ts) VALUES({ph},{ph},{ph},{ph},{ph},{ph},{now})",
                                (real_store, int(pid), cat, nm, float(base_qty), user["username"]),
                            )

                    _log(
                        cur,
                        store=real_store,
                        username=user["username"],
                        action="ORDER_MANCANTE",
                        category=cat,
                        name=f"{nm} (ordine {order_date})",
                        delta=float(base_qty),
                    )
            else:
                # Va in inventario
                line_area = (ln.get("area") or (get_selected_area(request) or "prodotti") or "prodotti")
                if line_area not in AREAS:
                    line_area = "prodotti"
                line_unit = (ln.get("unit") or "").strip()

                if pid:
                    cur.execute(
                        f"UPDATE products SET qty=qty+{ph}, missing_order_date=NULL, missing_delivery_date=NULL, missing_qty={ph}, updated_at={now} WHERE id={ph} AND store={ph}",
                        (float(received_qty), 0.0, int(pid), real_store),
                    )
                else:
                    # Ordine senza product_id (es. scambio): cerco per nome/categoria/area e se non esiste lo creo.
                    existing = cur.execute(
                        f"SELECT id, unit FROM products WHERE store={ph} AND category={ph} AND name={ph} AND area={ph}",
                        (real_store, cat, nm, line_area),
                    ).fetchone()
                    if existing:
                        cur.execute(
                            f"UPDATE products SET qty=qty+{ph}, unit=COALESCE(NULLIF(unit,''), {ph}), updated_at={now} WHERE id={ph} AND store={ph}",
                            (float(received_qty), line_unit, int(existing["id"]), real_store),
                        )
                    else:
                        cur.execute(
                            f"INSERT INTO products(store, category, name, area, location, unit, qty, min_qty, updated_at) VALUES({ph},{ph},{ph},{ph},{ph},{ph},{ph},{ph},{now})",
                            (real_store, cat, nm, line_area, line_unit, float(received_qty), 0.0),
                        )
                # --- SCAMBIO: aggiorno il mittente SOLO alla conferma del ricevente ---
                if is_transfer and src_store and dst_store and src_store in STORES and dst_store in STORES:
                    src_prod = cur.execute(
                        f"SELECT id, qty FROM products WHERE store={ph} AND category={ph} AND name={ph} AND area={ph}",
                        (src_store, cat, nm, line_area),
                    ).fetchone()
                    if src_prod:
                        avail_src = float(src_prod.get("qty") or 0)
                        new_qty = max(0.0, avail_src - float(received_qty))
                        cur.execute(
                            f"UPDATE products SET qty={ph}, updated_at={now} WHERE id={ph} AND store={ph}",
                            (new_qty, int(src_prod["id"]), src_store),
                        )
                        _log(
                            cur,
                            store=src_store,
                            username=user["username"],
                            action="TRANSFER_OUT_CONFERMATO",
                            category=cat,
                            name=f"{nm} → {STORES.get(dst_store, dst_store)} (confermato)",
                            delta=-float(received_qty),
                        )
                        if avail_src < float(received_qty):
                            _log(
                                cur,
                                store=src_store,
                                username=user["username"],
                                action="TRANSFER_WARNING",
                                category=cat,
                                name=f"{nm}: richiesto/scaricato {received_qty} ma disponibili {avail_src}",
                                delta=0.0,
                            )
                    else:
                        _log(
                            cur,
                            store=src_store,
                            username=user["username"],
                            action="TRANSFER_WARNING",
                            category=cat,
                            name=f"{nm}: prodotto non trovato nel negozio sorgente (scambio confermato)",
                            delta=0.0,
                        )



                _log(
                    cur,
                    store=real_store,
                    username=user["username"],
                    action=("TRANSFER_CARICATO_INVENTARIO" if is_transfer else "ORDER_CARICATO_INVENTARIO"),
                    category=cat,
                    name=nm,
                    delta=float(received_qty),
                )


        # chiudo ordine (NON cancellare: serve lo storico)
        cur.execute(
            f"UPDATE orders SET status='chiuso', closed_at={now} WHERE id={ph}",
            (int(order_id),),
        )

        _log(
            cur,
            store=real_store,
            username=user["username"],
            action=("TRANSFER_ARRIVATO" if is_transfer else "ORDER_ARRIVATO"),
            category=supplier,
            name=(f"Scambio #{int(order_id)} ricevuto ({delivered_date})" if is_transfer else f"Ordine #{int(order_id)} arrivato ({delivered_date})"),
            delta=0.0,
        )

    return RedirectResponse("/ordinati", status_code=HTTP_303_SEE_OTHER)


@app.get("/storico-ordini", response_class=HTMLResponse)
def orders_history_page(request: Request, kind: str = ""):
    """Storico (archivio) di ordini e scambi."""
    user = require_login(request)
    if not user:
        return RedirectResponse("/", status_code=HTTP_303_SEE_OTHER)

    store = _effective_store(request, user)
    brand = "ALL" if store == "ALL" else store
    kind = (kind or "").strip().lower()
    if kind not in ("", "ordine", "transfer"):
        kind = ""

    ph = _ph()
    with connect() as conn:
        cur = conn.cursor()

        params = []
        where = "status != 'in_corso'"

        if store == "ALL":
            pass
        else:
            # includi anche scambi in uscita
            where += f" AND (store={ph} OR (kind='transfer' AND from_store={ph}))"
            params.extend([store, store])

        if kind:
            where += f" AND kind={ph}"
            params.append(kind)

        rows = cur.execute(
            f"""
            SELECT id, store, supplier, status, created_by, ts, kind, from_store, to_store, transfer_id, closed_at
            FROM orders
            WHERE {where}
            ORDER BY COALESCE(closed_at, ts) DESC, id DESC
            """,
            tuple(params),
        ).fetchall()

        orders = []
        for o in rows:
            lines = cur.execute(
                f"SELECT id, category, name, qty, area, unit, received_qty, is_missing FROM order_lines WHERE order_id={ph} ORDER BY category, name",
                (int(o["id"]),),
            ).fetchall()
            od = dict(o)
            # direzione per la vista corrente
            if store != "ALL" and (od.get("kind") or "ordine") == "transfer":
                od["direction"] = "uscita" if od.get("from_store") == store else "entrata"
            else:
                od["direction"] = "entrata"
            od["lines"] = lines
            orders.append(od)

    active_store = request.session.get("active_store") if is_admin(request) else None
    return render(
        "order_history.html",
        user=user,
        orders=orders,
        kind=kind,
        stores=STORES,
        areas=AREAS,
        brand=brand,
        active_store=active_store,
    )


# =========================
# SCAMBI TRA NEGOZI
# =========================
@app.get("/scambi", response_class=HTMLResponse)
def transfers_page(request: Request, from_store: str = "", to_store: str = ""):
    user = require_login(request)
    if not user:
        return RedirectResponse("/", status_code=HTTP_303_SEE_OTHER)

    area = get_selected_area(request) or "prodotti"
    if area not in AREAS:
        area = "prodotti"

    # default: sorgente = negozio utente (o active_store per admin), destinazione = altro negozio
    admin = is_admin(request)
    default_from = (request.session.get("active_store") if admin else user.get("store")) or "spinza"
    if default_from == "ALL":
        default_from = "spinza"

    from_store = (from_store or default_from).strip() or default_from
    if from_store not in STORES:
        from_store = default_from

    if not to_store:
        # scegli il primo negozio diverso
        to_store = next((k for k in STORES.keys() if k != from_store), from_store)
    to_store = to_store.strip()
    if to_store not in STORES:
        to_store = next((k for k in STORES.keys() if k != from_store), from_store)

    ph = _ph()
    with connect() as conn:
        cur = conn.cursor()
        products = cur.execute(
            f"SELECT id, category, name, qty, unit FROM products WHERE store={ph} AND area={ph} ORDER BY category, name",
            (from_store, area),
        ).fetchall()

    return render(
        "transfers.html",
        user=user,
        stores=STORES,
        from_store=from_store,
        to_store=to_store,
        products=products,
        area=area,
        areas=AREAS,
        brand=(from_store if from_store else "spinza"),
        active_store=request.session.get("active_store") if admin else None,
    )


@app.get("/scambi/modifica/{order_id}", response_class=HTMLResponse)
def transfers_edit_page(request: Request, order_id: int):
    """Permette al mittente di modificare uno scambio in uscita (finché non viene confermato dal destinatario)."""
    user = require_login(request)
    if not user:
        return RedirectResponse("/", status_code=HTTP_303_SEE_OTHER)

    store = _effective_store(request, user)
    ph = _ph()

    with connect() as conn:
        cur = conn.cursor()
        o = cur.execute(
            f"SELECT id, store, kind, status, from_store, to_store, transfer_id FROM orders WHERE id={ph}",
            (int(order_id),),
        ).fetchone()
        if not o:
            return RedirectResponse("/ordinati", status_code=HTTP_303_SEE_OTHER)

        kind = (o.get("kind") or "ordine")
        if kind != "transfer" or o.get("status") != "in_corso":
            return RedirectResponse("/ordinati", status_code=HTTP_303_SEE_OTHER)

        from_store = o.get("from_store")
        to_store = o.get("to_store")
        transfer_id = o.get("transfer_id")

        # Permessi: mittente o admin
        if not is_admin(request) and store != from_store:
            return RedirectResponse("/ordinati", status_code=HTTP_303_SEE_OTHER)

        # Carico tutti i prodotti del negozio sorgente (tutte le aree) per poter aggiungere righe liberamente
        products = cur.execute(
            f"SELECT id, category, name, area, qty, unit FROM products WHERE store={ph} ORDER BY area, category, name",
            (from_store,),
        ).fetchall()

        selected = {}
        if transfer_id:
            tls = cur.execute(
                f"SELECT category, name, area, qty FROM transfer_lines WHERE transfer_id={ph}",
                (int(transfer_id),),
            ).fetchall()
            for tl in tls:
                k = f"{tl['category']}||{tl['name']}||{(tl.get('area') or 'prodotti')}"
                selected[k] = float(tl.get("qty") or 0)

    return render(
        "transfers_edit.html",
        user=user,
        order_id=int(order_id),
        from_store=from_store,
        to_store=to_store,
        products=products,
        selected=selected,
        stores=STORES,
        areas=AREAS,
        brand=from_store,
        active_store=request.session.get("active_store") if is_admin(request) else None,
    )


@app.post("/scambi/modifica/submit")
async def transfers_edit_submit(request: Request, order_id: int = Form(...)):
    user = require_login(request)
    if not user:
        return RedirectResponse("/", status_code=HTTP_303_SEE_OTHER)

    store = _effective_store(request, user)
    ph = _ph()
    now = _now()

    form = await request.form()

    with connect() as conn:
        cur = conn.cursor()
        o = cur.execute(
            f"SELECT id, store, kind, status, from_store, to_store, transfer_id FROM orders WHERE id={ph}",
            (int(order_id),),
        ).fetchone()
        if not o:
            return RedirectResponse("/ordinati", status_code=HTTP_303_SEE_OTHER)

        if (o.get("kind") or "ordine") != "transfer" or o.get("status") != "in_corso":
            return RedirectResponse("/ordinati", status_code=HTTP_303_SEE_OTHER)

        from_store = o.get("from_store")
        to_store = o.get("to_store")
        transfer_id = o.get("transfer_id")

        if not is_admin(request) and store != from_store:
            return RedirectResponse("/ordinati", status_code=HTTP_303_SEE_OTHER)

        # raccolgo righe selezionate
        move_requests = []
        for key in form.keys():
            if not key.startswith("qty_"):
                continue
            try:
                pid = int(key.split("_", 1)[1])
            except Exception:
                continue
            raw = form.get(key)
            try:
                q = float(raw)
            except Exception:
                q = 0.0
            if q and q > 0:
                move_requests.append((pid, q))

        # reset righe (storico trasferimento + righe ordine)
        if transfer_id:
            cur.execute(f"DELETE FROM transfer_lines WHERE transfer_id={ph}", (int(transfer_id),))
        cur.execute(f"DELETE FROM order_lines WHERE order_id={ph}", (int(order_id),))

        for pid, q in move_requests:
            src = cur.execute(
                f"SELECT id, category, name, qty, unit, area FROM products WHERE id={ph} AND store={ph}",
                (int(pid), from_store),
            ).fetchone()
            if not src:
                continue

            available = float(src.get("qty") or 0)
            qty_move = min(float(q), available)
            if qty_move <= 0:
                continue

            # righe trasferimento (storico)
            if transfer_id:
                cur.execute(
                    f"INSERT INTO transfer_lines(transfer_id, from_store, to_store, category, name, area, qty, unit) VALUES({ph},{ph},{ph},{ph},{ph},{ph},{ph},{ph})",
                    (int(transfer_id), from_store, to_store, src["category"], src["name"], (src.get("area") or "prodotti"), float(qty_move), (src.get("unit") or "")),
                )

            # righe ordine (per negozio ricevente)
            cur.execute(
                f"INSERT INTO order_lines(order_id, product_id, category, name, qty, area, unit) VALUES({ph},NULL,{ph},{ph},{ph},{ph},{ph})",
                (int(order_id), src["category"], src["name"], float(qty_move), (src.get("area") or "prodotti"), (src.get("unit") or "")),
            )

        # aggiorno timestamp ordine (così sale in cima)
        try:
            cur.execute(f"UPDATE orders SET ts={now} WHERE id={ph}", (int(order_id),))
        except Exception:
            pass

        _log(
            cur,
            store=from_store,
            username=user["username"],
            action="TRANSFER_MODIFICATO",
            category="SCAMBI",
            name=f"Scambio #{int(order_id)} modificato",
            delta=float(len(move_requests)),
        )
        _log(
            cur,
            store=to_store,
            username=user["username"],
            action="TRANSFER_MODIFICATO",
            category="SCAMBI",
            name=f"Scambio #{int(order_id)} aggiornato dal mittente",
            delta=float(len(move_requests)),
        )

    return RedirectResponse("/ordinati", status_code=HTTP_303_SEE_OTHER)


@app.post("/scambi/submit")
async def transfers_submit(request: Request):
    user = require_login(request)
    if not user:
        return RedirectResponse("/", status_code=HTTP_303_SEE_OTHER)

    form = await request.form()
    from_store = (form.get("from_store") or "").strip()
    to_store = (form.get("to_store") or "").strip()

    if from_store not in STORES or to_store not in STORES or from_store == to_store:
        return RedirectResponse("/scambi", status_code=HTTP_303_SEE_OTHER)

    area = get_selected_area(request) or "prodotti"
    if area not in AREAS:
        area = "prodotti"

    ph = _ph()
    now = _now()

    with connect() as conn:
        cur = conn.cursor()
        # raccolgo righe richieste
        move_requests = []
        for key in form.keys():
            if not key.startswith("qty_"):
                continue
            try:
                pid = int(key.split("_", 1)[1])
            except Exception:
                continue
            raw = form.get(key)
            try:
                q = float(raw)
            except Exception:
                q = 0.0
            if q > 0:
                move_requests.append((pid, q))

        if not move_requests:
            return RedirectResponse(f"/scambi?from_store={from_store}&to_store={to_store}", status_code=HTTP_303_SEE_OTHER)

        # crea testata trasferimento (storico)
        cur.execute(
            f"INSERT INTO transfers(from_store, to_store, created_by, ts) VALUES({ph},{ph},{ph},{now}) RETURNING id" if using_postgres() else
            f"INSERT INTO transfers(from_store, to_store, created_by, ts) VALUES({ph},{ph},{ph},{now})",
            (from_store, to_store, user["username"]),
        )
        if using_postgres():
            transfer_id = int(cur.fetchone()["id"])
        else:
            transfer_id = int(cur.lastrowid)

        # crea "ordine" di tipo SCAMBIO per il negozio ricevente (apparirà in Ordini in corso)
        supplier_label = f"SCAMBIO da {STORES.get(from_store, from_store)}"
        cur.execute(
            f"INSERT INTO orders(store, supplier, status, created_by, ts, kind, from_store, to_store, transfer_id) VALUES({ph},{ph},{ph},{ph},{now},{ph},{ph},{ph},{ph}) RETURNING id" if using_postgres() else
            f"INSERT INTO orders(store, supplier, status, created_by, ts, kind, from_store, to_store, transfer_id) VALUES({ph},{ph},{ph},{ph},{now},{ph},{ph},{ph},{ph})",
            (to_store, supplier_label, "in_corso", user["username"], "transfer", from_store, to_store, int(transfer_id)),
        )
        if using_postgres():
            order_id = int(cur.fetchone()["id"])
        else:
            order_id = int(cur.lastrowid)

        _log(
            cur,
            store=to_store,
            username=user["username"],
            action="TRANSFER_CREATO",
            category=supplier_label,
            name=f"Scambio #{order_id} in attesa",
            delta=0.0,
        )

        for pid, q in move_requests:
            src = cur.execute(
                f"SELECT id, category, name, qty, min_qty, unit, area FROM products WHERE id={ph} AND store={ph}",
                (pid, from_store),
            ).fetchone()
            if not src:
                continue

            # clamp: non andare sotto zero
            available = float(src.get("qty") or 0)
            qty_move = min(float(q), available)
            if qty_move <= 0:
                continue
            # righe trasferimento (storico)
            cur.execute(
                f"INSERT INTO transfer_lines(transfer_id, from_store, to_store, category, name, area, qty, unit) VALUES({ph},{ph},{ph},{ph},{ph},{ph},{ph},{ph})",
                (transfer_id, from_store, to_store, src["category"], src["name"], src.get("area") or area, float(qty_move), (src.get("unit") or "")),
            )

            # righe "ordine" per ricevente
            cur.execute(
                f"INSERT INTO order_lines(order_id, product_id, category, name, qty, area, unit) VALUES({ph},NULL,{ph},{ph},{ph},{ph},{ph})",
                (int(order_id), src["category"], src["name"], float(qty_move), (src.get("area") or area), (src.get("unit") or "")),
            )

            _log(
                cur,
                store=from_store,
                username=user["username"],
                action="TRANSFER_RICHIESTO",
                category=src["category"],
                name=f"{src['name']} → {STORES.get(to_store, to_store)}",
                delta=0.0,
            )
            _log(
                cur,
                store=to_store,
                username=user["username"],
                action="TRANSFER_IN_ATTESA",
                category=src["category"],
                name=f"{src['name']} ← {STORES.get(from_store, from_store)}",
                delta=0.0,
            )
    return RedirectResponse(f"/scambi?from_store={from_store}&to_store={to_store}", status_code=HTTP_303_SEE_OTHER)


@app.get("/fatture", response_class=HTMLResponse)
def invoices_page(request: Request, supplier: str = "", q_date: str = ""):
    user = require_login(request)
    if not user:
        return RedirectResponse("/", status_code=HTTP_303_SEE_OTHER)

    store = _effective_store(request, user)
    brand = "ALL" if store == "ALL" else store

    supplier = (supplier or "").strip()
    q_date = (q_date or "").strip()

    ph = _ph()
    with connect() as conn:
        cur = conn.cursor()
        if store == "ALL":
            sql = "SELECT id, store, supplier, doc_date, uploaded_by, ts, filename, content_type FROM invoices_docs WHERE 1=1"
            params = []
        else:
            sql = f"SELECT id, store, supplier, doc_date, uploaded_by, ts, filename, content_type FROM invoices_docs WHERE store={ph}"
            params = [store]

        if supplier:
            sql += f" AND lower(supplier) LIKE {ph}"
            params.append(f"%{supplier.lower()}%")
        if q_date:
            sql += f" AND CAST(doc_date AS TEXT) LIKE {ph}"
            params.append(f"%{q_date}%")

        sql += " ORDER BY store, doc_date DESC, id DESC" if store == "ALL" else " ORDER BY doc_date DESC, id DESC"
        rows = cur.execute(sql, tuple(params)).fetchall()

    active_store = request.session.get("active_store") if is_admin(request) else None
    return render("invoices.html", user=user, rows=rows, supplier=supplier, q_date=q_date, stores=STORES, brand=brand, active_store=active_store)


@app.post("/fatture/upload")
async def invoices_upload(request: Request, supplier: str = Form(...), doc_date: str = Form(...), files: list[UploadFile] = File(...)):
    user = require_login(request)
    if not user:
        return RedirectResponse("/", status_code=HTTP_303_SEE_OTHER)

    store = _effective_store(request, user)

    pdfs: list[bytes] = []
    base_name = "fattura"
    for f in files:
        raw = await f.read()
        filename_in = f.filename or base_name
        content_type_in = f.content_type or "application/octet-stream"
        pdf_bytes, _, _ = ensure_pdf(raw, filename_in, content_type_in)
        pdfs.append(pdf_bytes)

    # Unisco tutto in un unico PDF (multi-pagina / multi-file)
    if not pdfs:
        return RedirectResponse("/fatture", status_code=HTTP_303_SEE_OTHER)

    content = merge_pdfs(pdfs) if len(pdfs) > 1 else pdfs[0]
    first_name = (files[0].filename if files and files[0].filename else base_name) or base_name
    filename = first_name if first_name.lower().endswith(".pdf") else f"{first_name}.pdf"
    content_type = "application/pdf"

    ph = _ph()
    now = _now()
    with connect() as conn:
        cur = conn.cursor()
        cur.execute(
            f"INSERT INTO invoices_docs(store, supplier, doc_date, uploaded_by, ts, filename, content_type, data) VALUES({ph},{ph},{ph},{ph},{now},{ph},{ph},{ph})",
            (store, supplier, doc_date, user["username"], filename, content_type, content),
        )
        _log(
            cur,
            store=store,
            username=user["username"],
            action="DOC_UPLOAD",
            category="FATTURE",
            name=f"Fattura {supplier} {doc_date} ({filename})",
            delta=0.0,
        )

    return RedirectResponse("/fatture", status_code=HTTP_303_SEE_OTHER)


# =========================
# IMPORT PRODOTTI DA FOTO FATTURA (GUIDATO)
# =========================

@app.get("/fatture/importa-prodotti", response_class=HTMLResponse)
def invoice_import_upload_page(request: Request):
    user = require_login(request)
    if not user:
        return RedirectResponse("/", status_code=HTTP_303_SEE_OTHER)

    default_area = get_selected_area(request) or "prodotti"
    store = _effective_store(request, user)
    return render(
        "invoice_import_upload.html",
        user=user,
        default_area=default_area,
        brand=store,
    )


@app.post("/fatture/importa-prodotti/upload")
async def invoice_import_upload(request: Request, supplier: str = Form(...), doc_date: str = Form(...), area: str = Form(...), file: UploadFile = File(...)):
    user = require_login(request)
    if not user:
        return RedirectResponse("/", status_code=HTTP_303_SEE_OTHER)

    store = _effective_store(request, user)
    supplier = (supplier or "").strip()
    area = (area or "prodotti").strip().lower()
    if area not in ("bibite", "prodotti"):
        area = "prodotti"

    if not supplier:
        return RedirectResponse("/fatture/importa-prodotti", status_code=HTTP_303_SEE_OTHER)
    try:
        _ = date.fromisoformat(doc_date)
    except Exception:
        return RedirectResponse("/fatture/importa-prodotti", status_code=HTTP_303_SEE_OTHER)

    raw = await file.read()
    filename = file.filename or "fattura"
    content_type = file.content_type or "application/octet-stream"
    content, filename, content_type = ensure_pdf(raw, filename, content_type)

    ph = _ph()
    now = _now()
    with connect() as conn:
        cur = conn.cursor()
        cur.execute(
            f"INSERT INTO invoice_import_drafts(store, supplier, doc_date, uploaded_by, ts, filename, content_type, data) VALUES({ph},{ph},{ph},{ph},{now},{ph},{ph},{ph}) RETURNING id",
            (store, supplier, doc_date, user["username"], filename, content_type, content),
        )
        draft_id = cur.fetchone()[0]

    # Salvo l'area in sessione (comodo)
    set_selected_area(request, area)

    return RedirectResponse(f"/fatture/importa-prodotti/review/{draft_id}?area={area}", status_code=HTTP_303_SEE_OTHER)


@app.get("/fatture/importa-prodotti/draft/{draft_id}/image")
def invoice_import_draft_image(request: Request, draft_id: int):
    user = require_login(request)
    if not user:
        return RedirectResponse("/", status_code=HTTP_303_SEE_OTHER)
    store = _effective_store(request, user)
    ph = _ph()
    with connect() as conn:
        cur = conn.cursor()
        row = cur.execute(
            f"SELECT filename, content_type, data FROM invoice_import_drafts WHERE id={ph} AND store={ph}",
            (int(draft_id), store),
        ).fetchone()
    if not row:
        return PlainTextResponse("Not found", status_code=404)
    from fastapi.responses import Response
    headers = {"Content-Disposition": f"inline; filename=\"{row.get('filename') or 'fattura'}\""}
    return Response(content=row["data"], media_type=row.get("content_type") or "application/octet-stream", headers=headers)


@app.get("/fatture/importa-prodotti/review/{draft_id}", response_class=HTMLResponse)
def invoice_import_review_page(request: Request, draft_id: int, area: str = ""):
    user = require_login(request)
    if not user:
        return RedirectResponse("/", status_code=HTTP_303_SEE_OTHER)

    store = _effective_store(request, user)
    area = (area or get_selected_area(request) or "prodotti").strip().lower()
    if area not in ("bibite", "prodotti"):
        area = "prodotti"

    ph = _ph()
    with connect() as conn:
        cur = conn.cursor()
        draft = cur.execute(
            f"SELECT supplier, doc_date FROM invoice_import_drafts WHERE id={ph} AND store={ph}",
            (int(draft_id), store),
        ).fetchone()
        if not draft:
            return RedirectResponse("/fatture", status_code=HTTP_303_SEE_OTHER)

        products = cur.execute(
            f"SELECT id, category, name FROM products WHERE store={ph} AND area={ph} ORDER BY category, name",
            (store, area),
        ).fetchall()

    products_payload = [
        {"id": p["id"], "category": p["category"], "name": p["name"]}
        for p in products
    ]

    default_category = "BEVERAGE" if area == "bibite" else "PRODOTTI"

    return render(
        "invoice_import_review.html",
        user=user,
        draft_id=draft_id,
        supplier=draft["supplier"],
        doc_date=str(draft["doc_date"]),
        area=area,
        default_category=default_category,
        products_json=json.dumps(products_payload),
        brand=store,
    )


@app.post("/fatture/importa-prodotti/cancel/{draft_id}")
async def invoice_import_cancel(request: Request, draft_id: int):
    user = require_login(request)
    if not user:
        return RedirectResponse("/", status_code=HTTP_303_SEE_OTHER)

    store = _effective_store(request, user)
    ph = _ph()
    with connect() as conn:
        cur = conn.cursor()
        cur.execute(
            f"DELETE FROM invoice_import_drafts WHERE id={ph} AND store={ph}",
            (int(draft_id), store),
        )
    return RedirectResponse("/fatture", status_code=HTTP_303_SEE_OTHER)


@app.post("/fatture/importa-prodotti/confirm/{draft_id}")
async def invoice_import_confirm(request: Request, draft_id: int):
    user = require_login(request)
    if not user:
        return RedirectResponse("/", status_code=HTTP_303_SEE_OTHER)

    store = _effective_store(request, user)
    form = await request.form()

    supplier = str(form.get("supplier") or "").strip()
    doc_date = str(form.get("doc_date") or "").strip()
    area = str(form.get("area") or "prodotti").strip().lower()
    if area not in ("bibite", "prodotti"):
        area = "prodotti"

    raw_names = form.getlist("raw_name")
    qtys = form.getlist("qty")
    units = form.getlist("unit")
    cats = form.getlist("category")
    prod_ids = form.getlist("product_id")

    # validazione minima
    if not raw_names or not qtys or len(raw_names) != len(qtys) or len(raw_names) != len(cats) or len(raw_names) != len(prod_ids):
        return RedirectResponse(f"/fatture/importa-prodotti/review/{draft_id}?area={area}", status_code=HTTP_303_SEE_OTHER)

    try:
        _ = date.fromisoformat(doc_date)
    except Exception:
        return RedirectResponse(f"/fatture/importa-prodotti/review/{draft_id}?area={area}", status_code=HTTP_303_SEE_OTHER)

    ph = _ph()
    now = _now()

    with connect() as conn:
        cur = conn.cursor()

        # 1) recupero bozza
        draft = cur.execute(
            f"SELECT supplier, doc_date, filename, content_type, data FROM invoice_import_drafts WHERE id={ph} AND store={ph}",
            (int(draft_id), store),
        ).fetchone()
        if not draft:
            return RedirectResponse("/fatture", status_code=HTTP_303_SEE_OTHER)

        # 2) salvo documento in archivio fatture
        cur.execute(
            f"INSERT INTO invoices_docs(store, supplier, doc_date, uploaded_by, ts, filename, content_type, data) VALUES({ph},{ph},{ph},{ph},{now},{ph},{ph},{ph}) RETURNING id",
            (store, supplier or draft["supplier"], doc_date or str(draft["doc_date"]), user["username"], draft["filename"], draft["content_type"], draft["data"]),
        )
        invoice_doc_id = cur.fetchone()[0]

        # 3) creo import
        cur.execute(
            f"INSERT INTO invoice_imports(store, invoice_doc_id, supplier, doc_date, area, created_by, ts) VALUES({ph},{ph},{ph},{ph},{ph},{ph},{now}) RETURNING id",
            (store, invoice_doc_id, supplier or draft["supplier"], doc_date or str(draft["doc_date"]), area, user["username"]),
        )
        import_id = cur.fetchone()[0]

        # 4) applico righe: aggiorno/creo prodotti + scrivo log + salvo linee
        for i in range(len(raw_names)):
            rn = str(raw_names[i] or "").strip()
            if not rn:
                continue
            try:
                q = float(str(qtys[i] or "0").replace(",", "."))
            except Exception:
                q = 0.0
            if q == 0:
                continue
            unit = str(units[i] or "").strip() if i < len(units) else ""
            cat = str(cats[i] or "").strip() or ("BEVERAGE" if area == "bibite" else "PRODOTTI")
            pid = str(prod_ids[i] or "").strip()

            product_id = None
            product_name = rn

            if pid and pid != "__new__":
                # aggiorno prodotto esistente
                try:
                    product_id = int(pid)
                except Exception:
                    product_id = None
                if product_id:
                    rowp = cur.execute(
                        f"SELECT id, category, name, qty FROM products WHERE id={ph} AND store={ph}",
                        (product_id, store),
                    ).fetchone()
                    if rowp:
                        product_name = rowp["name"]
                        cur.execute(
                            f"UPDATE products SET qty = qty + {ph}, updated_at={_now()} WHERE id={ph} AND store={ph}",
                            (q, product_id, store),
                        )
            else:
                # crea (o aggiorna se già esiste)
                # unique index: (store, category, name)
                # 1) prova select
                rowx = cur.execute(
                    f"SELECT id FROM products WHERE store={ph} AND category={ph} AND name={ph}",
                    (store, cat, rn),
                ).fetchone()
                if rowx:
                    product_id = rowx["id"]
                    cur.execute(
                        f"UPDATE products SET qty = qty + {ph}, updated_at={_now()} WHERE id={ph} AND store={ph}",
                        (q, product_id, store),
                    )
                else:
                    cur.execute(
                        f"INSERT INTO products(store, category, name, area, qty, min_qty, updated_at) VALUES({ph},{ph},{ph},{ph},{ph},{ph},{_now()}) RETURNING id",
                        (store, cat, rn, area, q, 0),
                    )
                    product_id = cur.fetchone()[0]

            # salva line
            cur.execute(
                f"INSERT INTO invoice_import_lines(import_id, raw_name, category, qty, unit, product_id, product_name) VALUES({ph},{ph},{ph},{ph},{ph},{ph},{ph})",
                (import_id, rn, cat, q, unit, product_id, product_name),
            )

            # log
            cur.execute(
                f"INSERT INTO logs(ts, store, username, action, category, name, delta) VALUES({_now()},{ph},{ph},{ph},{ph},{ph},{ph})",
                (store, user["username"], "IMPORT_FATTURA", cat, product_name, q),
            )

        # 5) elimina bozza
        cur.execute(
            f"DELETE FROM invoice_import_drafts WHERE id={ph} AND store={ph}",
            (int(draft_id), store),
        )

    return RedirectResponse("/inventario", status_code=HTTP_303_SEE_OTHER)


@app.get("/fatture/{doc_id}")
def invoices_download(request: Request, doc_id: int):
    user = require_login(request)
    if not user:
        return RedirectResponse("/", status_code=HTTP_303_SEE_OTHER)

    # Solo admin può aprire/visualizzare il file
    if not is_admin(request):
        return PlainTextResponse("Solo admin", status_code=403)

    store = _effective_store(request, user)
    ph = _ph()

    with connect() as conn:
        cur = conn.cursor()
        if store == "ALL":
            row = cur.execute(
                f"SELECT filename, content_type, data FROM invoices_docs WHERE id={ph}",
                (int(doc_id),),
            ).fetchone()
        else:
            row = cur.execute(
                f"SELECT filename, content_type, data FROM invoices_docs WHERE id={ph} AND store={ph}",
                (int(doc_id), store),
            ).fetchone()

    if not row:
        return PlainTextResponse("Not found", status_code=404)

    from fastapi.responses import Response
    headers = {"Content-Disposition": f"inline; filename=\"{row.get('filename') or 'fattura'}\""}
    return Response(content=row["data"], media_type=row.get("content_type") or "application/octet-stream", headers=headers)

@app.post("/fatture/{doc_id}/delete")
def invoices_delete(request: Request, doc_id: int):
    user = require_login(request)
    if not user:
        return RedirectResponse("/", status_code=HTTP_303_SEE_OTHER)

    store = _effective_store(request, user)
    ph = _ph()
    now = _now()

    with connect() as conn:
        cur = conn.cursor()

        # recupera info per log
        if store == "ALL" and is_admin(request):
            info = cur.execute(
                f"SELECT store, supplier, doc_date, filename FROM invoices_docs WHERE id={ph}",
                (int(doc_id),),
            ).fetchone()
        else:
            info = cur.execute(
                f"SELECT store, supplier, doc_date, filename FROM invoices_docs WHERE id={ph} AND store={ph}",
                (int(doc_id), store),
            ).fetchone()

        if info:
            if store == "ALL" and is_admin(request):
                cur.execute(f"DELETE FROM invoices_docs WHERE id={ph}", (int(doc_id),))
                store_for_log = info["store"]
            else:
                cur.execute(f"DELETE FROM invoices_docs WHERE id={ph} AND store={ph}", (int(doc_id), store))
                store_for_log = store

            # log: modulo fatture
            cur.execute(
                f"INSERT INTO logs(ts, store, username, action, category, name, delta) VALUES({now},{ph},{ph},{ph},{ph},{ph},{ph})",
                (store_for_log, user["username"], "DELETE", "FATTURE", f'{info.get("supplier") or ""} {info.get("doc_date") or ""}'.strip(), 0.0),
            )

    return RedirectResponse("/fatture", status_code=HTTP_303_SEE_OTHER)

