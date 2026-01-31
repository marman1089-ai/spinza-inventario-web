import os
import json
import csv
import io
from datetime import date

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
    return request.session.get("user")

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

def _admin_store(request: Request) -> str:
    s = (request.session.get("admin_store") or "spinza")
    return s if s in STORES else "spinza"

def _render_admin(request: Request, *, user, users, msg=None, error=None):
    admin_store = _admin_store(request)
    return render(
        "admin.html",
        user=user,
        users=users,
        msg=msg,
        error=error,
        stores=STORES,
        admin_store=admin_store,
        brand=admin_store,
    )

def _admin_users_render_error(request: Request, user, error_msg: str):
    with connect() as conn:
        cur = conn.cursor()
        users = cur.execute("SELECT id, store, username, role FROM users ORDER BY role DESC, store, username").fetchall()
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
        # dopo login l'utente deve scegliere la sezione (bibite / prodotti)
        if not get_selected_area(request):
            return RedirectResponse("/select-area", status_code=HTTP_303_SEE_OTHER)
        return RedirectResponse("/inventario", status_code=HTTP_303_SEE_OTHER)
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

    # reset area ad ogni login per forzare la scelta sala/cucina
    request.session.pop("selected_area", None)
    return RedirectResponse("/select-area", status_code=HTTP_303_SEE_OTHER)


@app.get("/select-area", response_class=HTMLResponse)
def select_area_get(request: Request, area: str = ""):
    user = require_login(request)
    if not user:
        return RedirectResponse("/", status_code=HTTP_303_SEE_OTHER)

    area = (area or "").strip().lower()
    if area in ("bibite", "prodotti"):
        set_selected_area(request, area)
        return RedirectResponse("/inventario", status_code=HTTP_303_SEE_OTHER)

    # brand: store scelto
    brand = (request.session.get("active_store") if is_admin(request) else user.get("store")) or "spinza"
    if brand not in STORES and brand != "ALL":
        brand = "spinza"
    return render("select_area.html", user=user, brand=brand)


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
            "SELECT id, store, username, role FROM users ORDER BY role DESC, store, username"
        ).fetchall()

    return _render_admin(request, user=user, users=users)

@app.post("/admin/users/add", response_class=HTMLResponse)
def admin_add_user(
    request: Request,
    username: str = Form(...),
    password: str = Form(...),
    confirm_password: str = Form(...),
):
    user = require_login(request)
    if not user:
        return RedirectResponse("/", status_code=HTTP_303_SEE_OTHER)
    if not is_admin(request):
        return RedirectResponse("/inventario", status_code=HTTP_303_SEE_OTHER)

    username = username.strip()
    if not username:
        return RedirectResponse("/admin", status_code=HTTP_303_SEE_OTHER)
    if password != confirm_password:
        return _admin_users_render_error(request, user, "Le password non coincidono.")
    if len(password) < 4:
        return _admin_users_render_error(request, user, "Password troppo corta.")

    store = (request.session.get("admin_store") or "spinza")
    if store not in STORES:
        store = "spinza"

    salt, h = make_password(password)
    ph = _ph()

    with connect() as conn:
        cur = conn.cursor()
        exists = cur.execute(
            f"SELECT 1 FROM users WHERE username={ph} AND store={ph}",
            (username, store),
        ).fetchone()
        if exists:
            users = cur.execute("SELECT id, store, username, role FROM users ORDER BY role DESC, store, username").fetchall()
            return _render_admin(request, user=user, users=users, error="Username già esistente.")

        cur.execute(
            f"INSERT INTO users(store, username, role, pw_salt, pw_hash, legacy_sha256) VALUES({ph},{ph},'staff',{ph},{ph},NULL)",
            (store, username, salt, h),
        )
        users = cur.execute("SELECT id, store, username, role FROM users ORDER BY role DESC, store, username").fetchall()

    return _render_admin(request, user=user, users=users, msg=f"Utente '{username}' creato per {STORES.get(store, store)}.")

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

        users = cur.execute("SELECT id, store, username, role FROM users ORDER BY role DESC, store, username").fetchall()

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
        users = cur.execute("SELECT id, store, username, role FROM users ORDER BY role DESC, store, username").fetchall()

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
        users = cur.execute("SELECT id, store, username, role FROM users ORDER BY role DESC, store, username").fetchall()

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
# INVENTORY
# =========================
@app.get("/inventario", response_class=HTMLResponse)
def inventario(request: Request, q: str = "", cat: str = "ALL", only_low: int = 0):
    user = require_login(request)
    if not user:
        return RedirectResponse("/", status_code=HTTP_303_SEE_OTHER)

    area = get_selected_area(request)
    if not area or area not in AREAS:
        return RedirectResponse("/select-area", status_code=HTTP_303_SEE_OTHER)

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
    ph = _ph()

    with connect() as conn:
        cur = conn.cursor()

        # --- categorie ---
        cats_sql = "SELECT DISTINCT category FROM products WHERE area=" + ph
        cats_params = []
        cats_params.append(area)
        if active_store != "ALL":
            cats_sql += f" AND store={ph}"
            cats_params.append(active_store)
        cats_sql += " ORDER BY category"

        cats = [r["category"] for r in cur.execute(cats_sql, tuple(cats_params)).fetchall()] if cats_params \
               else [r["category"] for r in cur.execute(cats_sql).fetchall()]

        # --- prodotti ---
        sql = "SELECT * FROM products WHERE 1=1"
        params = []

        sql += f" AND area={ph}"
        params.append(area)

        if active_store != "ALL":
            sql += f" AND store={ph}"
            params.append(active_store)

        if cat != "ALL":
            sql += f" AND category={ph}"
            params.append(cat)

        if q:
            sql += f" AND (lower(name) LIKE {ph} OR lower(category) LIKE {ph})"
            params.extend([f"%{q}%", f"%{q}%"])

        if only_low:
            sql += " AND qty <= min_qty"

        sql += " ORDER BY category, name"
        items = cur.execute(sql, tuple(params)).fetchall()

    return render(
        "inventario.html",
        user=user,
        area=area,
        areas=AREAS,
        items=items,
        cats=cats,
        q=q,
        cat=cat,
        only_low=only_low,
        admin=admin,
        stores=STORES,
        active_store=active_store,
        can_edit=(active_store != "ALL"),
        brand=active_store if active_store != "ALL" else "spinza",
    )


@app.post("/items/{item_id}/delta")
def item_delta(request: Request, item_id: int, delta: float = Form(...)):
    user = require_login(request)
    if not user:
        return RedirectResponse("/", status_code=HTTP_303_SEE_OTHER)

    admin = is_admin(request)
    active_store = (request.session.get("active_store") if admin else user.get("store"))
    if not active_store or active_store == "ALL":
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
            (int(item_id), active_store),
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
            (active_store, user["username"], "DELTA", row["category"], row["name"], float(delta)),
        )

    # dopo login deve scegliere la sezione (bibite / prodotti)
    request.session.pop("selected_area", None)
    return RedirectResponse("/select-area", status_code=HTTP_303_SEE_OTHER)

@app.post("/items/{item_id}/set")
def item_set(request: Request, item_id: int, qty: float = Form(...)):
    user = require_login(request)
    if not user:
        return RedirectResponse("/", status_code=HTTP_303_SEE_OTHER)

    admin = is_admin(request)
    active_store = (request.session.get("active_store") if admin else user.get("store"))
    if not active_store or active_store == "ALL":
        return RedirectResponse("/inventario", status_code=HTTP_303_SEE_OTHER)

    ph = _ph()
    now = _now()

    with connect() as conn:
        cur = conn.cursor()
        row = cur.execute(
            f"SELECT * FROM products WHERE id={ph} AND store={ph}",
            (int(item_id), active_store),
        ).fetchone()
        if not row:
            return RedirectResponse("/inventario", status_code=HTTP_303_SEE_OTHER)

        cur.execute(
            f"UPDATE products SET qty={ph}, updated_at={now} WHERE id={ph}",
            (float(qty), int(item_id)),
        )
        cur.execute(
            f"INSERT INTO logs(ts, store, username, action, category, name, delta) VALUES({now},{ph},{ph},{ph},{ph},{ph},{ph})",
            (active_store, user["username"], "SET", row["category"], row["name"], float(qty)),
        )

    return RedirectResponse("/inventario", status_code=HTTP_303_SEE_OTHER)

@app.post("/items/add")
def item_add(
    request: Request,
    category: str = Form(...),
    name: str = Form(...),
    qty: float = Form(0),
    min_qty: float = Form(0),
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

    with connect() as conn:
        cur = conn.cursor()
        cur.execute(
            f"""INSERT INTO products(store, category, name, area, qty, min_qty, updated_at)
                VALUES({ph},{ph},{ph},{ph},{ph},{ph},{now})
                ON CONFLICT(store, category, name)
                DO UPDATE SET area=excluded.area, qty=excluded.qty, min_qty=excluded.min_qty, updated_at={now}""",
            (active_store, category, name, area, float(qty), float(min_qty)),
        )
        cur.execute(
            f"INSERT INTO logs(ts, store, username, action, category, name, delta) VALUES({now},{ph},{ph},{ph},{ph},{ph},{ph})",
            (active_store, user["username"], "ADD", category, name, float(qty)),
        )

    return RedirectResponse("/inventario", status_code=HTTP_303_SEE_OTHER)

@app.post("/items/{item_id}/edit")
def item_edit(
    request: Request,
    item_id: int,
    category: str = Form(...),
    name: str = Form(...),
    min_qty: float = Form(0),
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

    with connect() as conn:
        cur = conn.cursor()
        row = cur.execute(
            f"SELECT * FROM products WHERE id={ph} AND store={ph}",
            (int(item_id), active_store),
        ).fetchone()
        if not row:
            return RedirectResponse("/inventario", status_code=HTTP_303_SEE_OTHER)

        cur.execute(
            f"UPDATE products SET category={ph}, name={ph}, min_qty={ph}, updated_at={now} WHERE id={ph}",
            (category, name, float(min_qty), int(item_id)),
        )
        cur.execute(
            f"INSERT INTO logs(ts, store, username, action, category, name, delta) VALUES({now},{ph},{ph},{ph},{ph},{ph},{ph})",
            (active_store, user["username"], "EDIT", category, name, float(min_qty)),
        )

    return RedirectResponse("/inventario", status_code=HTTP_303_SEE_OTHER)

@app.post("/items/{item_id}/delete")
def item_delete(request: Request, item_id: int):
    user = require_login(request)
    if not user:
        return RedirectResponse("/", status_code=HTTP_303_SEE_OTHER)

    admin = is_admin(request)
    active_store = (request.session.get("active_store") if admin else user.get("store"))
    if not active_store or active_store == "ALL":
        return RedirectResponse("/inventario", status_code=HTTP_303_SEE_OTHER)

    ph = _ph()
    now = _now()

    with connect() as conn:
        cur = conn.cursor()
        row = cur.execute(
            f"SELECT * FROM products WHERE id={ph} AND store={ph}",
            (int(item_id), active_store),
        ).fetchone()
        if row:
            cur.execute(f"DELETE FROM products WHERE id={ph}", (int(item_id),))
            cur.execute(
                f"INSERT INTO logs(ts, store, username, action, category, name, delta) VALUES({now},{ph},{ph},{ph},{ph},{ph},{ph})",
                (active_store, user["username"], "DELETE", row["category"], row["name"], 0.0),
            )

    return RedirectResponse("/inventario", status_code=HTTP_303_SEE_OTHER)


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
            f"SELECT category, name, qty, min_qty FROM products WHERE store={ph} ORDER BY category, name",
            (active_store,),
        ).fetchall()

    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["category", "name", "qty", "min_qty"])
    for r in rows:
        w.writerow([r["category"], r["name"], r["qty"], r["min_qty"]])

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
                f"""INSERT INTO products(store, category, name, area, qty, min_qty, updated_at)
                    VALUES({ph},{ph},{ph},{ph},{ph},{ph},{now})
                    ON CONFLICT(store, category, name)
                    DO UPDATE SET area=excluded.area, qty=excluded.qty, min_qty=excluded.min_qty, updated_at={now}""",
                (active_store, cat, name, area, qty, min_qty),
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
    order_cond = "(UPPER(action) LIKE 'ORDER_%' OR UPPER(category) IN ('ORDINI','ORDER'))"
    doc_cond = "(UPPER(action) LIKE 'DOC_%' OR UPPER(category) IN ('CHIUSURE','FATTURE','SPESE'))"
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

    with connect() as conn:
        cur = conn.cursor()
        rows = cur.execute(
            f"SELECT * FROM logs WHERE {where_sql} ORDER BY id DESC LIMIT {ph}",
            tuple(params + [int(limit)]),
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
def orders_add_from_inventory(request: Request, item_id: int = Form(...)):
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

    # torna all'inventario mantenendo query string se c'è
    return RedirectResponse("/inventario", status_code=HTTP_303_SEE_OTHER)


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
            rows = cur.execute(
                "SELECT id, store, category, name, qty_to_order, added_by, ts FROM order_queue ORDER BY store, category, name",
            ).fetchall()
        else:
            rows = cur.execute(
                f"SELECT id, store, category, name, qty_to_order, added_by, ts FROM order_queue WHERE store={ph} ORDER BY category, name",
                (store,),
            ).fetchall()

    active_store = request.session.get("active_store") if is_admin(request) else None
    return render("orders.html", user=user, rows=rows, stores=STORES, brand=brand, active_store=active_store)


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
                "SELECT id, store, supplier, status, created_by, ts FROM orders WHERE status='in_corso' ORDER BY ts DESC, id DESC",
            ).fetchall()
        else:
            os_ = cur.execute(
                f"SELECT id, store, supplier, status, created_by, ts FROM orders WHERE status='in_corso' AND store={ph} ORDER BY ts DESC, id DESC",
                (store,),
            ).fetchall()

        orders = []
        for o in os_:
            lines = cur.execute(
                f"SELECT category, name, qty FROM order_lines WHERE order_id={ph} ORDER BY category, name",
                (int(o["id"]),),
            ).fetchall()
            od = dict(o)
            od["lines"] = lines
            orders.append(od)

    active_store = request.session.get("active_store") if is_admin(request) else None
    return render("ordered.html", user=user, orders=orders, stores=STORES, brand=brand, active_store=active_store)


@app.post("/ordinati/arrivato")
def orders_mark_arrived(request: Request, order_id: int = Form(...)):
    """Pulsante 'Arrivato': elimina definitivamente l'ordine in corso."""
    user = require_login(request)
    if not user:
        return RedirectResponse("/", status_code=HTTP_303_SEE_OTHER)

    store = _effective_store(request, user)
    ph = _ph()

    with connect() as conn:
        cur = conn.cursor()

        if store == "ALL" and is_admin(request):
            info = cur.execute(
                f"SELECT store, supplier FROM orders WHERE id={ph} AND status='in_corso'",
                (int(order_id),),
            ).fetchone()
            if not info:
                return RedirectResponse("/ordinati", status_code=HTTP_303_SEE_OTHER)

            cur.execute(f"DELETE FROM order_lines WHERE order_id={ph}", (int(order_id),))
            cur.execute(f"DELETE FROM orders WHERE id={ph}", (int(order_id),))

            _log(
                cur,
                store=info["store"],
                username=user["username"],
                action="ORDER_ARRIVATO",
                category=info["supplier"],
                name=f"Ordine #{int(order_id)} arrivato",
                delta=0.0,
            )
        else:
            info = cur.execute(
                f"SELECT supplier FROM orders WHERE id={ph} AND status='in_corso' AND store={ph}",
                (int(order_id), store),
            ).fetchone()
            if not info:
                return RedirectResponse("/ordinati", status_code=HTTP_303_SEE_OTHER)

            cur.execute(f"DELETE FROM order_lines WHERE order_id={ph}", (int(order_id),))
            cur.execute(f"DELETE FROM orders WHERE id={ph} AND store={ph}", (int(order_id), store))

            _log(
                cur,
                store=store,
                username=user["username"],
                action="ORDER_ARRIVATO",
                category=info["supplier"],
                name=f"Ordine #{int(order_id)} arrivato",
                delta=0.0,
            )

    return RedirectResponse("/ordinati", status_code=HTTP_303_SEE_OTHER)



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


