import os
import csv
import io

from fastapi import FastAPI, Request, Form, UploadFile, File
from fastapi.responses import RedirectResponse, HTMLResponse, PlainTextResponse
from fastapi.staticfiles import StaticFiles
from starlette.middleware.sessions import SessionMiddleware
from starlette.status import HTTP_303_SEE_OTHER
from jinja2 import Environment, FileSystemLoader, select_autoescape

from .db import connect, init_db, ensure_db_exists
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
# SESSION HELPERS
# =========================
def require_login(request: Request):
    return request.session.get("user")

def get_selected_store(request: Request):
    return request.session.get("selected_store")

def set_selected_store(request: Request, store: str):
    request.session["selected_store"] = store

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


# =========================
# ADMIN BOOTSTRAP (NO SHELL NEEDED)
# =========================
def ensure_admin_user():
    """
    Crea (se manca) l'admin con credenziali fisse o da ENV.
    - Default richiesto: admin / spinza2025
    - Se vuoi cambiare senza modificare codice, usa ENV:
        ADMIN_USERNAME, ADMIN_PASSWORD
    - Se vuoi forzare reset password, usa:
        RESET_ADMIN=1
      (poi rimettilo a 0 o eliminalo)
    """
    admin_user = os.environ.get("ADMIN_USERNAME", "admin").strip()
    admin_pass = os.environ.get("ADMIN_PASSWORD", "spinza2025").strip()
    reset = os.environ.get("RESET_ADMIN", "0").strip() == "1"

    if not admin_user or not admin_pass:
        return

    salt, h = make_password(admin_pass)

    with connect() as conn:
        cur = conn.cursor()
        row = cur.execute(
            "SELECT id FROM users WHERE username=? AND role='admin'",
            (admin_user,),
        ).fetchone()

        if row:
            if reset:
                cur.execute(
                    "UPDATE users SET pw_salt=?, pw_hash=?, legacy_sha256=NULL, store=?, role='admin' WHERE id=?",
                    (salt, h, "spinza", row["id"]),
                )
        else:
            cur.execute(
                "INSERT INTO users(store, username, role, pw_salt, pw_hash, legacy_sha256) VALUES(?,?,?,?,?,NULL)",
                ("spinza", admin_user, "admin", salt, h),
            )


# =========================
# STARTUP
# =========================
@app.on_event("startup")
def _startup():
    import os
    from .db import init_db, ensure_db_exists, using_postgres, connect
    from .security import make_password

    print("====================================")
    print("[STARTUP] Applicazione avviata")

    db_url = os.getenv("DATABASE_URL")
    print("[STARTUP] DATABASE_URL presente:", bool(db_url))
    print("[STARTUP] DB TYPE:", "POSTGRES (Supabase)" if using_postgres() else "SQLITE (ATTENZIONE)")

    ensure_db_exists()
    init_db()
    print("[STARTUP] init_db() completato")

    # === ADMIN FISSO (sempre disponibile) ===
    admin_username = os.getenv("ADMIN_USERNAME", "admin")
    admin_password = os.getenv("ADMIN_PASSWORD", "spinza2025")

    with connect() as conn:
        cur = conn.cursor()

        if using_postgres():
            row = cur.execute(
                "SELECT id FROM users WHERE username=%s AND role='admin'",
                (admin_username,),
            ).fetchone()
        else:
            row = cur.execute(
                "SELECT id FROM users WHERE username=? AND role='admin'",
                (admin_username,),
            ).fetchone()

        if not row:
            salt, h = make_password(admin_password)
            if using_postgres():
                cur.execute(
                    "INSERT INTO users(store, username, role, pw_salt, pw_hash) VALUES(%s,%s,'admin',%s,%s)",
                    ("spinza", admin_username, salt, h),
                )
            else:
                cur.execute(
                    "INSERT INTO users(store, username, role, pw_salt, pw_hash) VALUES(?,?,'admin',?,?)",
                    ("spinza", admin_username, salt, h),
                )
            print(f"[STARTUP] Admin '{admin_username}' CREATO")
        else:
            print(f"[STARTUP] Admin '{admin_username}' già esistente")

    # Migrazione opzionale (se la usi)
    if os.environ.get("MIGRATE_ON_START") == "1":
        data_dir = os.environ.get("OLD_DATA_DIR", ".")
        run_migration(data_dir)

    print("====================================")


# =========================
# ROUTES: HOME / STORE / AUTH
# =========================
@app.get("/", response_class=HTMLResponse)
def home(request: Request):
    if require_login(request):
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

    with connect() as conn:
        cur = conn.cursor()
        row = cur.execute(
            "SELECT * FROM users WHERE username = ? AND store = ?",
            (username, store)
        ).fetchone()

        if not row:
            return render("login.html", error="Credenziali non valide.", store=store, store_label=STORES[store], brand=store)

        ok = False
        if row["pw_salt"] and row["pw_hash"]:
            ok = verify_password(password, row["pw_salt"], row["pw_hash"])
        elif row["legacy_sha256"]:
            ok = (legacy_sha256(password) == row["legacy_sha256"])
            if ok:
                salt, h = make_password(password)
                cur.execute(
                    "UPDATE users SET pw_salt=?, pw_hash=?, legacy_sha256=NULL WHERE id=?",
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

    return RedirectResponse("/inventario", status_code=HTTP_303_SEE_OTHER)


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

    with connect() as conn:
        cur = conn.cursor()
        exists = cur.execute(
            "SELECT 1 FROM users WHERE username = ? AND store = ?",
            (username, store)
        ).fetchone()
        if exists:
            return render("register.html", error="Username già esistente.", ok=False, user=None, store=store, store_label=STORES[store], brand=store)

        cur.execute(
            "INSERT INTO users(store, username, role, pw_salt, pw_hash, legacy_sha256) VALUES(?,?,?,?,?,NULL)",
            (store, username, "staff", salt, h),
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

    with connect() as conn:
        cur = conn.cursor()
        row = cur.execute(
            "SELECT * FROM users WHERE username = ? AND role='admin'",
            (username,),
        ).fetchone()

        if not row:
            return render("admin_login.html", error="Credenziali admin non valide.", user=None)

        ok = False
        if row["pw_salt"] and row["pw_hash"]:
            ok = verify_password(password, row["pw_salt"], row["pw_hash"])
        elif row["legacy_sha256"]:
            ok = (legacy_sha256(password) == row["legacy_sha256"])
            if ok:
                salt, h = make_password(password)
                cur.execute(
                    "UPDATE users SET pw_salt=?, pw_hash=?, legacy_sha256=NULL WHERE id=?",
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

    with connect() as conn:
        cur = conn.cursor()
        exists = cur.execute(
            "SELECT 1 FROM users WHERE username=? AND store=?",
            (username, store)
        ).fetchone()
        if exists:
            users = cur.execute("SELECT id, store, username, role FROM users ORDER BY role DESC, store, username").fetchall()
            return _render_admin(request, user=user, users=users, error="Username già esistente.")

        cur.execute(
            "INSERT INTO users(store, username, role, pw_salt, pw_hash, legacy_sha256) VALUES(?,?,?,?,?,NULL)",
            (store, username, "staff", salt, h),
        )
        users = cur.execute("SELECT id, store, username, role FROM users ORDER BY role DESC, store, username").fetchall()

    return _render_admin(request, user=user, users=users, msg=f"Utente '{username}' creato per {STORES.get(store, store)}.")


def _admin_users_render_error(request: Request, user, error_msg: str):
    with connect() as conn:
        cur = conn.cursor()
        users = cur.execute("SELECT id, store, username, role FROM users ORDER BY role DESC, store, username").fetchall()
    return _render_admin(request, user=user, users=users, error=error_msg)


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

    with connect() as conn:
        cur = conn.cursor()
        target = cur.execute("SELECT id, store, username, role FROM users WHERE id=?", (int(user_id),)).fetchone()
        if not target:
            return _admin_users_render_error(request, user, "Utente non trovato.")
        if target["role"] == "admin":
            return _admin_users_render_error(request, user, "Lo username admin si cambia dal Profilo.")

        exists = cur.execute(
            "SELECT 1 FROM users WHERE store=? AND username=? AND id<>?",
            (target["store"], new_username, int(user_id)),
        ).fetchone()
        if exists:
            return _admin_users_render_error(request, user, "Username già esistente in questo negozio.")

        cur.execute("UPDATE users SET username=? WHERE id=?", (new_username, int(user_id)))

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

    with connect() as conn:
        cur = conn.cursor()
        target = cur.execute("SELECT id, store, username, role FROM users WHERE id=?", (int(user_id),)).fetchone()
        if not target:
            return _admin_users_render_error(request, user, "Utente non trovato.")

        # admin può cambiare anche la propria password
        cur.execute(
            "UPDATE users SET pw_salt=?, pw_hash=?, legacy_sha256=NULL WHERE id=?",
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
    with connect() as conn:
        cur = conn.cursor()
        target = cur.execute("SELECT id, store, username, role FROM users WHERE id=?", (int(user_id),)).fetchone()
        if target and target["role"] != "admin":
            cur.execute("DELETE FROM users WHERE id=?", (int(user_id),))
            msg = f"Utente '{target['username']}' eliminato."
        users = cur.execute("SELECT id, store, username, role FROM users ORDER BY role DESC, store, username").fetchall()

    return _render_admin(request, user=user, users=users, msg=msg)


@app.get("/logout")
def logout(request: Request):
    request.session.clear()
    return RedirectResponse("/", status_code=HTTP_303_SEE_OTHER)


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

    with connect() as conn:
        cur = conn.cursor()
        # Admin by id, staff by store+username
        if user.get("role") == "admin" and user.get("id"):
            row = cur.execute("SELECT * FROM users WHERE id=?", (int(user["id"]),)).fetchone()
        else:
            row = cur.execute(
                "SELECT * FROM users WHERE username=? AND store=?",
                (user["username"], user.get("store")),
            ).fetchone()

        if not row:
            request.session.clear()
            return RedirectResponse("/", status_code=HTTP_303_SEE_OTHER)

        ok = False
        if row["pw_salt"] and row["pw_hash"]:
            ok = verify_password(current_password, row["pw_salt"], row["pw_hash"])
        elif row["legacy_sha256"]:
            ok = (legacy_sha256(current_password) == row["legacy_sha256"])

        if not ok:
            return render("profile.html", user=user, msg=None, error="Password attuale errata.", brand=brand)

        salt, h = make_password(new_password)
        cur.execute(
            "UPDATE users SET pw_salt=?, pw_hash=?, legacy_sha256=NULL WHERE id=?",
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

    with connect() as conn:
        cur = conn.cursor()
        exists = cur.execute(
            "SELECT 1 FROM users WHERE role='admin' AND username=? AND id<>?",
            (new_username, int(user.get("id", -1))),
        ).fetchone()
        if exists:
            return render("profile.html", user=user, msg=None, error="Username admin già esistente.", brand=brand)

        cur.execute("UPDATE users SET username=? WHERE id=?", (new_username, int(user.get("id"))))

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

    with connect() as conn:
        cur = conn.cursor()

        cats_sql = "SELECT DISTINCT category FROM products"
        cats_params = []
        if active_store != "ALL":
            cats_sql += " WHERE store = ?"
            cats_params.append(active_store)
        cats_sql += " ORDER BY category"
        cats = [r["category"] for r in cur.execute(cats_sql, cats_params).fetchall()]

        sql = "SELECT * FROM products WHERE 1=1"
        params = []
        if active_store != "ALL":
            sql += " AND store = ?"
            params.append(active_store)
        if cat != "ALL":
            sql += " AND category = ?"
            params.append(cat)
        if q:
            sql += " AND (lower(name) LIKE ? OR lower(category) LIKE ?)"
            params.extend([f"%{q}%", f"%{q}%"])
        if only_low:
            sql += " AND qty <= min_qty"
        sql += " ORDER BY category, name"

        items = cur.execute(sql, params).fetchall()

    return render(
        "inventario.html",
        user=user,
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

    with connect() as conn:
        cur = conn.cursor()
        row = cur.execute("SELECT * FROM products WHERE id=? AND store=?", (int(item_id), active_store)).fetchone()
        if not row:
            return RedirectResponse("/inventario", status_code=HTTP_303_SEE_OTHER)

        new_qty = float(row["qty"]) + float(delta)
        if new_qty < 0:
            new_qty = 0.0

        cur.execute("UPDATE products SET qty=? WHERE id=?", (new_qty, int(item_id)))
        cur.execute(
            "INSERT INTO logs(ts, store, username, action, category, name, delta) VALUES(datetime('now'),?,?,?,?,?,?)",
            (active_store, user["username"], "DELTA", row["category"], row["name"], float(delta)),
        )

    return RedirectResponse("/inventario", status_code=HTTP_303_SEE_OTHER)

@app.post("/items/{item_id}/set")
def item_set(request: Request, item_id: int, qty: float = Form(...)):
    user = require_login(request)
    if not user:
        return RedirectResponse("/", status_code=HTTP_303_SEE_OTHER)

    admin = is_admin(request)
    active_store = (request.session.get("active_store") if admin else user.get("store"))
    if not active_store or active_store == "ALL":
        return RedirectResponse("/inventario", status_code=HTTP_303_SEE_OTHER)

    with connect() as conn:
        cur = conn.cursor()
        row = cur.execute("SELECT * FROM products WHERE id=? AND store=?", (int(item_id), active_store)).fetchone()
        if not row:
            return RedirectResponse("/inventario", status_code=HTTP_303_SEE_OTHER)

        cur.execute("UPDATE products SET qty=? WHERE id=?", (float(qty), int(item_id)))
        cur.execute(
            "INSERT INTO logs(ts, store, username, action, category, name, delta) VALUES(datetime('now'),?,?,?,?,?,?)",
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

    with connect() as conn:
        cur = conn.cursor()
        cur.execute(
            """INSERT INTO products(store, category, name, qty, min_qty)
               VALUES(?,?,?,?,?)
               ON CONFLICT(store, category, name) DO UPDATE SET qty=excluded.qty, min_qty=excluded.min_qty""",
            (active_store, category, name, float(qty), float(min_qty)),
        )
        cur.execute(
            "INSERT INTO logs(ts, store, username, action, category, name, delta) VALUES(datetime('now'),?,?,?,?,?,?)",
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

    with connect() as conn:
        cur = conn.cursor()
        row = cur.execute("SELECT * FROM products WHERE id=? AND store=?", (int(item_id), active_store)).fetchone()
        if not row:
            return RedirectResponse("/inventario", status_code=HTTP_303_SEE_OTHER)

        cur.execute(
            "UPDATE products SET category=?, name=?, min_qty=? WHERE id=?",
            (category, name, float(min_qty), int(item_id)),
        )
        cur.execute(
            "INSERT INTO logs(ts, store, username, action, category, name, delta) VALUES(datetime('now'),?,?,?,?,?,?)",
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

    with connect() as conn:
        cur = conn.cursor()
        row = cur.execute("SELECT * FROM products WHERE id=? AND store=?", (int(item_id), active_store)).fetchone()
        if row:
            cur.execute("DELETE FROM products WHERE id=?", (int(item_id),))
            cur.execute(
                "INSERT INTO logs(ts, store, username, action, category, name, delta) VALUES(datetime('now'),?,?,?,?,?,?)",
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

    with connect() as conn:
        cur = conn.cursor()
        rows = cur.execute(
            "SELECT category, name, qty, min_qty FROM products WHERE store=? ORDER BY category, name",
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

    count = 0
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
                """INSERT INTO products(store, category, name, qty, min_qty)
                   VALUES(?,?,?,?,?)
                   ON CONFLICT(store, category, name) DO UPDATE SET qty=excluded.qty, min_qty=excluded.min_qty""",
                (active_store, cat, name, qty, min_qty),
            )
            count += 1

        cur.execute(
            "INSERT INTO logs(ts, store, username, action, category, name, delta) VALUES(datetime('now'),?,?,?,?,?,?)",
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

    with connect() as conn:
        cur = conn.cursor()
        rows = cur.execute("SELECT * FROM logs ORDER BY id DESC LIMIT ?", (int(limit),)).fetchall()

    brand = request.session.get("active_store") or "spinza"
    return render("logs.html", user=user, rows=rows, limit=limit, stores=STORES, brand=brand)
