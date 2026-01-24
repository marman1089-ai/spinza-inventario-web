import os
import json
import csv
import io
from datetime import date

from fastapi import FastAPI, Request, Form, UploadFile, File
from .pdf_tools import ensure_pdf
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


# =========================
# LOGS PAGE (admin only)
# =========================
@app.get("/logs", response_class=HTMLResponse)
def logs_page(request: Request, limit: int = 200):
    user = require_login(request)
    if not user:
        return RedirectResponse("/", status_code=HTTP_303_SEE_OTHER)
    if not is_admin(request):
        return RedirectResponse("/inventario", status_code=HTTP_303_SEE_OTHER)

    ph = _ph()
    with connect() as conn:
        cur = conn.cursor()
        rows = cur.execute(
            f"SELECT * FROM logs ORDER BY id DESC LIMIT {ph}",
            (int(limit),),
        ).fetchall()

    brand = request.session.get("active_store") or "spinza"
    return render("logs.html", user=user, rows=rows, limit=limit, stores=STORES, brand=brand)


# =========================
# CHIUSURE (foto) & FATTURE (documenti)
# =========================
def _effective_store(request: Request, user: dict) -> str:
    if is_admin(request):
        s = request.session.get("active_store") or "spinza"
        if s == "ALL" or s not in STORES:
            s = "spinza"
        return s
    return user.get("store") or "spinza"


@app.get("/chiusure", response_class=HTMLResponse)
def closures_page(request: Request, q: str = ""):
    user = require_login(request)
    if not user:
        return RedirectResponse("/", status_code=HTTP_303_SEE_OTHER)

    store = _effective_store(request, user)
    brand = store

    q = (q or "").strip()
    ph = _ph()

    with connect() as conn:
        cur = conn.cursor()
        sql = f"SELECT id, store, closure_date, uploaded_by, ts, filename, content_type FROM closures WHERE store={ph}"
        params = [store]
        if q:
            # ricerca semplice: confronta stringa data
            sql += f" AND CAST(closure_date AS TEXT) LIKE {ph}"
            params.append(f"%{q}%")
        sql += " ORDER BY closure_date DESC, id DESC"
        rows = cur.execute(sql, tuple(params)).fetchall()

    return render("closures.html", user=user, rows=rows, q=q, stores=STORES, brand=brand)


@app.post("/chiusure/upload")
async def closures_upload(request: Request, closure_date: str = Form(...), file: UploadFile = File(...)):
    user = require_login(request)
    if not user:
        return RedirectResponse("/", status_code=HTTP_303_SEE_OTHER)

    store = _effective_store(request, user)
    raw = await file.read()
    filename = file.filename or "chiusura"
    content_type = file.content_type or "application/octet-stream"
    content, filename, content_type = ensure_pdf(raw, filename, content_type)

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

    return RedirectResponse("/chiusure", status_code=HTTP_303_SEE_OTHER)


@app.get("/chiusure/{doc_id}")
def closures_download(request: Request, doc_id: int):
    user = require_login(request)
    if not user:
        return RedirectResponse("/", status_code=HTTP_303_SEE_OTHER)
    store = _effective_store(request, user)

    ph = _ph()
    with connect() as conn:
        cur = conn.cursor()
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
    with connect() as conn:
        cur = conn.cursor()
        # elimina solo se appartiene allo store corrente
        cur.execute(f"DELETE FROM closures WHERE id={ph} AND store={ph}", (int(doc_id), store))
    return RedirectResponse("/chiusure", status_code=HTTP_303_SEE_OTHER)


@app.get("/fatture", response_class=HTMLResponse)
def invoices_page(request: Request, supplier: str = "", q_date: str = ""):
    user = require_login(request)
    if not user:
        return RedirectResponse("/", status_code=HTTP_303_SEE_OTHER)

    store = _effective_store(request, user)
    brand = store

    supplier = (supplier or "").strip()
    q_date = (q_date or "").strip()

    ph = _ph()
    with connect() as conn:
        cur = conn.cursor()
        sql = f"SELECT id, store, supplier, doc_date, uploaded_by, ts, filename, content_type FROM invoices_docs WHERE store={ph}"
        params = [store]
        if supplier:
            sql += f" AND lower(supplier) LIKE {ph}"
            params.append(f"%{supplier.lower()}%")
        if q_date:
            sql += f" AND CAST(doc_date AS TEXT) LIKE {ph}"
            params.append(f"%{q_date}%")
        sql += " ORDER BY doc_date DESC, id DESC"
        rows = cur.execute(sql, tuple(params)).fetchall()

    return render("invoices.html", user=user, rows=rows, supplier=supplier, q_date=q_date, stores=STORES, brand=brand)


@app.post("/fatture/upload")
async def invoices_upload(request: Request, supplier: str = Form(...), doc_date: str = Form(...), file: UploadFile = File(...)):
    user = require_login(request)
    if not user:
        return RedirectResponse("/", status_code=HTTP_303_SEE_OTHER)

    store = _effective_store(request, user)
    supplier = (supplier or "").strip()
    if not supplier:
        return RedirectResponse("/fatture", status_code=HTTP_303_SEE_OTHER)

    try:
        _ = date.fromisoformat(doc_date)
    except Exception:
        return RedirectResponse("/fatture", status_code=HTTP_303_SEE_OTHER)

    raw = await file.read()
    filename = file.filename or "fattura"
    content_type = file.content_type or "application/octet-stream"
    content, filename, content_type = ensure_pdf(raw, filename, content_type)

    ph = _ph()
    now = _now()
    with connect() as conn:
        cur = conn.cursor()
        cur.execute(
            f"INSERT INTO invoices_docs(store, supplier, doc_date, uploaded_by, ts, filename, content_type, data) VALUES({ph},{ph},{ph},{ph},{now},{ph},{ph},{ph})",
            (store, supplier, doc_date, user["username"], filename, content_type, content),
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
    store = _effective_store(request, user)

    ph = _ph()
    with connect() as conn:
        cur = conn.cursor()
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
    with connect() as conn:
        cur = conn.cursor()
        cur.execute(f"DELETE FROM invoices_docs WHERE id={ph} AND store={ph}", (int(doc_id), store))
    return RedirectResponse("/fatture", status_code=HTTP_303_SEE_OTHER)

