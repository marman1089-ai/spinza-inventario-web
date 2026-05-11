import os
import datetime
import json
import csv
import io
from datetime import date, datetime, timedelta
import re
import sqlite3

from fastapi import FastAPI, Request, Form, UploadFile, File
from .pdf_tools import ensure_pdf, merge_pdfs
from fastapi.responses import RedirectResponse, HTMLResponse, PlainTextResponse
from fastapi.staticfiles import StaticFiles
from starlette.middleware.sessions import SessionMiddleware
from starlette.status import HTTP_303_SEE_OTHER
from jinja2 import Environment, FileSystemLoader, select_autoescape

from .db import connect, init_db, ensure_db_exists, using_postgres
from .security import verify_password, legacy_sha256, make_password
from .migrate_from_old import run_migration

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

    ensure_admin_user()
    print("[STARTUP] ensure_admin_user() completato")

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
    return STORES.get(store, store)


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


def _parse_flexible_amount(text: str):
    s = (text or '').replace(" ", ' ').replace('€', ' € ')
    matches = re.findall(r"([0-9]+(?:[\.,][0-9]{1,2})?)\s*€", s)
    if matches:
        candidate = matches[-1]
    else:
        eq_match = re.search(r"=\s*€?\s*([0-9]+(?:[\.,][0-9]{1,2})?)", s)
        if eq_match:
            candidate = eq_match.group(1)
        else:
            nums = re.findall(r"([0-9]+(?:[\.,][0-9]{1,2})?)", s)
            if not nums:
                return None
            candidate = nums[-1]
    try:
        return float(candidate.replace(',', '.'))
    except Exception:
        return None


def _normalize_import_payment_method(label: str):
    s = (label or '').strip().lower()
    s = re.sub(r"^[\-•⁃–—·*]+", '', s).strip()
    mapping = {
        'pos': 'pos',
        'cash': 'contanti',
        'contanti': 'contanti',
        'deliveroo': 'deliveroo',
        'glovo': 'glovo',
        'just eat': 'just eat',
        'justeat': 'just eat',
        'scuola': 'scuola',
        'satispay': 'satispay',
        'paypal': 'paypal',
    }
    for key, value in mapping.items():
        if s.startswith(key):
            return value
    return ''


def _parse_import_txt_blocks(raw_text: str, fallback_store: str = 'spinza'):
    text = (raw_text or '').replace('\r\n', '\n').replace('\r', '\n')
    lines = [ln.strip() for ln in text.split('\n')]
    current = None
    blocks = []
    store = fallback_store or 'spinza'

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

        upper_line = line.upper()
        if not current and ('SPINZA' in upper_line):
            store = 'spinza'
            continue
        if not current and ('CAMALDOLI' in upper_line):
            store = 'reburger_camaldoli'
            continue
        if not current and ('PALAZZUOLO' in upper_line):
            store = 'reburger_palazzuolo'
            continue

        m_date = re.search(r"(\d{1,2})/(\d{1,2})/(\d{2,4})", line)
        if m_date:
            close_current()
            dd, mm, yy = m_date.groups()
            year = int(yy)
            if year < 100:
                year += 2000
            try:
                iso = date(year, int(mm), int(dd)).isoformat()
            except Exception:
                continue
            current = {
                'store': store,
                'date': iso,
                'incomes': [],
                'expenses': [],
                'notes': [],
            }
            continue

        if not current:
            continue

        lower = line.lower()
        if lower.startswith('total sales') or lower.startswith('avg / day'):
            current['notes'].append(line)
            continue
        if lower.startswith('total '):
            continue
        if lower.startswith('fondo cassa'):
            current['notes'].append(line)
            continue

        amount = _parse_flexible_amount(line)
        if amount is None:
            current['notes'].append(line)
            continue

        label = re.split(r"[0-9]", line, 1)[0].strip(' :-–—')
        pay_method = _normalize_import_payment_method(label)
        if pay_method:
            current['incomes'].append({'payment_method': pay_method, 'amount': amount, 'raw': line})
            if '(' in line or ' x ' in lower:
                current['notes'].append(line)
        else:
            expense_name = label or line
            current['expenses'].append({'name': expense_name[:120], 'amount': amount, 'raw': line})

    close_current()
    return blocks

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
    expense_breakdown = _dict_rows(
        cur,
        f"SELECT LOWER(TRIM(category)) AS name, COALESCE(SUM(amount),0) AS total FROM cash_expenses WHERE {where_sql} AND flow_date BETWEEN {ph} AND {ph} GROUP BY LOWER(TRIM(category)) ORDER BY total DESC, name ASC",
        params + (start_s, end_s),
    )

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
    "Soft Drink",
    "Alcol Drink",
    "Desert",
    "Delivery",
]

SALES_REPORT_GROUP_ALIASES = {
    "pinze classiche": "Pinze classiche",
    "pinze speciali": "Pinze speciali",
    "pinze stagionali": "Pinze stagionali",
    "soft drink": "Soft Drink",
    "soft drinks": "Soft Drink",
    "alcol drink": "Alcol Drink",
    "alcol drinks": "Alcol Drink",
    "alcool drink": "Alcol Drink",
    "alcool drinks": "Alcol Drink",
    "desert": "Desert",
    "dessert": "Desert",
    "desert (dolci)": "Desert",
    "dolci": "Desert",
    "delivery": "Delivery",
}

SALES_REPORT_EXACT_GROUP_MAP = {
    "birra artigianale": "Alcol Drink",
    "vino naturale": "Alcol Drink",
    "stiramisu": "Desert",
    "diavola": "Pinze classiche",
    "diavola delivery": "Delivery",
    "margherita e bufala": "Pinze classiche",
    "marinara": "Pinze classiche",
    "nuda": "Pinze classiche",
    "buona cavolo": "Pinze speciali",
    "che cavolo": "Pinze speciali",
    "miele on tartufo": "Pinze speciali",
    "nordica": "Pinze speciali",
    "salsiccia 2.0": "Pinze speciali",
    "sunday porchetta": "Pinze speciali",
    "speckiosa": "Pinze speciali",
    "carciofo crunch": "Pinze stagionali",
    "fave & pears": "Pinze stagionali",
    "finocchio": "Pinze stagionali",
    "fragolina scandal": "Pinze stagionali",
    "smokd' asparagus": "Pinze stagionali",
    "wild porcini": "Pinze stagionali",
    "zucca blue": "Pinze stagionali",
    "acqua frizzante": "Soft Drink",
    "acqua naturale": "Soft Drink",
    "coca cola": "Soft Drink",
    "limonata": "Soft Drink",
}

SALES_REPORT_GROUP_KEYWORDS = {
    "Delivery": ["delivery", "deliveroo", "glovo", "just eat", "justeat"],
    "Alcol Drink": ["birra", "beer", "vino", "wine", "spritz", "cocktail", "gin", "prosecco", "campari", "aperol", "negroni", "amaro", "vermouth"],
    "Soft Drink": ["acqua", "coca", "cola", "fanta", "sprite", "limonata", "cedrata", "chinotto", "the ", "tè", "tea", "succo", "tonica", "soda", "soft drink"],
    "Desert": ["tiramisu", "tiramisù", "dessert", "desert", "dolce", "cheesecake", "brownie", "gelato", "sorbetto", "cookie", "cannolo", "babà", "baba", "panna cotta"],
}


def _sales_name_norm(value: str) -> str:
    return ' '.join((value or '').strip().lower().split())


def _canonical_sales_report_group_name(value: str) -> str:
    norm = _sales_name_norm(value)
    return SALES_REPORT_GROUP_ALIASES.get(norm, '')


def _guess_sales_report_group_for_name(name: str) -> str:
    norm = _sales_name_norm(name)
    if not norm:
        return ''
    if norm in SALES_REPORT_EXACT_GROUP_MAP:
        return SALES_REPORT_EXACT_GROUP_MAP[norm]
    for group_name, keywords in SALES_REPORT_GROUP_KEYWORDS.items():
        for keyword in keywords:
            if keyword and keyword in norm:
                return group_name
    return ''


def _ensure_sales_report_runtime_schema():
    """Assicura che le tabelle del report vendite esistano anche su DB vecchi o incompleti.

    Utile soprattutto su Render/Supabase quando il deploy aggiorna il codice ma lo
    schema non è ancora stato creato completamente.
    """
    ph = _ph()
    qty_col = "DOUBLE PRECISION" if using_postgres() else "REAL"
    ts_default = "TIMESTAMP DEFAULT now()" if using_postgres() else "TEXT DEFAULT (datetime('now'))"

    def _exec(cur, conn, sql: str):
        try:
            cur.execute(sql)
        except Exception:
            if using_postgres():
                try:
                    conn.rollback()
                except Exception:
                    pass
            raise

    with connect() as conn:
        cur = conn.cursor()
        _exec(cur, conn, f"""
            CREATE TABLE IF NOT EXISTS sales_report_periods (
                id {"SERIAL PRIMARY KEY" if using_postgres() else "INTEGER PRIMARY KEY AUTOINCREMENT"},
                store TEXT NOT NULL,
                month_key TEXT NOT NULL,
                label TEXT NOT NULL DEFAULT '',
                created_by TEXT NOT NULL,
                ts {ts_default}
            )
        """)
        _exec(cur, conn, f"""
            CREATE TABLE IF NOT EXISTS sales_report_groups (
                id {"SERIAL PRIMARY KEY" if using_postgres() else "INTEGER PRIMARY KEY AUTOINCREMENT"},
                period_id INTEGER NOT NULL,
                parent_id INTEGER,
                name TEXT NOT NULL,
                base_name TEXT NOT NULL DEFAULT '',
                amount {qty_col} NOT NULL DEFAULT 0,
                quantity {qty_col} NOT NULL DEFAULT 0,
                sort_order INTEGER NOT NULL DEFAULT 0,
                created_by TEXT NOT NULL,
                ts {ts_default}
            )
        """)
        _exec(cur, conn, f"""
            CREATE TABLE IF NOT EXISTS sales_report_name_rules (
                id {"SERIAL PRIMARY KEY" if using_postgres() else "INTEGER PRIMARY KEY AUTOINCREMENT"},
                store TEXT NOT NULL,
                source_name_norm TEXT NOT NULL,
                source_name TEXT NOT NULL DEFAULT '',
                target_group_name TEXT NOT NULL DEFAULT '',
                target_name TEXT NOT NULL DEFAULT '',
                is_deleted INTEGER NOT NULL DEFAULT 0,
                created_by TEXT NOT NULL DEFAULT 'system',
                ts {ts_default}
            )
        """)
        _exec(cur, conn, f"""
            CREATE TABLE IF NOT EXISTS sales_report_group_models (
                id {"SERIAL PRIMARY KEY" if using_postgres() else "INTEGER PRIMARY KEY AUTOINCREMENT"},
                store TEXT NOT NULL,
                name TEXT NOT NULL,
                name_norm TEXT NOT NULL DEFAULT '',
                sort_order INTEGER NOT NULL DEFAULT 0,
                is_active INTEGER NOT NULL DEFAULT 1,
                created_by TEXT NOT NULL DEFAULT 'system',
                ts {ts_default}
            )
        """)

        alters = [
            "ALTER TABLE sales_report_groups ADD COLUMN IF NOT EXISTS base_name TEXT NOT NULL DEFAULT ''" if using_postgres() else "ALTER TABLE sales_report_groups ADD COLUMN base_name TEXT NOT NULL DEFAULT ''",
            "ALTER TABLE sales_report_name_rules ADD COLUMN IF NOT EXISTS source_name TEXT NOT NULL DEFAULT ''" if using_postgres() else "ALTER TABLE sales_report_name_rules ADD COLUMN source_name TEXT NOT NULL DEFAULT ''",
            "ALTER TABLE sales_report_name_rules ADD COLUMN IF NOT EXISTS target_group_name TEXT NOT NULL DEFAULT ''" if using_postgres() else "ALTER TABLE sales_report_name_rules ADD COLUMN target_group_name TEXT NOT NULL DEFAULT ''",
            "ALTER TABLE sales_report_name_rules ADD COLUMN IF NOT EXISTS target_name TEXT NOT NULL DEFAULT ''" if using_postgres() else "ALTER TABLE sales_report_name_rules ADD COLUMN target_name TEXT NOT NULL DEFAULT ''",
            "ALTER TABLE sales_report_name_rules ADD COLUMN IF NOT EXISTS is_deleted INTEGER NOT NULL DEFAULT 0" if using_postgres() else "ALTER TABLE sales_report_name_rules ADD COLUMN is_deleted INTEGER NOT NULL DEFAULT 0",
            "ALTER TABLE sales_report_name_rules ADD COLUMN IF NOT EXISTS created_by TEXT NOT NULL DEFAULT 'system'" if using_postgres() else "ALTER TABLE sales_report_name_rules ADD COLUMN created_by TEXT NOT NULL DEFAULT 'system'",
        ]
        for stmt in alters:
            try:
                _exec(cur, conn, stmt)
            except Exception:
                pass

        for stmt in [
            "CREATE UNIQUE INDEX IF NOT EXISTS ux_sales_report_period_store_month ON sales_report_periods(store, month_key)",
            "CREATE UNIQUE INDEX IF NOT EXISTS ux_sales_report_name_rules_store_source ON sales_report_name_rules(store, source_name_norm)",
            "CREATE UNIQUE INDEX IF NOT EXISTS ux_sales_report_group_models_store_name ON sales_report_group_models(store, name_norm)",
        ]:
            try:
                _exec(cur, conn, stmt)
            except Exception:
                pass

        try:
            _exec(cur, conn, "UPDATE sales_report_groups SET base_name=name WHERE COALESCE(base_name,'')=''")
        except Exception:
            pass
        try:
            _exec(cur, conn, "UPDATE sales_report_group_models SET name_norm=LOWER(TRIM(name)) WHERE COALESCE(name_norm,'')=''")
        except Exception:
            pass


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
    ph = _ph()
    rows = _sales_report_model_groups(cur, store)
    canonical_by_norm = {_sales_name_norm(name): name for name in DEFAULT_SALES_REPORT_GROUPS}

    for row in rows:
        current_name = (row.get('name') or '').strip()
        current_norm = _sales_name_norm(current_name)
        canonical_name = _canonical_sales_report_group_name(current_name) or canonical_by_norm.get(current_norm, '')
        if canonical_name:
            wanted_sort = (DEFAULT_SALES_REPORT_GROUPS.index(canonical_name) + 1) * 10
            cur.execute(
                f"UPDATE sales_report_group_models SET name={ph}, name_norm={ph}, sort_order={ph}, is_active=1, created_by={ph} WHERE id={ph}",
                (canonical_name, _sales_name_norm(canonical_name), wanted_sort, username or 'system', int(row['id']))
            )
        else:
            cur.execute(f"UPDATE sales_report_group_models SET is_active=0 WHERE id={ph}", (int(row['id']),))

    for idx, label in enumerate(DEFAULT_SALES_REPORT_GROUPS, start=1):
        _upsert_sales_report_model_group(cur, store, label, username=username, sort_order=idx*10, is_active=1)
    return _sales_report_model_groups(cur, store)


def _ensure_sales_report_groups_from_models(cur, period_id: int, store: str, username: str = 'system'):
    ph = _ph()
    models = _ensure_sales_report_root_models(cur, store, username)
    existing = _dict_rows(cur, f"SELECT id, name, sort_order FROM sales_report_groups WHERE period_id={ph} AND parent_id IS NULL ORDER BY sort_order ASC, id ASC", (int(period_id),))
    for row in existing:
        canonical_name = _canonical_sales_report_group_name(row.get('name') or '')
        if not canonical_name:
            continue
        sort_order = (DEFAULT_SALES_REPORT_GROUPS.index(canonical_name) + 1) * 10 if canonical_name in DEFAULT_SALES_REPORT_GROUPS else int(row.get('sort_order') or 0)
        cur.execute(
            f"UPDATE sales_report_groups SET name={ph}, base_name=CASE WHEN COALESCE(base_name,'')='' THEN {ph} ELSE base_name END, sort_order={ph} WHERE id={ph}",
            (canonical_name, canonical_name, sort_order, int(row['id']))
        )
    existing = _dict_rows(cur, f"SELECT id, name FROM sales_report_groups WHERE period_id={ph} AND parent_id IS NULL ORDER BY sort_order ASC, id ASC", (int(period_id),))
    existing_norm = {_sales_name_norm(_canonical_sales_report_group_name(r.get('name') or '') or r.get('name') or '') for r in existing}
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
def gestionale_home(request: Request, period_type: str = 'week', anchor_date: str = ''):
    user = require_login(request)
    if not user:
        return RedirectResponse("/", status_code=HTTP_303_SEE_OTHER)

    brand = _current_store_scope(request, user)
    active_store = request.session.get("active_store") if is_admin(request) else None
    can_view_finance = can_view_management_finance(request, user)
    store = brand
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

        compare, totals, recent_entries, recent_expenses, period_meta, insights = _build_cash_dashboard(cur, store, period_type, anchor_date)

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
    )




@app.get("/gestionale/dashboard", response_class=HTMLResponse)
def gestionale_dashboard(request: Request, period_type: str = 'week', anchor_date: str = ''):
    user = require_login(request)
    if not user:
        return RedirectResponse("/", status_code=HTTP_303_SEE_OTHER)
    if not can_view_management_finance(request, user):
        return RedirectResponse("/gestionale", status_code=HTTP_303_SEE_OTHER)

    brand = _current_store_scope(request, user)
    active_store = request.session.get("active_store") if is_admin(request) else None
    with connect() as conn:
        cur = conn.cursor()
        compare, totals, recent_entries, recent_expenses, period_meta, insights = _build_cash_dashboard(cur, brand, period_type, anchor_date)
    return render(
        "cashflow_dashboard.html",
        user=user,
        brand=brand,
        active_store=active_store,
        compare=compare,
        totals=totals,
        period_meta=period_meta,
        recent_entries=recent_entries,
        recent_expenses=recent_expenses,
        stores=STORES,
        insights=insights,
    )


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

    _ensure_sales_report_runtime_schema()
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
        parent_options=parent_options,
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
    _ensure_sales_report_runtime_schema()
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
                    continue
                target_name = (rule.get('target_name') or '').strip() or name
                target_group_name = (rule.get('target_group_name') or '').strip()
                if not target_group_name:
                    target_group_name = _guess_sales_report_group_for_name(name)
                    if target_group_name:
                        _upsert_sales_report_rule(cur, store, name, user.get('username') or 'system', target_group_name=target_group_name, target_name=target_name, is_deleted=0)
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


@app.post("/gestionale/report-vendite/reset-all")
async def sales_report_reset_all(request: Request):
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
        _ensure_sales_report_root_models(cur, store, user.get('username') or 'system')
        cur.execute(f"DELETE FROM sales_report_groups WHERE period_id IN (SELECT id FROM sales_report_periods WHERE store={_ph()})", (store,))
        cur.execute(f"DELETE FROM sales_report_periods WHERE store={_ph()}", (store,))
        cur.execute(f"DELETE FROM sales_report_name_rules WHERE store={_ph()}", (store,))
        _log(cur, store=store, username=user['username'], action='DELETE', category='REPORT_VENDITE', name=f'Reset completo report vendite {store}', delta=0)
        conn.commit()

    return RedirectResponse(f'/gestionale/report-vendite?store={store}&month_key={month_key}', status_code=HTTP_303_SEE_OTHER)



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
    persist_model = str(form.get('persist_model') or '').strip() in ('1', 'true', 'on', 'yes')
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
        if store not in STORES:
            store = row.get('store') or 'spinza'
        if not month_key:
            month_key = row.get('month_key') or _today_str()[:7]
        if target_parent == group_id:
            target_parent = None

        descendants = {group_id}
        frontier = [group_id]
        while frontier:
            current = frontier.pop()
            children = _dict_rows(cur, f"SELECT id FROM sales_report_groups WHERE parent_id={ph}", (current,))
            for c in children:
                cid = int(c['id'])
                if cid not in descendants:
                    descendants.add(cid)
                    frontier.append(cid)
        if target_parent in descendants:
            target_parent = None
        if target_parent is not None:
            valid = _fetch_one_int(cur, f"SELECT COUNT(*) FROM sales_report_groups WHERE id={ph} AND period_id={ph}", (target_parent, period_id))
            if not valid:
                target_parent = None

        base_name = (row.get('base_name') or row.get('name') or '').strip()
        target_group_name = ''
        if target_parent is not None:
            target_row = cur.execute(f"SELECT id, name, parent_id FROM sales_report_groups WHERE id={ph}", (target_parent,)).fetchone()
            if target_row:
                target_group_name = (dict(target_row).get('name') or '').strip() if dict(target_row).get('parent_id') is None else ''
        is_leaf = _fetch_one_int(cur, f"SELECT COUNT(*) FROM sales_report_groups WHERE parent_id={ph}", (group_id,)) == 0
        if is_leaf and base_name:
            _upsert_sales_report_rule(cur, row.get('store') or 'spinza', base_name, user.get('username') or 'system', target_group_name=target_group_name)
            _sales_report_apply_rules_to_existing_rows(cur, row.get('store') or 'spinza', base_name, user.get('username') or 'system')
        else:
            was_root = row.get('parent_id') is None
            cur.execute(f"UPDATE sales_report_groups SET parent_id={ph} WHERE id={ph}", (target_parent, group_id))
            if was_root and target_parent is not None:
                _delete_sales_report_model_group(cur, row.get('store') or 'spinza', row.get('name') or '')
            elif (not was_root) and target_parent is None:
                next_sort_model = _fetch_one_int(cur, f"SELECT COALESCE(MAX(sort_order),0) FROM sales_report_group_models WHERE store={ph}", (row.get('store') or 'spinza',)) + 10
                _upsert_sales_report_model_group(cur, row.get('store') or 'spinza', row.get('name') or '', username=user.get('username') or 'system', sort_order=next_sort_model, is_active=1)
        _log(cur, store=row.get('store') or 'spinza', username=user['username'], action='UPDATE', category='REPORT_VENDITE', name=f"Spostata voce report {row.get('name','')}", delta=0)
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
            f"SELECT g.id, g.name, g.base_name, p.store, p.month_key FROM sales_report_groups g JOIN sales_report_periods p ON p.id=g.period_id WHERE g.id={ph}",
            (group_id,),
        ).fetchone()
        if not row:
            return RedirectResponse("/gestionale/report-vendite", status_code=HTTP_303_SEE_OTHER)
        row = dict(row)
        if brand != 'ALL' and row.get('store') != brand:
            return RedirectResponse("/gestionale/report-vendite", status_code=HTTP_303_SEE_OTHER)
        base_name = (row.get('base_name') or row.get('name') or '').strip()
        is_leaf = _fetch_one_int(cur, f"SELECT COUNT(*) FROM sales_report_groups WHERE parent_id={ph}", (group_id,)) == 0
        parent_row = cur.execute(f"SELECT parent_id FROM sales_report_groups WHERE id={ph}", (group_id,)).fetchone()
        is_root = (dict(parent_row).get('parent_id') if parent_row else None) is None
        if is_leaf and base_name:
            rule = _sales_report_rule_for_name(cur, row.get('store') or 'spinza', base_name) or {}
            _upsert_sales_report_rule(cur, row.get('store') or 'spinza', base_name, user.get('username') or 'system', target_group_name=rule.get('target_group_name') or '', target_name=new_name, is_deleted=0)
            _sales_report_apply_rules_to_existing_rows(cur, row.get('store') or 'spinza', base_name, user.get('username') or 'system')
        elif is_root:
            cur.execute(f"UPDATE sales_report_groups SET name={ph}, base_name=CASE WHEN COALESCE(base_name,'')='' THEN {ph} ELSE base_name END WHERE period_id IN (SELECT id FROM sales_report_periods WHERE store={ph}) AND parent_id IS NULL AND LOWER(TRIM(name))={ph}", (new_name, new_name, row.get('store') or 'spinza', _sales_name_norm(row.get('name') or '')))
            model_row = cur.execute(f"SELECT sort_order FROM sales_report_group_models WHERE store={ph} AND name_norm={ph}", (row.get('store') or 'spinza', _sales_name_norm(row.get('name') or ''))).fetchone()
            sort_order = int((dict(model_row).get('sort_order') if model_row else 0) or 0) if model_row else None
            _delete_sales_report_model_group(cur, row.get('store') or 'spinza', row.get('name') or '')
            _upsert_sales_report_model_group(cur, row.get('store') or 'spinza', new_name, username=user.get('username') or 'system', sort_order=sort_order, is_active=1)
        else:
            cur.execute(f"UPDATE sales_report_groups SET name={ph} WHERE id={ph}", (new_name, group_id))
        _log(cur, store=row.get('store') or 'spinza', username=user['username'], action='UPDATE', category='REPORT_VENDITE', name=f"Rinominata voce report {row.get('name','')} -> {new_name}", delta=0)
        conn.commit()
        return RedirectResponse(f"/gestionale/report-vendite?store={row.get('store')}&month_key={row.get('month_key')}", status_code=HTTP_303_SEE_OTHER)


@app.post("/gestionale/report-vendite/voce/{group_id}/delete")
def sales_report_delete_group(request: Request, group_id: int):
    user = require_login(request)
    if not user:
        return RedirectResponse("/", status_code=HTTP_303_SEE_OTHER)
    if not can_view_management_finance(request, user):
        return RedirectResponse("/gestionale", status_code=HTTP_303_SEE_OTHER)

    brand = _current_store_scope(request, user)
    ph = _ph()
    with connect() as conn:
        cur = conn.cursor()
        row = cur.execute(
            f"SELECT g.id, g.name, g.amount, p.store, p.month_key FROM sales_report_groups g JOIN sales_report_periods p ON p.id=g.period_id WHERE g.id={ph}",
            (group_id,),
        ).fetchone()
        if not row:
            return RedirectResponse("/gestionale/report-vendite", status_code=HTTP_303_SEE_OTHER)
        row = dict(row)
        if brand != 'ALL' and row.get('store') != brand:
            return RedirectResponse("/gestionale/report-vendite", status_code=HTTP_303_SEE_OTHER)
        is_leaf = _fetch_one_int(cur, f"SELECT COUNT(*) FROM sales_report_groups WHERE parent_id={ph}", (group_id,)) == 0
        parent_row = cur.execute(f"SELECT parent_id FROM sales_report_groups WHERE id={ph}", (group_id,)).fetchone()
        is_root = (dict(parent_row).get('parent_id') if parent_row else None) is None
        base_row = cur.execute(f"SELECT base_name FROM sales_report_groups WHERE id={ph}", (group_id,)).fetchone()
        base_name = ((dict(base_row).get('base_name') if base_row else '') or row.get('name') or '').strip()
        if is_leaf and base_name:
            rule = _sales_report_rule_for_name(cur, row.get('store') or 'spinza', base_name) or {}
            _upsert_sales_report_rule(cur, row.get('store') or 'spinza', base_name, user.get('username') or 'system', target_group_name=rule.get('target_group_name') or '', target_name=rule.get('target_name') or '', is_deleted=1)
            _sales_report_apply_rules_to_existing_rows(cur, row.get('store') or 'spinza', base_name, user.get('username') or 'system')
        else:
            if is_root:
                _delete_sales_report_model_group(cur, row.get('store') or 'spinza', row.get('name') or '')
                period_ids = _dict_rows(cur, f"SELECT id FROM sales_report_periods WHERE store={ph}", (row.get('store') or 'spinza',))
                for pr in period_ids:
                    rid = cur.execute(f"SELECT id FROM sales_report_groups WHERE period_id={ph} AND parent_id IS NULL AND LOWER(TRIM(name))={ph}", (int(pr['id']), _sales_name_norm(row.get('name') or ''))).fetchone()
                    if not rid:
                        continue
                    gid2 = int(dict(rid).get('id'))
                    ids = [gid2]
                    cursor = 0
                    while cursor < len(ids):
                        children = _dict_rows(cur, f"SELECT id FROM sales_report_groups WHERE parent_id={ph}", (ids[cursor],))
                        ids.extend(int(c['id']) for c in children if int(c['id']) not in ids)
                        cursor += 1
                    placeholders = ','.join([ph] * len(ids))
                    cur.execute(f"DELETE FROM sales_report_groups WHERE id IN ({placeholders})", tuple(ids))
            ids = [group_id]
            cursor = 0
            while cursor < len(ids):
                children = _dict_rows(cur, f"SELECT id FROM sales_report_groups WHERE parent_id={ph}", (ids[cursor],))
                ids.extend(int(c['id']) for c in children if int(c['id']) not in ids)
                cursor += 1
            placeholders = ','.join([ph] * len(ids))
            cur.execute(f"DELETE FROM sales_report_groups WHERE id IN ({placeholders})", tuple(ids))
        _log(cur, store=row.get('store') or 'spinza', username=user['username'], action='DELETE', category='REPORT_VENDITE', name=f"Report vendite eliminato {row.get('month_key','')} - {row.get('name','')}", delta=float(row.get('amount') or 0))
        conn.commit()
        return RedirectResponse(f"/gestionale/report-vendite?store={row.get('store')}&month_key={row.get('month_key')}", status_code=HTTP_303_SEE_OTHER)



@app.get("/gestionale/incassi", response_class=HTMLResponse)
def cash_entries_page(request: Request, flow_date: str = '', payment_method: str = 'ALL', period_type: str = 'week', anchor_date: str = '', imported_entries: int = 0, imported_expenses: int = 0, import_error: str = ''):
    user = require_login(request)
    if not user:
        return RedirectResponse("/", status_code=HTTP_303_SEE_OTHER)
    if not can_view_management_finance(request, user):
        return RedirectResponse("/gestionale", status_code=HTTP_303_SEE_OTHER)
    brand = _current_store_scope(request, user)
    active_store = request.session.get("active_store") if is_admin(request) else None
    can_edit_management = user.get('role') in ('admin', 'manager', 'staff')
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
    with connect() as conn:
        cur = conn.cursor()
        rows = _dict_rows(cur, sql, tuple(qparams))
        payments = [r['payment_method'] for r in _dict_rows(cur, f"SELECT DISTINCT payment_method FROM cash_entries WHERE {where_sql} ORDER BY payment_method ASC", params) if r.get('payment_method')]
        available_payment_methods = _load_cash_payment_methods(cur)
        history_methods, grouped_rows = _group_cash_rows_by_date(rows, available_payment_methods)
        chart_rows = _build_store_period_chart(cur, brand if brand != 'ALL' else (active_store or 'spinza'), period_type, anchor_date)
        total_amount = sum(float(r.get('amount') or 0) for r in rows)
    return render(
        "cashflow_entries.html",
        user=user, brand=brand, active_store=active_store, stores=STORES,
        can_edit_management=can_edit_management, rows=rows, today=date.today().isoformat(),
        selected_date=flow_date, selected_payment=payment_method, payments=payments,
        total_amount=total_amount, chart_rows=chart_rows, chart_store=(brand if brand != 'ALL' else (active_store or 'spinza')), imported_entries=imported_entries, imported_expenses=imported_expenses, import_error=import_error,
        available_payment_methods=available_payment_methods, history_methods=history_methods, grouped_rows=grouped_rows, selected_period_type=period_type, selected_anchor_date=(anchor_date or date.today().isoformat()),
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


@app.post("/gestionale/incassi/import-txt")
async def cash_entries_import_txt(request: Request):
    require_login(request)
    return RedirectResponse('/gestionale/incassi?import_error=Import+TXT+disattivato', status_code=HTTP_303_SEE_OTHER)


@app.get("/gestionale/uscite", response_class=HTMLResponse)
def cash_expenses_page(request: Request, flow_date: str = "", category: str = "ALL"):
    user = require_login(request)
    if not user:
        return RedirectResponse("/", status_code=HTTP_303_SEE_OTHER)
    if not can_view_management_finance(request, user):
        return RedirectResponse("/gestionale", status_code=HTTP_303_SEE_OTHER)
    brand = _current_store_scope(request, user)
    active_store = request.session.get("active_store") if is_admin(request) else None
    can_edit_management = user.get('role') in ('admin', 'manager', 'staff')
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
    with connect() as conn:
        cur = conn.cursor()
        rows = _dict_rows(cur, sql, tuple(qparams))
        categories = [r['category'] for r in _dict_rows(cur, f"SELECT DISTINCT category FROM cash_expenses WHERE {where_sql} ORDER BY category ASC", params) if r.get('category')]
        chart_rows = _build_store_period_chart(cur, brand if brand != 'ALL' else (active_store or 'spinza'))
        total_amount = sum(float(r.get('amount') or 0) for r in rows)
    return render(
        "cashflow_expenses.html",
        user=user, brand=brand, active_store=active_store, stores=STORES,
        can_edit_management=can_edit_management, rows=rows, today=date.today().isoformat(),
        selected_date=flow_date, selected_category=category, categories=categories,
        total_amount=total_amount, chart_rows=chart_rows, chart_store=(brand if brand != 'ALL' else (active_store or 'spinza')),
    )


@app.post("/gestionale/uscite")
def cash_expenses_create(
    request: Request,
    flow_date: str = Form(...),
    store: str = Form(...),
    category: str = Form(...),
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
    ph = _ph()
    with connect() as conn:
        cur = conn.cursor()
        cur.execute(
            f"INSERT INTO cash_expenses(store, flow_date, category, supplier, payment_method, amount, notes, created_by) VALUES({ph},{ph},{ph},{ph},{ph},{ph},{ph},{ph})",
            (store, flow_date, (category or '').strip(), (supplier or '').strip(), (payment_method or '').strip(), amount_value, (notes or '').strip(), user['username']),
        )
        _log(cur, store=store, username=user['username'], action='CREATE', category='CASSA', name=f"Uscita {flow_date} - {(category or '').strip()}", delta=-amount_value)
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
        # prendi unit non vuota se esiste
        for rr in lst_sorted:
            if (rr.get("unit") or "").strip():
                unit = (rr.get("unit") or "").strip()
                break
        positions = []
        total = 0.0
        any_low = False
        for rr in lst_sorted:
            qty = float(rr.get("qty") or 0)
            mn = float(rr.get("min_qty") or 0)
            low = qty <= mn
            total += qty
            any_low = any_low or low
            positions.append({
                "id": rr.get("id"),
                "store": rr.get("store"),
                "category": rr.get("category"),
                "name": rr.get("name"),
                "area": rr.get("area") or area_k,
                "location": (rr.get("location") or "").strip() or "MAGAZZINO",
                "unit": unit,
                "qty": qty,
                "min_qty": mn,
                "low": low,
                "missing_qty": float(rr.get("missing_qty") or 0),
                "missing_order_date": rr.get("missing_order_date"),
                "missing_delivery_date": rr.get("missing_delivery_date"),
            })
        groups.append({
            "store": store_k,
            "area": area_k,
            "category": cat_k,
            "name": name_k,
            "unit": unit,
            "positions": positions,
            "total": total,
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

        new_qty = float(row["qty"]) + float(delta)
        if new_qty < 0:
            new_qty = 0.0

        cur.execute(
            f"UPDATE products SET qty={ph}, updated_at={now} WHERE id={ph}",
            (new_qty, int(item_id)),
        )
        cur.execute(
            f"INSERT INTO logs(ts, store, username, action, category, name, delta) VALUES({now},{ph},{ph},{ph},{ph},{ph},{ph})",
            (effective_store, user["username"], "DELTA", row["category"], row["name"], float(delta)),
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

        cur.execute(
            f"UPDATE products SET qty={ph}, updated_at={now} WHERE id={ph}",
            (float(qty), int(item_id)),
        )
        cur.execute(
            f"INSERT INTO logs(ts, store, username, action, category, name, delta) VALUES({now},{ph},{ph},{ph},{ph},{ph},{ph})",
            (effective_store, user["username"], "SET", row["category"], row["name"], float(qty)),
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

    with connect() as conn:
        cur = conn.cursor()
        cur.execute(
            f"""INSERT INTO products(store, category, name, area, location, unit, qty, min_qty, updated_at)
                VALUES({ph},{ph},{ph},{ph},{ph},{ph},{ph},{ph},{now})
                ON CONFLICT(store, area, category, name, location)
                DO UPDATE SET unit=excluded.unit, qty=excluded.qty, min_qty=excluded.min_qty, updated_at={now}""",
            (active_store, category, name, area, location, unit, float(qty), float(min_qty)),
        )
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

        cur.execute(
            f"UPDATE products SET category={ph}, name={ph}, location={ph}, unit={ph}, min_qty={ph}, updated_at={now} WHERE id={ph}",
            (category, name, location, unit, float(min_qty), int(item_id)),
        )
        cur.execute(
            f"INSERT INTO logs(ts, store, username, action, category, name, delta) VALUES({now},{ph},{ph},{ph},{ph},{ph},{ph})",
            (effective_store, user["username"], "EDIT", category, name, float(min_qty)),
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
            cur.execute(f"DELETE FROM products WHERE id={ph}", (int(item_id),))
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

