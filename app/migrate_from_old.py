import os, csv, io, base64, hashlib
from pathlib import Path
from cryptography.fernet import Fernet

from .db import connect, init_db
from .security import make_password

def _fernet_from_password(pw: str) -> Fernet:
    key = base64.urlsafe_b64encode(hashlib.sha256(pw.encode("utf-8")).digest())
    return Fernet(key)

def _read_encrypted_csv(path: Path, pw: str):
    if not path.exists():
        return []
    fnet = _fernet_from_password(pw)
    token = path.read_bytes()
    data = fnet.decrypt(token).decode("utf-8")
    return list(csv.reader(io.StringIO(data)))

def run_migration(data_dir: str):
    data_dir = Path(data_dir)
    enc_pw = os.environ.get("OLD_ENCRYPTION_PASSWORD", "spinza2025")

    inv = _read_encrypted_csv(data_dir / "inventario.csv", enc_pw)
    users = _read_encrypted_csv(data_dir / "utenti.csv", enc_pw)
    logs = _read_encrypted_csv(data_dir / "log_modifiche.csv", enc_pw)

    init_db()
    conn = connect()
    cur = conn.cursor()
    store = os.environ.get("DEFAULT_STORE", "spinza")


    for row in inv:
        if len(row) < 4:
            continue
        category, name, qty, min_qty = row[0], row[1], row[2], row[3]
        try:
            qty_f = float(qty)
        except:
            qty_f = 0.0
        try:
            min_f = float(min_qty)
        except:
            min_f = 0.0
        cur.execute(
            """INSERT INTO products(store, category, name, qty, min_qty)
               VALUES(?,?,?,?,?)
               ON CONFLICT(category, name) DO UPDATE SET qty=excluded.qty, min_qty=excluded.min_qty""",
            (store, category, name, qty_f, min_f),
        )

    for row in users:
        if len(row) < 2:
            continue
        username, legacy = row[0], row[1]
        cur.execute(
            """INSERT OR IGNORE INTO users(store, username, role, legacy_sha256)
               VALUES(?,?,?,?)""",
            (store, username, "employee", legacy),
        )

    admin_user = os.environ.get("ADMIN_USERNAME", "marco06")
    admin_pass = os.environ.get("ADMIN_PASSWORD", "spinza2025")
    salt, h = make_password(admin_pass)
    cur.execute(
        """INSERT INTO users(username, role, pw_salt, pw_hash)
           VALUES(?,?,?,?)
           ON CONFLICT(username) DO UPDATE SET role='super_admin', pw_salt=excluded.pw_salt, pw_hash=excluded.pw_hash, legacy_sha256=NULL""",
        (store, admin_user, "super_admin", salt, h),
    )

    for row in logs:
        if len(row) < 6:
            continue
        ts, username, action, category, name, delta = row[:6]
        try:
            delta_f = float(delta)
        except:
            delta_f = 0.0
        cur.execute(
            """INSERT INTO logs(ts, store, username, action, category, name, delta)
               VALUES(?,?,?,?,?,?,?)""",
            (ts, store, username, action, category, name, delta_f),
        )

    conn.commit()
    conn.close()
