from __future__ import annotations

import csv
import io
import json
import mimetypes
import shutil
import sqlite3
import threading
import webbrowser
from datetime import date, datetime
from calendar import monthrange
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

APP_DIR = Path(__file__).resolve().parent
STATIC_DIR = APP_DIR / "static"
DATA_DIR = APP_DIR / "data"

DB_PATH = DATA_DIR / "gestione_pagamenti.db"
HOST = "127.0.0.1"
PORT_START = 8911
PORT_END = 8940


def now_iso() -> str:
    return datetime.now().replace(microsecond=0).isoformat(sep=" ")


def today_iso() -> str:
    return date.today().isoformat()


def money_to_cents(value, *, allow_zero: bool = False) -> int:
    if value is None:
        raise ValueError("Importo mancante")
    text = str(value).strip().replace("€", "").replace(" ", "")
    if not text:
        raise ValueError("Importo mancante")
    if "," in text and "." in text:
        if text.rfind(",") > text.rfind("."):
            text = text.replace(".", "").replace(",", ".")
        else:
            text = text.replace(",", "")
    elif "," in text:
        text = text.replace(".", "").replace(",", ".")
    elif "." in text:
        parts = text.split(".")
        if len(parts) > 1 and all(part.isdigit() and len(part) == 3 for part in parts[1:]):
            text = "".join(parts)
    try:
        dec = Decimal(text).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    except InvalidOperation:
        raise ValueError("Importo non valido")
    cents = int(dec * 100)
    if cents < 0 or (cents == 0 and not allow_zero):
        raise ValueError("L'importo deve essere maggiore di zero" if not allow_zero else "L'importo non può essere negativo")
    return cents


def month_bounds(d: date) -> tuple[str, str]:
    start = d.replace(day=1)
    if start.month == 12:
        next_start = date(start.year + 1, 1, 1)
    else:
        next_start = date(start.year, start.month + 1, 1)
    return start.isoformat(), next_start.isoformat()


def previous_month(d: date) -> date:
    return date(d.year - 1, 12, 1) if d.month == 1 else date(d.year, d.month - 1, 1)




def month_start(d: date) -> date:
    return d.replace(day=1)


def month_end(d: date) -> date:
    return d.replace(day=monthrange(d.year, d.month)[1])


def tracking_start_date(conn: sqlite3.Connection, store_id: int) -> date | None:
    row = conn.execute(
        "SELECT MIN(date(created_at)) AS d FROM movements WHERE store_id=? AND movement_type='initial'",
        (store_id,),
    ).fetchone()
    raw = row["d"] if row else None
    if not raw:
        return None
    try:
        return date.fromisoformat(raw)
    except ValueError:
        return None


def actual_expenses_for_month(conn: sqlite3.Connection, store_id: int, target_month: date) -> tuple[int, int]:
    """Ritorna (totale, numero movimenti) delle uscite realmente pagate nel mese."""
    start, nxt = month_bounds(target_month)
    paid = conn.execute(
        """SELECT COALESCE(SUM(amount_cents),0) AS total, COUNT(*) AS c FROM payments
           WHERE store_id=? AND status='paid' AND date(paid_at)>=? AND date(paid_at)<?""",
        (store_id, start, nxt),
    ).fetchone()
    direct = conn.execute(
        """SELECT COALESCE(SUM(amount_cents),0) AS total, COUNT(*) AS c FROM variable_purchases
           WHERE store_id=? AND status='paid' AND payment_id IS NULL
             AND date(COALESCE(paid_at,purchase_date))>=? AND date(COALESCE(paid_at,purchase_date))<?""",
        (store_id, start, nxt),
    ).fetchone()
    return int(paid["total"] or 0) + int(direct["total"] or 0), int(paid["c"] or 0) + int(direct["c"] or 0)


def latest_baseline(conn: sqlite3.Connection, store_id: int, up_to_month: date) -> tuple[int, str | None]:
    key = up_to_month.strftime("%Y-%m")
    row = conn.execute(
        """SELECT report_month, COALESCE(SUM(amount_cents),0) AS total
           FROM baseline_expenses WHERE store_id=? AND report_month<=?
           GROUP BY report_month ORDER BY report_month DESC LIMIT 1""",
        (store_id, key),
    ).fetchone()
    if not row:
        return 0, None
    return int(row["total"] or 0), row["report_month"]


def forecast_reference(conn: sqlite3.Connection, store_id: int, target_month: date) -> dict:
    """
    Sceglie una base mensile affidabile senza trattare un mese parziale come un mese a zero.

    Regola:
    - se il mese precedente e' stato monitorato dall'inizio e contiene uscite reali, usa il consuntivo;
    - se il mese precedente e' parziale (tipico primo avvio a fine mese), usa l'ultimo storico disponibile;
    - se un mese completo non contiene alcun movimento, mantiene l'ultimo storico come guardrail e segnala la cosa.
    """
    prev = previous_month(target_month)
    prev_start = month_start(prev)
    prev_end = month_end(prev)
    actual, actual_count = actual_expenses_for_month(conn, store_id, prev)
    tracking_start = tracking_start_date(conn, store_id)
    complete = bool(tracking_start and tracking_start <= prev_start)
    if not tracking_start or tracking_start > prev_end:
        tracked_days = 0
    else:
        tracked_days = (prev_end - max(prev_start, tracking_start)).days + 1
    prev_days = monthrange(prev.year, prev.month)[1]
    tracked_pct = round(tracked_days * 100.0 / prev_days, 1) if prev_days else 0.0
    baseline_total, baseline_month = latest_baseline(conn, store_id, prev)

    if complete and actual_count > 0:
        amount = actual
        source = "actual_complete"
        reference_month = prev.strftime("%Y-%m")
        reason = "Il mese precedente e' stato monitorato dall'inizio alla fine: uso le uscite realmente pagate."
        quality = "reale"
    elif baseline_total > 0:
        amount = baseline_total
        reference_month = baseline_month
        if baseline_month == prev.strftime("%Y-%m"):
            source = "baseline_exact"
            reason = "Uso il dato storico caricato esplicitamente per il mese precedente."
            quality = "storico"
        elif complete:
            source = "baseline_guardrail"
            reason = "Il mese precedente risulta completo ma non contiene uscite reali: mantengo l'ultimo storico per evitare una previsione falsa a zero."
            quality = "protetta"
        else:
            source = "baseline_partial_fallback"
            reason = "Il mese precedente e' solo parziale: mantengo l'ultimo storico affidabile finche' non avrai un mese completo di dati."
            quality = "storico"
    else:
        amount = actual
        source = "partial_actual" if not complete else "actual_zero"
        reference_month = prev.strftime("%Y-%m")
        reason = "Non esiste ancora uno storico alternativo: uso i dati disponibili e segnalo che la previsione ha bassa affidabilita'."
        quality = "parziale"

    return {
        "amount_cents": int(amount),
        "source": source,
        "reference_month": reference_month,
        "reason": reason,
        "quality": quality,
        "previous_month": prev.strftime("%Y-%m"),
        "previous_month_actual_cents": int(actual),
        "previous_month_actual_count": int(actual_count),
        "previous_month_tracking_complete": complete,
        "previous_month_tracking_days": int(tracked_days),
        "previous_month_days": int(prev_days),
        "previous_month_tracking_percent": tracked_pct,
        "tracking_start_date": tracking_start.isoformat() if tracking_start else None,
    }

def add_months(d: date, months: int) -> date:
    """Sposta una data di N mesi mantenendo, quando possibile, il giorno."""
    total = d.year * 12 + (d.month - 1) + int(months)
    year, month0 = divmod(total, 12)
    month = month0 + 1
    day = min(d.day, monthrange(year, month)[1])
    return date(year, month, day)


FREQUENCY_LABELS = {1: "Mensile", 2: "Bimestrale", 3: "Trimestrale", 6: "Semestrale", 12: "Annuale"}


def fixed_monthly_equivalent(amount_cents: int, frequency_months: int) -> Decimal:
    return Decimal(int(amount_cents)) / Decimal(int(frequency_months))


def fixed_reserved_amount(row: sqlite3.Row, target: date | None = None) -> int:
    """
    Quota virtualmente impegnata per la prossima rata.

    La rata viene prima trasformata nella sua quota mensile (es. annuale / 12),
    poi ogni mese quella quota viene distribuita sui giorni reali del mese.
    La scadenza permette di ricostruire l'inizio del ciclo e quindi anche le
    quote maturate nei mesi precedenti.
    """
    target = target or date.today()
    due = date.fromisoformat(row["next_due_date"])
    freq = int(row["frequency_months"])
    amount = int(row["amount_cents"])
    cycle_start = add_months(due, -freq)
    if target < cycle_start:
        return 0
    if target >= due:
        return amount
    monthly = fixed_monthly_equivalent(amount, freq)
    total = Decimal(0)
    cursor = cycle_start
    while cursor <= target:
        days = monthrange(cursor.year, cursor.month)[1]
        total += monthly / Decimal(days)
        cursor = cursor.fromordinal(cursor.toordinal() + 1)
    cents = int(total.quantize(Decimal('1'), rounding=ROUND_HALF_UP))
    return max(0, min(amount, cents))


def serialize_fixed_expense(row: sqlite3.Row, target: date | None = None) -> dict:
    target = target or date.today()
    amount = int(row["amount_cents"])
    freq = int(row["frequency_months"])
    reserved = fixed_reserved_amount(row, target)
    monthly = int(fixed_monthly_equivalent(amount, freq).quantize(Decimal('1'), rounding=ROUND_HALF_UP))
    days = monthrange(target.year, target.month)[1]
    daily = int((Decimal(monthly) / Decimal(days)).quantize(Decimal('1'), rounding=ROUND_HALF_UP))
    due = date.fromisoformat(row["next_due_date"])
    progress = 100.0 if amount <= 0 else min(100.0, max(0.0, reserved * 100.0 / amount))
    keys = set(row.keys())
    return {
        "id": row["id"],
        "store_id": row["store_id"],
        "store_name": row["store_name"] if "store_name" in keys else None,
        "description": row["description"],
        "amount_cents": amount,
        "frequency_months": freq,
        "frequency_label": FREQUENCY_LABELS.get(freq, f"Ogni {freq} mesi"),
        "next_due_date": row["next_due_date"],
        "category": row["category"],
        "notes": row["notes"],
        "active": bool(row["active"]),
        "created_at": row["created_at"],
        "monthly_equivalent_cents": monthly,
        "daily_reserve_cents": daily,
        "reserved_cents": reserved,
        "remaining_cents": max(0, amount - reserved),
        "progress_percent": round(progress, 1),
        "days_to_due": (due - target).days,
        "cycle_start_date": add_months(due, -freq).isoformat(),
    }


def get_db() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def has_column(conn: sqlite3.Connection, table: str, column: str) -> bool:
    return any(row["name"] == column for row in conn.execute(f"PRAGMA table_info({table})").fetchall())


def init_db() -> None:
    # Copia di sicurezza al primo avvio della versione aggiornata.
    backup_dir = DATA_DIR / "backup"
    backup_dir.mkdir(exist_ok=True)
    pre_upgrade = backup_dir / "gestione_pagamenti_prima_selezione.db"
    if DB_PATH.exists() and not pre_upgrade.exists():
        with sqlite3.connect(DB_PATH) as source, sqlite3.connect(pre_upgrade) as destination:
            source.backup(destination)
    with get_db() as conn:
        conn.execute("CREATE TABLE IF NOT EXISTS expense_selections (selection_key TEXT PRIMARY KEY, created_at TEXT NOT NULL)")
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS stores (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL,
                created_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS payments (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                store_id INTEGER,
                description TEXT NOT NULL,
                amount_cents INTEGER NOT NULL CHECK(amount_cents > 0),
                due_date TEXT,
                category TEXT DEFAULT '',
                priority INTEGER NOT NULL DEFAULT 50 CHECK(priority BETWEEN 1 AND 100),
                notes TEXT DEFAULT '',
                status TEXT NOT NULL DEFAULT 'pending' CHECK(status IN ('pending','paid')),
                created_at TEXT NOT NULL,
                paid_at TEXT
            );

            CREATE TABLE IF NOT EXISTS recharges (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                store_id INTEGER,
                amount_cents INTEGER NOT NULL CHECK(amount_cents > 0),
                note TEXT DEFAULT '',
                created_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS movements (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                store_id INTEGER,
                movement_type TEXT NOT NULL CHECK(movement_type IN ('initial','recharge','payment')),
                amount_cents INTEGER NOT NULL,
                ref_id INTEGER,
                note TEXT DEFAULT '',
                created_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS baseline_expenses (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                store_id INTEGER,
                report_month TEXT NOT NULL,
                description TEXT NOT NULL,
                amount_cents INTEGER NOT NULL CHECK(amount_cents > 0),
                category TEXT DEFAULT '',
                notes TEXT DEFAULT '',
                created_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS fixed_expenses (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                store_id INTEGER NOT NULL,
                description TEXT NOT NULL,
                amount_cents INTEGER NOT NULL CHECK(amount_cents > 0),
                frequency_months INTEGER NOT NULL CHECK(frequency_months IN (1,2,3,6,12)),
                next_due_date TEXT NOT NULL,
                category TEXT DEFAULT '',
                notes TEXT DEFAULT '',
                active INTEGER NOT NULL DEFAULT 1 CHECK(active IN (0,1)),
                created_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS variable_budgets (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                store_id INTEGER NOT NULL,
                description TEXT NOT NULL,
                monthly_budget_cents INTEGER NOT NULL CHECK(monthly_budget_cents > 0),
                category TEXT DEFAULT '',
                notes TEXT DEFAULT '',
                active INTEGER NOT NULL DEFAULT 1 CHECK(active IN (0,1)),
                created_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS spending_limits (
                store_id INTEGER PRIMARY KEY,
                monthly_limit_cents INTEGER NOT NULL CHECK (monthly_limit_cents > 0),
                updated_at TEXT NOT NULL,
                FOREIGN KEY(store_id) REFERENCES stores(id)
            );

            CREATE TABLE IF NOT EXISTS variable_purchases (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                budget_id INTEGER NOT NULL,
                store_id INTEGER NOT NULL,
                amount_cents INTEGER NOT NULL CHECK(amount_cents > 0),
                purchase_date TEXT NOT NULL,
                note TEXT DEFAULT '',
                created_at TEXT NOT NULL
            );
            """
        )

        # Migrazione trasparente dalla v1.x: aggiunge store_id alle tabelle esistenti.
        for table in ("payments", "recharges", "movements", "baseline_expenses"):
            if not has_column(conn, table, "store_id"):
                conn.execute(f"ALTER TABLE {table} ADD COLUMN store_id INTEGER")
        if not has_column(conn, "payments", "fixed_expense_id"):
            conn.execute("ALTER TABLE payments ADD COLUMN fixed_expense_id INTEGER")
        if not has_column(conn, "payments", "estimated_amount_cents"):
            conn.execute("ALTER TABLE payments ADD COLUMN estimated_amount_cents INTEGER")
        if not has_column(conn, "variable_purchases", "status"):
            conn.execute("ALTER TABLE variable_purchases ADD COLUMN status TEXT NOT NULL DEFAULT 'paid'")
        if not has_column(conn, "variable_purchases", "payment_id"):
            conn.execute("ALTER TABLE variable_purchases ADD COLUMN payment_id INTEGER")
        if not has_column(conn, "variable_purchases", "due_date"):
            conn.execute("ALTER TABLE variable_purchases ADD COLUMN due_date TEXT")
        if not has_column(conn, "variable_purchases", "paid_at"):
            conn.execute("ALTER TABLE variable_purchases ADD COLUMN paid_at TEXT")
        conn.execute("UPDATE variable_purchases SET status='paid' WHERE status IS NULL OR status=''")
        conn.execute("UPDATE variable_purchases SET paid_at=created_at WHERE status='paid' AND (paid_at IS NULL OR paid_at='')")

        conn.executescript(
            """
            CREATE INDEX IF NOT EXISTS idx_payments_store_status ON payments(store_id, status);
            CREATE INDEX IF NOT EXISTS idx_payments_paid_at ON payments(paid_at);
            CREATE INDEX IF NOT EXISTS idx_recharges_store_created ON recharges(store_id, created_at);
            CREATE INDEX IF NOT EXISTS idx_movements_store_created ON movements(store_id, created_at);
            CREATE INDEX IF NOT EXISTS idx_baseline_store_month ON baseline_expenses(store_id, report_month);
            CREATE INDEX IF NOT EXISTS idx_fixed_store_active ON fixed_expenses(store_id, active);
            CREATE INDEX IF NOT EXISTS idx_fixed_due ON fixed_expenses(next_due_date);
            CREATE INDEX IF NOT EXISTS idx_payments_fixed_expense ON payments(fixed_expense_id);
            CREATE INDEX IF NOT EXISTS idx_variable_budget_store_active ON variable_budgets(store_id, active);
            CREATE INDEX IF NOT EXISTS idx_variable_purchase_store_date ON variable_purchases(store_id, purchase_date);
            CREATE INDEX IF NOT EXISTS idx_variable_purchase_budget ON variable_purchases(budget_id);
            CREATE INDEX IF NOT EXISTS idx_variable_purchase_payment ON variable_purchases(payment_id);
            """
        )

        legacy_count = 0
        for table in ("payments", "recharges", "movements", "baseline_expenses"):
            legacy_count += conn.execute(f"SELECT COUNT(*) AS c FROM {table} WHERE store_id IS NULL").fetchone()["c"]
        if legacy_count:
            store = conn.execute("SELECT id FROM stores ORDER BY id LIMIT 1").fetchone()
            if not store:
                cur = conn.execute("INSERT INTO stores(name, created_at) VALUES(?,?)", ("Negozio principale", now_iso()))
                store_id = cur.lastrowid
            else:
                store_id = store["id"]
            for table in ("payments", "recharges", "movements", "baseline_expenses"):
                conn.execute(f"UPDATE {table} SET store_id=? WHERE store_id IS NULL", (store_id,))


def get_store(conn: sqlite3.Connection, store_id: int) -> sqlite3.Row:
    row = conn.execute("SELECT * FROM stores WHERE id=?", (store_id,)).fetchone()
    if not row:
        raise ValueError("Negozio non trovato")
    return row


def serialize_payment(row: sqlite3.Row) -> dict:
    keys = set(row.keys())
    return {
        "id": row["id"],
        "store_id": row["store_id"],
        "store_name": row["store_name"] if "store_name" in keys else None,
        "description": row["description"],
        "amount_cents": row["amount_cents"],
        "due_date": row["due_date"],
        "category": row["category"],
        "priority": row["priority"],
        "notes": row["notes"],
        "status": row["status"],
        "created_at": row["created_at"],
        "paid_at": row["paid_at"],
        "fixed_expense_id": row["fixed_expense_id"] if "fixed_expense_id" in keys else None,
        "estimated_amount_cents": row["estimated_amount_cents"] if "estimated_amount_cents" in keys else None,
    }


def sync_overdue_fixed_expenses(conn: sqlite3.Connection, target: date | None = None) -> int:
    """Sposta le rate fisse dei mesi conclusi nei Pagamenti e fa avanzare il ciclo."""
    target = target or date.today()
    current_month_start = target.replace(day=1)
    rows = conn.execute("SELECT * FROM fixed_expenses WHERE active=1 ORDER BY id").fetchall()
    created = 0
    for row in rows:
        due = date.fromisoformat(row["next_due_date"])
        freq = int(row["frequency_months"])
        while due < current_month_start:
            exists = conn.execute(
                "SELECT 1 FROM payments WHERE fixed_expense_id=? AND due_date=? LIMIT 1",
                (row["id"], due.isoformat()),
            ).fetchone()
            if not exists:
                conn.execute(
                    """INSERT INTO payments(store_id,description,amount_cents,estimated_amount_cents,due_date,category,priority,notes,status,created_at,fixed_expense_id)
                       VALUES(?,?,?,?,?,?,95,?,'pending',?,?)""",
                    (row["store_id"], row["description"], row["amount_cents"], row["amount_cents"], due.isoformat(),
                     row["category"] or "Spesa fissa arretrata", row["notes"] or "Rata fissa non pagata nel mese", now_iso(), row["id"]),
                )
                created += 1
            due = add_months(due, freq)
        if due.isoformat() != row["next_due_date"]:
            conn.execute("UPDATE fixed_expenses SET next_due_date=? WHERE id=?", (due.isoformat(), row["id"]))
    return created


def serialize_variable_budget(conn: sqlite3.Connection, row: sqlite3.Row, target: date | None = None) -> dict:
    target = target or date.today()
    start, nxt = month_bounds(target)
    orders = conn.execute(
        """SELECT p.*, pay.amount_cents AS linked_paid_amount, pay.status AS linked_payment_status
           FROM variable_purchases p
           LEFT JOIN payments pay ON pay.id=p.payment_id
           WHERE p.budget_id=? AND date(p.purchase_date)>=? AND date(p.purchase_date)<?
           ORDER BY date(p.purchase_date), p.id""",
        (row["id"], start, nxt),
    ).fetchall()

    budget = int(row["monthly_budget_cents"])
    ordered = sum(int(r["amount_cents"]) for r in orders)
    order_count = len(orders)
    pending_rows = [r for r in orders if r["status"] == "pending"]
    paid_rows = [r for r in orders if r["status"] == "paid"]
    pending_cents = sum(int(r["amount_cents"]) for r in pending_rows)
    paid_actual_cents = 0
    for r in paid_rows:
        if r["payment_id"] and r["linked_payment_status"] == "paid" and r["linked_paid_amount"] is not None:
            paid_actual_cents += int(r["linked_paid_amount"])
        else:
            paid_actual_cents += int(r["amount_cents"])

    remaining_to_order = max(0, budget - ordered)
    over_budget = max(0, ordered - budget)
    # Il budget previsto viene protetto subito. Quando una parte viene pagata,
    # non deve restare impegnata una seconda volta: rimangono protetti gli
    # ordini ancora da saldare + la parte di budget non ancora ordinata.
    committed = pending_cents + remaining_to_order
    average_order = round(ordered / order_count) if order_count else 0

    return {
        "id": row["id"], "store_id": row["store_id"],
        "store_name": row["store_name"] if "store_name" in row.keys() else None,
        "description": row["description"], "monthly_budget_cents": budget,
        "category": row["category"], "notes": row["notes"], "active": bool(row["active"]),
        "created_at": row["created_at"], "month": target.strftime("%Y-%m"),
        "spent_cents": ordered, "ordered_cents": ordered,
        "remaining_cents": budget - ordered, "remaining_to_order_cents": remaining_to_order,
        "over_budget_cents": over_budget,
        "purchase_count": order_count, "order_count": order_count,
        "pending_order_count": len(pending_rows), "paid_order_count": len(paid_rows),
        "pending_order_cents": pending_cents, "paid_order_cents": paid_actual_cents,
        "average_order_cents": average_order,
        "progress_percent": round((ordered * 100 / budget) if budget else 0, 1),
        "committed_cents": committed,
    }

def store_dashboard(conn: sqlite3.Connection, store_id: int, target: date | None = None) -> dict:
    store = get_store(conn, store_id)
    today = target or date.today()
    sync_overdue_fixed_expenses(conn, today)
    current_start, current_next = month_bounds(today)
    prev = previous_month(today)
    prev_key = prev.strftime("%Y-%m")

    initialized = conn.execute(
        "SELECT 1 FROM movements WHERE store_id=? AND movement_type='initial' LIMIT 1", (store_id,)
    ).fetchone() is not None
    baseline_any_count = conn.execute(
        "SELECT COUNT(*) AS c FROM baseline_expenses WHERE store_id=?", (store_id,)
    ).fetchone()["c"]

    reference = forecast_reference(conn, store_id, today)
    reference_expenses = int(reference["amount_cents"])

    balance = conn.execute(
        "SELECT COALESCE(SUM(amount_cents),0) AS total FROM movements WHERE store_id=?", (store_id,)
    ).fetchone()["total"]

    current_paid_payments = conn.execute(
        """SELECT COALESCE(SUM(amount_cents),0) AS total FROM payments
           WHERE store_id=? AND status='paid' AND date(paid_at)>=? AND date(paid_at)<?""",
        (store_id, current_start, current_next),
    ).fetchone()["total"]
    current_variable_direct_paid = conn.execute(
        """SELECT COALESCE(SUM(amount_cents),0) AS total FROM variable_purchases
           WHERE store_id=? AND status='paid' AND payment_id IS NULL
             AND date(COALESCE(paid_at,purchase_date))>=? AND date(COALESCE(paid_at,purchase_date))<?""",
        (store_id, current_start, current_next),
    ).fetchone()["total"]
    current_expenses = int(current_paid_payments) + int(current_variable_direct_paid)
    current_regular_payments = conn.execute(
        """SELECT COALESCE(SUM(p.amount_cents),0) AS total FROM payments p
           WHERE p.store_id=? AND p.status='paid' AND p.fixed_expense_id IS NULL
             AND NOT EXISTS (SELECT 1 FROM variable_purchases v WHERE v.payment_id=p.id)
             AND date(p.paid_at)>=? AND date(p.paid_at)<?""",
        (store_id, current_start, current_next),
    ).fetchone()["total"]
    current_variable_expenses = int(current_regular_payments)
    current_fixed_paid = int(current_paid_payments) - int(current_regular_payments)
    current_recharges = conn.execute(
        """SELECT COALESCE(SUM(amount_cents),0) AS total FROM recharges
           WHERE store_id=? AND date(created_at)>=? AND date(created_at)<?""",
        (store_id, current_start, current_next),
    ).fetchone()["total"]

    pending_rows = conn.execute(
        """SELECT p.*, s.name AS store_name FROM payments p JOIN stores s ON s.id=p.store_id
           WHERE p.store_id=? AND p.status='pending'
           ORDER BY CASE WHEN p.due_date IS NULL OR p.due_date='' THEN 1 ELSE 0 END,
                    date(p.due_date) ASC, p.priority DESC, p.id DESC""",
        (store_id,),
    ).fetchall()
    pending_count = len(pending_rows)
    pending_amount = sum(int(r["amount_cents"]) for r in pending_rows)
    paid_count = conn.execute(
        "SELECT COUNT(*) AS c FROM payments WHERE store_id=? AND status='paid'", (store_id,)
    ).fetchone()["c"]
    next_due_rows = pending_rows[:5]

    overdue_amount = overdue_count = due_7_amount = due_7_count = due_30_amount = due_30_count = 0
    for r in pending_rows:
        raw = r["due_date"]
        if not raw:
            continue
        try:
            due = date.fromisoformat(raw)
        except ValueError:
            continue
        delta = (due - today).days
        amount = int(r["amount_cents"])
        if delta < 0:
            overdue_count += 1; overdue_amount += amount
        elif delta <= 7:
            due_7_count += 1; due_7_amount += amount
        if 0 <= delta <= 30:
            due_30_count += 1; due_30_amount += amount

    fixed_rows = conn.execute(
        """SELECT f.*, s.name AS store_name FROM fixed_expenses f
           JOIN stores s ON s.id=f.store_id
           WHERE f.store_id=? AND f.active=1
           ORDER BY date(f.next_due_date) ASC, f.id ASC""",
        (store_id,),
    ).fetchall()
    fixed_items = [serialize_fixed_expense(r, today) for r in fixed_rows]
    fixed_monthly_decimal = sum((fixed_monthly_equivalent(r["amount_cents"], r["frequency_months"]) for r in fixed_rows), Decimal(0))
    fixed_monthly_only = int(fixed_monthly_decimal.quantize(Decimal('1'), rounding=ROUND_HALF_UP))

    variable_rows = conn.execute(
        """SELECT v.*, s.name AS store_name FROM variable_budgets v
           JOIN stores s ON s.id=v.store_id
           WHERE v.store_id=? AND v.active=1 ORDER BY v.id""",
        (store_id,),
    ).fetchall()
    variable_items = [serialize_variable_budget(conn, r, today) for r in variable_rows]
    variable_monthly_budget_total = sum(int(i["monthly_budget_cents"]) for i in variable_items)
    variable_commitment = sum(int(i["committed_cents"]) for i in variable_items)
    current_variable_orders = sum(int(i["ordered_cents"]) for i in variable_items)
    variable_pending_orders = sum(int(i["pending_order_cents"]) for i in variable_items)
    variable_paid_orders = sum(int(i["paid_order_cents"]) for i in variable_items)
    variable_remaining_to_order = sum(int(i["remaining_to_order_cents"]) for i in variable_items)
    variable_over_budget = sum(int(i["over_budget_cents"]) for i in variable_items)
    variable_order_count = sum(int(i["order_count"]) for i in variable_items)
    variable_pending_order_count = sum(int(i["pending_order_count"]) for i in variable_items)
    variable_paid_order_count = sum(int(i["paid_order_count"]) for i in variable_items)
    variable_average_order = round(current_variable_orders / variable_order_count) if variable_order_count else 0

    protected_monthly_quota = fixed_monthly_only + variable_monthly_budget_total
    fixed_reserved_today = sum(int(i["reserved_cents"]) for i in fixed_items)
    committed_money = fixed_reserved_today + variable_commitment

    month_days = monthrange(today.year, today.month)[1]
    variable_projection = int(round((current_variable_orders / max(1, today.day)) * month_days)) if current_variable_orders else 0
    variable_projection_delta = variable_projection - variable_monthly_budget_total
    variable_progress_percent = round((current_variable_orders * 100 / variable_monthly_budget_total), 1) if variable_monthly_budget_total else 0.0
    fixed_daily = int((fixed_monthly_decimal / Decimal(month_days)).quantize(Decimal('1'), rounding=ROUND_HALF_UP)) if fixed_rows else 0

    # La base flessibile e' cio' che rimane della previsione mensile dopo aver protetto fisse + budget fornitori.
    flexible_budget = max(0, reference_expenses - protected_monthly_quota)
    daily_aware_decimal = Decimal(flexible_budget) / Decimal(month_days)
    daily_aware = int(daily_aware_decimal.quantize(Decimal('1'), rounding=ROUND_HALF_UP))
    aware_days = today.day
    aware_gross = int((daily_aware_decimal * Decimal(aware_days)).quantize(Decimal('1'), rounding=ROUND_HALF_UP))
    aware_funds = aware_gross - int(current_variable_expenses)

    # Gli ordini variabili da pagare sono gia' inclusi nel budget variabile impegnato: non contarli due volte.
    other_pending = max(0, int(pending_amount) - int(variable_pending_orders))
    known_obligations = int(committed_money) + other_pending
    cash_after_commitments = int(balance) - known_obligations
    coverage_percent = round((int(balance) * 100.0 / known_obligations), 1) if known_obligations > 0 else None

    planned_month_total = max(reference_expenses, protected_monthly_quota)
    forecast_gap = max(0, protected_monthly_quota - reference_expenses)
    month_progress = round(today.day * 100.0 / month_days, 1)
    actual_vs_plan = round(current_expenses * 100.0 / planned_month_total, 1) if planned_month_total else 0.0

    track_start = tracking_start_date(conn, store_id)
    current_start_date = date.fromisoformat(current_start)
    current_tracking_complete = bool(track_start and track_start <= current_start_date)
    current_tracking_days = 0 if not track_start or track_start > today else (today - max(track_start, current_start_date)).days + 1
    current_tracking_percent = round(current_tracking_days * 100.0 / month_days, 1) if month_days else 0.0

    # Anteprima della regola che verra' usata al cambio mese.
    if current_tracking_complete:
        next_month_policy = "actual_when_closed"
        next_month_reference_cents = None
    else:
        fallback_total, _fallback_month = latest_baseline(conn, store_id, today)
        next_month_policy = "fallback_history"
        next_month_reference_cents = int(fallback_total or reference_expenses)

    setup_complete = initialized and baseline_any_count > 0

    return {
        "scope": "store", "is_global": False,
        "store_id": store_id, "store_name": store["name"],
        "initialized": initialized, "setup_complete": setup_complete,
        "needs_initial_balance": not initialized, "needs_previous_report": baseline_any_count == 0,
        "balance_cents": int(balance),

        # Compatibilita' + nuova semantica esplicita della base previsione.
        "previous_month_expenses_cents": int(reference_expenses),
        "previous_month_source": reference["source"],
        "forecast_reference_cents": int(reference_expenses),
        "forecast_reference_month": reference["reference_month"],
        "forecast_reference_source": reference["source"],
        "forecast_reference_reason": reference["reason"],
        "forecast_reference_quality": reference["quality"],
        "previous_month_actual_cents": reference["previous_month_actual_cents"],
        "previous_month_actual_count": reference["previous_month_actual_count"],
        "previous_month_tracking_complete": reference["previous_month_tracking_complete"],
        "previous_month_tracking_days": reference["previous_month_tracking_days"],
        "previous_month_days": reference["previous_month_days"],
        "previous_month_tracking_percent": reference["previous_month_tracking_percent"],
        "tracking_start_date": reference["tracking_start_date"],
        "current_month_tracking_complete": current_tracking_complete,
        "current_month_tracking_days": int(current_tracking_days),
        "current_month_tracking_percent": current_tracking_percent,
        "next_month_policy": next_month_policy,
        "next_month_reference_cents": next_month_reference_cents,

        "fixed_monthly_equivalent_cents": protected_monthly_quota,
        "fixed_only_monthly_equivalent_cents": fixed_monthly_only,
        "fixed_reserved_today_cents": int(fixed_reserved_today),
        "variable_monthly_budget_total_cents": int(variable_monthly_budget_total),
        "variable_budget_commitment_cents": int(variable_commitment),
        "variable_orders_cents": int(current_variable_orders),
        "variable_pending_orders_cents": int(variable_pending_orders),
        "variable_paid_orders_cents": int(variable_paid_orders),
        "variable_remaining_to_order_cents": int(variable_remaining_to_order),
        "variable_over_budget_cents": int(variable_over_budget),
        "variable_order_count": int(variable_order_count),
        "variable_pending_order_count": int(variable_pending_order_count),
        "variable_paid_order_count": int(variable_paid_order_count),
        "variable_average_order_cents": int(variable_average_order),
        "variable_projection_cents": int(variable_projection),
        "variable_projection_delta_cents": int(variable_projection_delta),
        "variable_progress_percent": variable_progress_percent,
        "fixed_daily_reserve_cents": fixed_daily,
        "committed_money_cents": int(committed_money),
        "known_obligations_cents": int(known_obligations),
        "other_pending_cents": int(other_pending),
        "cash_after_commitments_cents": int(cash_after_commitments),
        "commitment_coverage_percent": coverage_percent,
        "planned_month_total_cents": int(planned_month_total),
        "forecast_gap_cents": int(forecast_gap),
        "variable_budget_cents": int(flexible_budget),
        "current_month_expenses_cents": int(current_expenses),
        "current_variable_expenses_cents": int(current_variable_expenses),
        "current_fixed_paid_cents": int(current_fixed_paid),
        "current_month_recharges_cents": int(current_recharges),
        "daily_aware_cents": daily_aware,
        "month_days": int(month_days),
        "month_progress_percent": month_progress,
        "actual_vs_plan_percent": actual_vs_plan,
        "aware_gross_cents": aware_gross,
        "aware_funds_cents": aware_funds,
        "day_of_month": today.day, "aware_days": int(aware_days), "aware_start_date": None,
        "pending_count": int(pending_count), "pending_amount_cents": int(pending_amount), "paid_count": int(paid_count),
        "overdue_count": int(overdue_count), "overdue_amount_cents": int(overdue_amount),
        "due_7_count": int(due_7_count), "due_7_amount_cents": int(due_7_amount),
        "due_30_count": int(due_30_count), "due_30_amount_cents": int(due_30_amount),
        "fixed_expenses_count": len(fixed_items), "fixed_expenses": fixed_items,
        "variable_budget_count": len(variable_items), "variable_purchases_cents": int(current_variable_orders),
        "next_due": [serialize_payment(r) for r in next_due_rows],
        "today": today.isoformat(), "current_month": today.strftime("%Y-%m"), "previous_month": prev_key,
    }


def global_dashboard(conn: sqlite3.Connection, target: date | None = None) -> dict:
    stores = conn.execute("SELECT * FROM stores ORDER BY name COLLATE NOCASE, id").fetchall()
    today = target or date.today()
    store_data = [store_dashboard(conn, r["id"], today) for r in stores]
    next_due_rows = conn.execute(
        """SELECT p.*, s.name AS store_name FROM payments p JOIN stores s ON s.id=p.store_id
           WHERE p.status='pending'
           ORDER BY CASE WHEN p.due_date IS NULL OR p.due_date='' THEN 1 ELSE 0 END,
                    date(p.due_date) ASC, p.priority DESC, p.id DESC LIMIT 8"""
    ).fetchall()
    fixed_items = []
    for d in store_data:
        fixed_items.extend(d.get("fixed_expenses", []))
    fixed_items.sort(key=lambda x: (x["next_due_date"], x["store_name"] or "", x["description"]))

    reference_sources = {d.get("forecast_reference_source") for d in store_data}
    reference_months = {d.get("forecast_reference_month") for d in store_data}
    tracking_complete = all(d.get("previous_month_tracking_complete") for d in store_data) if store_data else False
    current_tracking_complete = all(d.get("current_month_tracking_complete") for d in store_data) if store_data else False
    known_obligations = sum(d.get("known_obligations_cents", 0) for d in store_data)
    balance = sum(d["balance_cents"] for d in store_data)
    coverage = round(balance * 100.0 / known_obligations, 1) if known_obligations > 0 else None

    if len(reference_sources) == 1:
        ref_source = next(iter(reference_sources)) if reference_sources else "aggregate"
    else:
        ref_source = "mixed"
    ref_month = next(iter(reference_months)) if len(reference_months) == 1 else None
    if ref_source == "mixed":
        ref_reason = "I negozi stanno usando fonti di previsione diverse; ogni punto vendita mantiene la propria base piu' affidabile."
        ref_quality = "mista"
    else:
        ref_reason = store_data[0].get("forecast_reference_reason", "") if store_data else ""
        qualities = {d.get("forecast_reference_quality") for d in store_data}
        ref_quality = next(iter(qualities)) if len(qualities) == 1 else "mista"

    return {
        "scope": "global", "is_global": True, "store_id": None, "store_name": "Tutti i negozi",
        "initialized": bool(stores), "setup_complete": bool(stores) and all(d["setup_complete"] for d in store_data),
        "needs_initial_balance": False, "needs_previous_report": False,
        "balance_cents": balance,
        "previous_month_expenses_cents": sum(d["previous_month_expenses_cents"] for d in store_data),
        "previous_month_source": ref_source,
        "forecast_reference_cents": sum(d.get("forecast_reference_cents",0) for d in store_data),
        "forecast_reference_month": ref_month,
        "forecast_reference_source": ref_source,
        "forecast_reference_reason": ref_reason,
        "forecast_reference_quality": ref_quality,
        "previous_month_actual_cents": sum(d.get("previous_month_actual_cents",0) for d in store_data),
        "previous_month_actual_count": sum(d.get("previous_month_actual_count",0) for d in store_data),
        "previous_month_tracking_complete": tracking_complete,
        "previous_month_tracking_days": min((d.get("previous_month_tracking_days",0) for d in store_data), default=0),
        "previous_month_days": monthrange(previous_month(today).year, previous_month(today).month)[1],
        "previous_month_tracking_percent": min((d.get("previous_month_tracking_percent",0) for d in store_data), default=0),
        "tracking_start_date": min((d.get("tracking_start_date") for d in store_data if d.get("tracking_start_date")), default=None),
        "current_month_tracking_complete": current_tracking_complete,
        "current_month_tracking_days": min((d.get("current_month_tracking_days",0) for d in store_data), default=0),
        "current_month_tracking_percent": min((d.get("current_month_tracking_percent",0) for d in store_data), default=0),
        "next_month_policy": "actual_when_closed" if current_tracking_complete else "fallback_history",
        "next_month_reference_cents": None if current_tracking_complete else sum((d.get("next_month_reference_cents") or 0) for d in store_data),

        "fixed_monthly_equivalent_cents": sum(d["fixed_monthly_equivalent_cents"] for d in store_data),
        "fixed_only_monthly_equivalent_cents": sum(d.get("fixed_only_monthly_equivalent_cents",0) for d in store_data),
        "fixed_reserved_today_cents": sum(d.get("fixed_reserved_today_cents",0) for d in store_data),
        "variable_monthly_budget_total_cents": sum(d.get("variable_monthly_budget_total_cents",0) for d in store_data),
        "variable_budget_commitment_cents": sum(d.get("variable_budget_commitment_cents",0) for d in store_data),
        "variable_orders_cents": sum(d.get("variable_orders_cents",0) for d in store_data),
        "variable_pending_orders_cents": sum(d.get("variable_pending_orders_cents",0) for d in store_data),
        "variable_paid_orders_cents": sum(d.get("variable_paid_orders_cents",0) for d in store_data),
        "variable_remaining_to_order_cents": sum(d.get("variable_remaining_to_order_cents",0) for d in store_data),
        "variable_over_budget_cents": sum(d.get("variable_over_budget_cents",0) for d in store_data),
        "variable_order_count": sum(d.get("variable_order_count",0) for d in store_data),
        "variable_pending_order_count": sum(d.get("variable_pending_order_count",0) for d in store_data),
        "variable_paid_order_count": sum(d.get("variable_paid_order_count",0) for d in store_data),
        "variable_average_order_cents": (round(sum(d.get("variable_orders_cents",0) for d in store_data) / max(1, sum(d.get("variable_order_count",0) for d in store_data))) if sum(d.get("variable_order_count",0) for d in store_data) else 0),
        "variable_projection_cents": sum(d.get("variable_projection_cents",0) for d in store_data),
        "variable_projection_delta_cents": sum(d.get("variable_projection_delta_cents",0) for d in store_data),
        "variable_progress_percent": (round(sum(d.get("variable_orders_cents",0) for d in store_data) * 100 / max(1, sum(d.get("variable_monthly_budget_total_cents",0) for d in store_data)), 1) if sum(d.get("variable_monthly_budget_total_cents",0) for d in store_data) else 0.0),
        "fixed_daily_reserve_cents": sum(d["fixed_daily_reserve_cents"] for d in store_data),
        "committed_money_cents": sum(d["committed_money_cents"] for d in store_data),
        "known_obligations_cents": known_obligations,
        "other_pending_cents": sum(d.get("other_pending_cents",0) for d in store_data),
        "cash_after_commitments_cents": balance - known_obligations,
        "commitment_coverage_percent": coverage,
        "planned_month_total_cents": sum(d.get("planned_month_total_cents",0) for d in store_data),
        "forecast_gap_cents": sum(d.get("forecast_gap_cents",0) for d in store_data),
        "variable_budget_cents": sum(d["variable_budget_cents"] for d in store_data),
        "current_month_expenses_cents": sum(d["current_month_expenses_cents"] for d in store_data),
        "current_variable_expenses_cents": sum(d["current_variable_expenses_cents"] for d in store_data),
        "current_fixed_paid_cents": sum(d["current_fixed_paid_cents"] for d in store_data),
        "current_month_recharges_cents": sum(d["current_month_recharges_cents"] for d in store_data),
        "daily_aware_cents": sum(d["daily_aware_cents"] for d in store_data),
        "aware_gross_cents": sum(d["aware_gross_cents"] for d in store_data),
        "aware_funds_cents": sum(d["aware_funds_cents"] for d in store_data),
        "day_of_month": today.day, "month_days": monthrange(today.year, today.month)[1], "aware_days": today.day, "aware_start_date": None,
        "month_progress_percent": round(today.day * 100.0 / monthrange(today.year,today.month)[1],1),
        "actual_vs_plan_percent": (round(sum(d["current_month_expenses_cents"] for d in store_data) * 100.0 / max(1,sum(d.get("planned_month_total_cents",0) for d in store_data)),1) if store_data else 0.0),
        "pending_count": sum(d["pending_count"] for d in store_data), "pending_amount_cents": sum(d["pending_amount_cents"] for d in store_data), "paid_count": sum(d["paid_count"] for d in store_data),
        "overdue_count": sum(d.get("overdue_count",0) for d in store_data), "overdue_amount_cents": sum(d.get("overdue_amount_cents",0) for d in store_data),
        "due_7_count": sum(d.get("due_7_count",0) for d in store_data), "due_7_amount_cents": sum(d.get("due_7_amount_cents",0) for d in store_data),
        "due_30_count": sum(d.get("due_30_count",0) for d in store_data), "due_30_amount_cents": sum(d.get("due_30_amount_cents",0) for d in store_data),
        "fixed_expenses_count": sum(d["fixed_expenses_count"] for d in store_data), "fixed_expenses": fixed_items,
        "variable_budget_count": sum(d.get("variable_budget_count",0) for d in store_data), "variable_purchases_cents": sum(d.get("variable_purchases_cents",0) for d in store_data),
        "next_due": [serialize_payment(r) for r in next_due_rows],
        "today": today.isoformat(), "current_month": today.strftime("%Y-%m"), "previous_month": previous_month(today).strftime("%Y-%m"),
        "stores": [{
            "id": d["store_id"], "name": d["store_name"], "setup_complete": d["setup_complete"],
            "balance_cents": d["balance_cents"], "aware_funds_cents": d["aware_funds_cents"],
            "committed_money_cents": d["committed_money_cents"], "known_obligations_cents": d.get("known_obligations_cents",0),
            "fixed_monthly_equivalent_cents": d["fixed_monthly_equivalent_cents"],
            "fixed_only_monthly_equivalent_cents": d.get("fixed_only_monthly_equivalent_cents",0),
            "variable_monthly_budget_total_cents": d.get("variable_monthly_budget_total_cents",0),
            "variable_orders_cents": d.get("variable_orders_cents",0), "variable_pending_orders_cents": d.get("variable_pending_orders_cents",0),
            "variable_remaining_to_order_cents": d.get("variable_remaining_to_order_cents",0),
            "daily_aware_cents": d["daily_aware_cents"], "current_month_expenses_cents": d["current_month_expenses_cents"],
            "current_month_recharges_cents": d["current_month_recharges_cents"], "pending_count": d["pending_count"],
            "fixed_expenses_count": d["fixed_expenses_count"], "variable_budget_count": d.get("variable_budget_count",0),
            "forecast_reference_cents": d.get("forecast_reference_cents",0), "forecast_reference_source": d.get("forecast_reference_source")
        } for d in store_data],
    }

def parse_store_id(value) -> int:
    try:
        sid = int(value)
    except Exception:
        raise ValueError("Seleziona un negozio")
    if sid <= 0:
        raise ValueError("Seleziona un negozio")
    return sid



def expense_history(conn: sqlite3.Connection, scope: str, store_id: int | None, period: str, anchor: date) -> dict:
    """Registro delle uscite realmente sostenute, senza duplicare gli ordini collegati a pagamenti."""
    if period not in {"week", "month", "year", "all"}:
        raise ValueError("Periodo storico non valido")
    if period == "week":
        first = anchor.fromordinal(anchor.toordinal() - anchor.weekday())
        last = first.fromordinal(first.toordinal() + 7)
    elif period == "month":
        first = month_start(anchor)
        last = add_months(first, 1)
    elif period == "year":
        first, last = date(anchor.year, 1, 1), date(anchor.year + 1, 1, 1)
    else:
        first = last = None

    parts = ["p.status='paid'", "p.paid_at IS NOT NULL"]
    direct = ["v.status='paid'", "v.payment_id IS NULL"]
    args_p: list = []
    args_v: list = []
    if scope != 'global':
        if store_id is None:
            raise ValueError('Seleziona un negozio')
        parts.append('p.store_id=?'); direct.append('v.store_id=?')
        args_p.append(store_id); args_v.append(store_id)
    if first is not None:
        parts += ["date(p.paid_at)>=?", "date(p.paid_at)<?"]
        direct += ["date(COALESCE(v.paid_at,v.purchase_date))>=?", "date(COALESCE(v.paid_at,v.purchase_date))<?"]
        args_p += [first.isoformat(), last.isoformat()]
        args_v += [first.isoformat(), last.isoformat()]
    sql = f"""
        SELECT p.id AS id, p.store_id, s.name AS store_name, p.description,
               p.category, p.notes, p.amount_cents, p.paid_at AS event_at,
               CASE WHEN p.fixed_expense_id IS NOT NULL THEN 'Spesa fissa'
                    WHEN EXISTS (SELECT 1 FROM variable_purchases v WHERE v.payment_id=p.id)
                    THEN 'Ordine fornitore' ELSE 'Pagamento' END AS expense_type
          FROM payments p JOIN stores s ON s.id=p.store_id
         WHERE {' AND '.join(parts)}
        UNION ALL
        SELECT v.id AS id, v.store_id, s.name AS store_name,
               b.description || ' - ordine' AS description,
               b.category, v.note AS notes, v.amount_cents,
               COALESCE(v.paid_at,v.purchase_date) AS event_at,
               'Ordine fornitore' AS expense_type
          FROM variable_purchases v JOIN stores s ON s.id=v.store_id
          JOIN variable_budgets b ON b.id=v.budget_id
         WHERE {' AND '.join(direct)}
        ORDER BY event_at DESC, id DESC
    """
    rows = [dict(r) for r in conn.execute(sql, args_p + args_v).fetchall()]
    return {
        'period': period, 'anchor': anchor.isoformat(),
        'start': first.isoformat() if first else None,
        'end_exclusive': last.isoformat() if last else None,
        'total_cents': sum(int(r['amount_cents']) for r in rows),
        'count': len(rows), 'items': rows,
    }


def expense_overview(conn: sqlite3.Connection, scope: str, store_id: int | None, target: date | None = None) -> dict:
    """Cruscotto operativo: importi da pagare e tetto mensile, indipendente dalle previsioni storiche."""
    target = target or date.today()
    sync_overdue_fixed_expenses(conn, target)
    start, next_month = month_bounds(target)
    stores = conn.execute('SELECT id,name FROM stores ORDER BY name').fetchall()
    if scope != 'global':
        stores = [r for r in stores if int(r['id']) == store_id]
        if not stores:
            raise ValueError('Negozio non trovato')
    ids = [int(r['id']) for r in stores]
    by_store = []
    cards = []
    for store in stores:
        sid = int(store['id'])
        cap = conn.execute('SELECT monthly_limit_cents FROM spending_limits WHERE store_id=?', (sid,)).fetchone()
        paid, paid_count = actual_expenses_for_month(conn, sid, target)
        payments = conn.execute("SELECT * FROM payments WHERE store_id=? AND status='pending' ORDER BY due_date, id",(sid,)).fetchall()
        open_cents = sum(int(p['amount_cents']) for p in payments)
        pending_for_month = 0
        for p in payments:
            due_str = p['due_date'] or ''
            if not due_str or due_str < next_month:
                pending_for_month += int(p['amount_cents'])
            cards.append({
                'id': int(p['id']), 'kind': 'payment', 'store_id': sid, 'store_name': store['name'],
                'description': p['description'], 'amount_cents': int(p['amount_cents']),
                'due_date': p['due_date'], 'priority': int(p['priority']), 'category': p['category'] or '',
                'fixed_expense_id': p['fixed_expense_id'],
            })
        fixed_due_total = 0
        fixed_rows = conn.execute('SELECT * FROM fixed_expenses WHERE store_id=? AND active=1', (sid,)).fetchall()
        for f in fixed_rows:
            due = f['next_due_date']
            if due >= next_month and (date.fromisoformat(due) - target).days > 30:
                continue
            exists = conn.execute('SELECT 1 FROM payments WHERE fixed_expense_id=? AND due_date=? LIMIT 1', (f['id'], due)).fetchone()
            if exists:
                continue
            if due < next_month:
                fixed_due_total += int(f['amount_cents'])
            cards.append({
                'id': int(f['id']), 'kind': 'fixed', 'store_id': sid, 'store_name': store['name'],
                'description': f['description'], 'amount_cents': int(f['amount_cents']),
                'due_date': due, 'priority': 80, 'category': f['category'] or 'Spesa fissa',
            })
        monthly_limit = int(cap['monthly_limit_cents']) if cap else None
        planned_cents = paid + pending_for_month + fixed_due_total
        by_store.append({
            'id': sid, 'name': store['name'], 'limit_cents': monthly_limit,
            'paid_cents': paid, 'paid_count': paid_count, 'pending_cents': pending_for_month,
            'fixed_due_cents': fixed_due_total, 'scheduled_cents': planned_cents,
            'margin_cents': monthly_limit - planned_cents if monthly_limit is not None else None,
            'open_payments_cents': open_cents,
        })
    buckets = {'overdue': [], 'today': [], 'week': [], 'later': [], 'no_date': []}
    for c in cards:
        due_str = c['due_date']
        if not due_str:
            bucket, days = 'no_date', None
        else:
            days = (date.fromisoformat(due_str) - target).days
            bucket = 'overdue' if days < 0 else 'today' if days == 0 else 'week' if days <= 7 else 'later'
        c['days_to_due'] = days
        c['bucket'] = bucket
        buckets[bucket].append(c)
    for k in buckets:
        buckets[k].sort(key=lambda x: (x['days_to_due'] if x['days_to_due'] is not None else 999999, -x['priority'], -x['amount_cents']))
    due_soon = buckets['overdue'] + buckets['today'] + buckets['week']
    covered = all(r['limit_cents'] is not None for r in by_store) and bool(by_store)
    return {
        'today': target.isoformat(), 'scope': scope, 'stores': by_store,
        'limit_complete': covered,
        'monthly_limit_cents': sum(r['limit_cents'] or 0 for r in by_store),
        'paid_this_month_cents': sum(r['paid_cents'] for r in by_store),
        'due_this_month_cents': sum(r['pending_cents'] + r['fixed_due_cents'] for r in by_store),
        'expected_month_cents': sum(r['scheduled_cents'] for r in by_store),
        'margin_cents': sum(r['margin_cents'] or 0 for r in by_store) if covered else None,
        'open_cents': sum(r['open_payments_cents'] for r in by_store),
        'urgent_count': len(due_soon),
        'urgent_cents': sum(c['amount_cents'] for c in due_soon),
        'groups': buckets,
    }


def selection_candidates(conn):
    # Stable identity for a fixed instalment, including when it becomes overdue.
    sync_overdue_fixed_expenses(conn, date.today())
    items = []
    for row in conn.execute("SELECT p.*, s.name AS store_name FROM payments p JOIN stores s ON s.id=p.store_id WHERE p.status='pending'"):
        item = serialize_payment(row)
        item['kind'] = 'payment'
        item['selection_key'] = (f"fixed:{row['fixed_expense_id']}:{row['due_date']}" if row['fixed_expense_id'] else f"payment:{row['id']}")
        items.append(item)
    for row in conn.execute("SELECT f.*, s.name AS store_name FROM fixed_expenses f JOIN stores s ON s.id=f.store_id WHERE f.active=1"):
        if conn.execute("SELECT 1 FROM payments WHERE fixed_expense_id=? AND due_date=?", (row['id'], row['next_due_date'])).fetchone():
            continue
        item = dict(row)
        item.update(kind='fixed', due_date=row['next_due_date'], priority=80,
                    selection_key=f"fixed:{row['id']}:{row['next_due_date']}")
        items.append(item)
    return items


def selected_expenses(conn, scope, store_id):
    candidates = selection_candidates(conn)
    valid = {p['selection_key'] for p in candidates}
    keys = {r['selection_key'] for r in conn.execute('SELECT selection_key FROM expense_selections')}
    for key in keys - valid:
        conn.execute('DELETE FROM expense_selections WHERE selection_key=?', (key,))
    items = [p for p in candidates if p['selection_key'] in keys and (scope == 'global' or p['store_id'] == store_id)]
    items.sort(key=lambda p: (p['due_date'] or '9999', -p['priority'], p['id']))
    return {'items': items, 'total_cents': sum(p['amount_cents'] for p in items), 'count': len(items)}


class AppHandler(BaseHTTPRequestHandler):
    server_version = "GestionePagamenti/4.0"

    def log_message(self, fmt: str, *args) -> None:
        print(f"[{datetime.now().strftime('%H:%M:%S')}] {self.address_string()} - {fmt % args}")

    def _send_json(self, payload, status=HTTPStatus.OK):
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    def _send_bytes(self, data: bytes, content_type: str, filename: str | None = None, status=HTTPStatus.OK):
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        if filename:
            self.send_header("Content-Disposition", f'attachment; filename="{filename}"')
        self.end_headers()
        self.wfile.write(data)

    def _read_json(self):
        length = int(self.headers.get("Content-Length", "0") or 0)
        raw = self.rfile.read(length) if length else b"{}"
        try:
            return json.loads(raw.decode("utf-8") or "{}")
        except json.JSONDecodeError:
            raise ValueError("Dati JSON non validi")

    def _serve_static(self, relative_path: str):
        rel = relative_path.lstrip("/") or "index.html"
        target = (STATIC_DIR / rel).resolve()
        if STATIC_DIR.resolve() not in target.parents and target != STATIC_DIR.resolve():
            return self.send_error(HTTPStatus.FORBIDDEN)
        if not target.exists() or not target.is_file():
            return self.send_error(HTTPStatus.NOT_FOUND)
        content = target.read_bytes()
        content_type = mimetypes.guess_type(str(target))[0] or "application/octet-stream"
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(content)))
        self.send_header("Cache-Control", "no-store, no-cache, must-revalidate, max-age=0")
        self.send_header("Pragma", "no-cache")
        self.send_header("Expires", "0")
        self.end_headers()
        self.wfile.write(content)

    def do_GET(self):
        parsed = urlparse(self.path)
        path, query = parsed.path, parse_qs(parsed.query)
        try:
            if path == "/api/expenses/selected":
                scope = (query.get('scope', [''])[0] or '').lower()
                sid = None if scope == 'global' else parse_store_id(query.get('store_id', [None])[0])
                with get_db() as conn:
                    result = selected_expenses(conn, scope, sid)
                return self._send_json(result)

            if path == "/api/expenses/overview":
                scope = (query.get('scope', [''])[0] or '').lower()
                sid = None if scope == 'global' else parse_store_id(query.get('store_id', [None])[0])
                with get_db() as conn:
                    return self._send_json(expense_overview(conn, scope, sid))

            if path == "/api/expenses/history":
                scope = (query.get('scope', [''])[0] or '').lower()
                sid = None if scope == 'global' else parse_store_id(query.get('store_id', [None])[0])
                period = (query.get('period', ['month'])[0] or 'month').lower()
                raw_date = (query.get('anchor', [today_iso()])[0] or today_iso())
                try:
                    anchor = date.fromisoformat(raw_date)
                except ValueError:
                    raise ValueError('Data di riferimento non valida')
                with get_db() as conn:
                    return self._send_json(expense_history(conn, scope, sid, period, anchor))

            if path == "/api/stores":
                with get_db() as conn:
                    rows = conn.execute("SELECT * FROM stores ORDER BY name COLLATE NOCASE, id").fetchall()
                    stores = []
                    for r in rows:
                        d = store_dashboard(conn, r["id"])
                        stores.append({
                            "id": r["id"], "name": r["name"], "created_at": r["created_at"],
                            "setup_complete": d["setup_complete"], "balance_cents": d["balance_cents"],
                            "aware_funds_cents": d["aware_funds_cents"], "pending_count": d["pending_count"]
                        })
                return self._send_json({"stores": stores})

            if path == "/api/dashboard":
                scope = (query.get("scope", [""])[0] or "").lower()
                with get_db() as conn:
                    if scope == "global":
                        return self._send_json(global_dashboard(conn))
                    store_id = parse_store_id(query.get("store_id", [None])[0])
                    return self._send_json(store_dashboard(conn, store_id))

            if path == "/api/payments":
                status = (query.get("status", ["pending"])[0] or "pending").lower()
                if status not in {"pending", "paid"}:
                    raise ValueError("Stato non valido")
                month = (query.get("month", [""])[0] or "").strip()
                scope = (query.get("scope", [""])[0] or "").lower()
                params = []
                where = ["p.status=?"]
                params.append(status)
                if scope != "global":
                    store_id = parse_store_id(query.get("store_id", [None])[0])
                    where.append("p.store_id=?")
                    params.append(store_id)
                if status == "paid" and month:
                    try:
                        y, m = map(int, month.split("-")); start, nxt = month_bounds(date(y, m, 1))
                    except Exception:
                        raise ValueError("Mese non valido")
                    where.extend(["date(p.paid_at)>=?", "date(p.paid_at)<?"]); params.extend([start, nxt])
                order = "datetime(p.paid_at) DESC, p.id DESC" if status == "paid" else "CASE WHEN p.due_date IS NULL OR p.due_date='' THEN 1 ELSE 0 END, date(p.due_date) ASC, p.priority DESC, p.id DESC"
                sql = f"SELECT p.*, s.name AS store_name FROM payments p JOIN stores s ON s.id=p.store_id WHERE {' AND '.join(where)} ORDER BY {order}"
                with get_db() as conn:
                    rows = conn.execute(sql, params).fetchall()
                return self._send_json({"payments": [serialize_payment(r) for r in rows]})

            if path == "/api/recharges":
                month = (query.get("month", [""])[0] or "").strip()
                scope = (query.get("scope", [""])[0] or "").lower()
                params = []
                where = []
                if scope != "global":
                    store_id = parse_store_id(query.get("store_id", [None])[0])
                    where.append("r.store_id=?"); params.append(store_id)
                if month:
                    try:
                        y, m = map(int, month.split("-")); start, nxt = month_bounds(date(y, m, 1))
                    except Exception:
                        raise ValueError("Mese non valido")
                    where.extend(["date(r.created_at)>=?", "date(r.created_at)<?"]); params.extend([start, nxt])
                clause = " WHERE " + " AND ".join(where) if where else ""
                with get_db() as conn:
                    rows = conn.execute(f"SELECT r.*, s.name AS store_name FROM recharges r JOIN stores s ON s.id=r.store_id{clause} ORDER BY datetime(r.created_at) DESC, r.id DESC", params).fetchall()
                return self._send_json({"recharges": [{"id":r["id"],"store_id":r["store_id"],"store_name":r["store_name"],"amount_cents":r["amount_cents"],"note":r["note"],"created_at":r["created_at"]} for r in rows]})

            if path == "/api/fixed-expenses":
                scope = (query.get("scope", [""])[0] or "").lower()
                params = []
                where = ["f.active=1"]
                if scope != "global":
                    store_id = parse_store_id(query.get("store_id", [None])[0])
                    where.append("f.store_id=?"); params.append(store_id)
                with get_db() as conn:
                    rows = conn.execute(
                        f"SELECT f.*, s.name AS store_name FROM fixed_expenses f JOIN stores s ON s.id=f.store_id WHERE {' AND '.join(where)} ORDER BY date(f.next_due_date), f.id",
                        params,
                    ).fetchall()
                    items = [serialize_fixed_expense(r, date.today()) for r in rows]
                return self._send_json({"fixed_expenses": items})

            if path == "/api/variable-budgets":
                scope = (query.get("scope", [""])[0] or "").lower()
                params=[]; where=["v.active=1"]
                if scope != "global":
                    store_id=parse_store_id(query.get("store_id",[None])[0]); where.append("v.store_id=?"); params.append(store_id)
                with get_db() as conn:
                    rows=conn.execute(f"SELECT v.*, s.name AS store_name FROM variable_budgets v JOIN stores s ON s.id=v.store_id WHERE {' AND '.join(where)} ORDER BY s.name, v.description", params).fetchall()
                    items=[serialize_variable_budget(conn,r,date.today()) for r in rows]
                return self._send_json({"variable_budgets":items})

            if path in {"/api/variable-purchases", "/api/variable-orders"}:
                budget_id = int(query.get("budget_id",[0])[0] or 0)
                month=(query.get("month",[date.today().strftime("%Y-%m")])[0] or date.today().strftime("%Y-%m")).strip()
                try:
                    y,m=map(int,month.split("-")); start,nxt=month_bounds(date(y,m,1))
                except Exception: raise ValueError("Mese non valido")
                with get_db() as conn:
                    rows=conn.execute("SELECT p.*, v.description AS budget_description, s.name AS store_name FROM variable_purchases p JOIN variable_budgets v ON v.id=p.budget_id JOIN stores s ON s.id=p.store_id WHERE p.budget_id=? AND date(p.purchase_date)>=? AND date(p.purchase_date)<? ORDER BY date(p.purchase_date) DESC, p.id DESC",(budget_id,start,nxt)).fetchall()
                items=[dict(r) for r in rows]
                return self._send_json({"orders":items, "purchases":items})

            if path == "/api/export.csv":
                scope=(query.get("scope",[""])[0] or "").lower(); params=[]; store_clause=""
                if scope != "global":
                    store_id=parse_store_id(query.get("store_id",[None])[0]); store_clause=" AND x.store_id=?"; params=[store_id]
                out=io.StringIO(); w=csv.writer(out,delimiter=';',lineterminator='\n')
                w.writerow(["Data","Negozio","Tipo","Descrizione","Categoria","Importo stimato","Importo reale","Stato","Scadenza","Note"])
                with get_db() as conn:
                    sync_overdue_fixed_expenses(conn,date.today())
                    psql="SELECT x.*,s.name AS store_name FROM payments x JOIN stores s ON s.id=x.store_id WHERE NOT EXISTS (SELECT 1 FROM variable_purchases vp WHERE vp.payment_id=x.id)"+store_clause+" ORDER BY COALESCE(x.paid_at,x.created_at)"
                    for r in conn.execute(psql,params).fetchall():
                        estimated=r["estimated_amount_cents"] if "estimated_amount_cents" in r.keys() and r["estimated_amount_cents"] else (r["amount_cents"] if r["fixed_expense_id"] else None)
                        w.writerow([(r["paid_at"] or r["created_at"] or "")[:10],r["store_name"],"Spesa fissa" if r["fixed_expense_id"] else "Pagamento",r["description"],r["category"],"" if estimated is None else f"{estimated/100:.2f}".replace('.',','),f"{r['amount_cents']/100:.2f}".replace('.',',') if r["status"]=='paid' else "", "Pagato" if r["status"]=='paid' else "Da pagare",r["due_date"] or "",r["notes"] or ""])
                    vparams=[]; vclause=""
                    if scope != "global": vclause=" WHERE p.store_id=?"; vparams=[store_id]
                    vsql="SELECT p.*,v.description,v.category,s.name AS store_name, pay.amount_cents AS paid_amount FROM variable_purchases p JOIN variable_budgets v ON v.id=p.budget_id JOIN stores s ON s.id=p.store_id LEFT JOIN payments pay ON pay.id=p.payment_id"+vclause+" ORDER BY p.purchase_date"
                    for r in conn.execute(vsql,vparams).fetchall():
                        is_paid=(r["status"]=="paid")
                        actual=(r["paid_amount"] if r["payment_id"] and r["paid_amount"] is not None else (r["amount_cents"] if is_paid else None))
                        w.writerow([r["purchase_date"],r["store_name"],"Ordine variabile",r["description"],r["category"],f"{r['amount_cents']/100:.2f}".replace('.',','),"" if actual is None else f"{actual/100:.2f}".replace('.',','),"Pagato" if is_paid else "Da pagare",r["due_date"] or "",r["note"] or ""])
                    vbparams=[]; vbclause=" WHERE v.active=1"
                    if scope != "global": vbclause+=" AND v.store_id=?"; vbparams=[store_id]
                    for r in conn.execute("SELECT v.*,s.name AS store_name FROM variable_budgets v JOIN stores s ON s.id=v.store_id"+vbclause+" ORDER BY s.name,v.description",vbparams).fetchall():
                        w.writerow([date.today().replace(day=1).isoformat(),r["store_name"],"Budget variabile previsto",r["description"],r["category"],f"{r['monthly_budget_cents']/100:.2f}".replace('.',','),"","Previsto mese","",r["notes"] or ""])
                    fparams=[]; fclause=" WHERE f.active=1"
                    if scope != "global": fclause+=" AND f.store_id=?"; fparams=[store_id]
                    for r in conn.execute("SELECT f.*,s.name AS store_name FROM fixed_expenses f JOIN stores s ON s.id=f.store_id"+fclause+" ORDER BY f.next_due_date",fparams).fetchall():
                        w.writerow(["",r["store_name"],"Spesa fissa prevista",r["description"],r["category"],f"{r['amount_cents']/100:.2f}".replace('.',','),"","Prevista",r["next_due_date"],r["notes"] or ""])
                data=('\ufeff'+out.getvalue()).encode('utf-8')
                return self._send_bytes(data,'text/csv; charset=utf-8',f"gestione_spese_{date.today().isoformat()}.csv")

            if path == "/api/health":
                return self._send_json({"ok": True, "version": "3.2", "db": str(DB_PATH)})
            if path == "/":
                return self._serve_static("index.html")
            if path.startswith("/static/"):
                return self._serve_static(path[len("/static/"):])
            self.send_error(HTTPStatus.NOT_FOUND)
        except ValueError as exc:
            self._send_json({"error": str(exc)}, HTTPStatus.BAD_REQUEST)
        except Exception as exc:
            print("Errore GET:", repr(exc))
            self._send_json({"error": "Errore interno del programma"}, HTTPStatus.INTERNAL_SERVER_ERROR)

    def do_POST(self):
        parsed = urlparse(self.path); path = parsed.path
        try:
            payload = self._read_json()

            if path == "/api/expenses/selection":
                key = str(payload.get('key') or '')
                selected = payload.get('selected')
                if not isinstance(selected, bool):
                    raise ValueError('Selezione non valida')
                with get_db() as conn:
                    if selected:
                        if key not in {p['selection_key'] for p in selection_candidates(conn)}:
                            raise ValueError('Spesa non più disponibile: aggiorna la pagina')
                        conn.execute('INSERT OR IGNORE INTO expense_selections VALUES (?,?)', (key, now_iso()))
                    else:
                        conn.execute('DELETE FROM expense_selections WHERE selection_key=?', (key,))
                return self._send_json({'ok': True})

            if path == "/api/expenses/limit":
                sid = parse_store_id(payload.get('store_id'))
                amount = money_to_cents(payload.get('monthly_limit'))
                with get_db() as conn:
                    get_store(conn, sid)
                    conn.execute(
                        """INSERT INTO spending_limits(store_id,monthly_limit_cents,updated_at)
                           VALUES(?,?,?) ON CONFLICT(store_id) DO UPDATE SET
                           monthly_limit_cents=excluded.monthly_limit_cents, updated_at=excluded.updated_at""",
                        (sid, amount, now_iso()),
                    )
                return self._send_json({'ok': True, 'store_id': sid, 'monthly_limit_cents': amount})

            if path == "/api/stores":
                name = (payload.get("name") or "").strip()
                if len(name) < 2:
                    raise ValueError("Inserisci il nome del negozio")
                if len(name) > 80:
                    raise ValueError("Nome negozio troppo lungo")
                with get_db() as conn:
                    exists = conn.execute("SELECT 1 FROM stores WHERE lower(name)=lower(?)", (name,)).fetchone()
                    if exists:
                        return self._send_json({"error":"Esiste già un negozio con questo nome"}, HTTPStatus.CONFLICT)
                    cur = conn.execute("INSERT INTO stores(name, created_at) VALUES(?,?)", (name, now_iso()))
                    sid = cur.lastrowid
                return self._send_json({"ok":True,"store":{"id":sid,"name":name,"setup_complete":False}}, HTTPStatus.CREATED)

            if path == "/api/setup":
                store_id = parse_store_id(payload.get("store_id"))
                previous_total = money_to_cents(payload.get("previous_month_total"))
                initial_amount = money_to_cents(payload.get("initial_amount"), allow_zero=True)
                prev_key = previous_month(date.today()).strftime("%Y-%m")
                with get_db() as conn:
                    get_store(conn, store_id)
                    initial_exists = conn.execute("SELECT 1 FROM movements WHERE store_id=? AND movement_type='initial' LIMIT 1", (store_id,)).fetchone()
                    report_exists = conn.execute("SELECT 1 FROM baseline_expenses WHERE store_id=? LIMIT 1", (store_id,)).fetchone()
                    if initial_exists or report_exists:
                        return self._send_json({"error":"Questo negozio è già stato configurato"}, HTTPStatus.CONFLICT)
                    conn.execute("INSERT INTO movements(store_id,movement_type,amount_cents,note,created_at) VALUES(?,'initial',?,?,?)", (store_id, initial_amount, "Saldo iniziale", now_iso()))
                    conn.execute("INSERT INTO baseline_expenses(store_id,report_month,description,amount_cents,category,notes,created_at) VALUES(?,?,?,?,?,?,?)", (store_id, prev_key, "Totale spese mese scorso", previous_total, "", "Dato iniziale", now_iso()))
                    dash = store_dashboard(conn, store_id)
                return self._send_json({"ok":True,"dashboard":dash}, HTTPStatus.CREATED)

            if path == "/api/recharges":
                store_id = parse_store_id(payload.get("store_id")); amount = money_to_cents(payload.get("amount")); note = (payload.get("note") or "").strip()[:250]; created_at = now_iso()
                with get_db() as conn:
                    get_store(conn, store_id)
                    cur = conn.execute("INSERT INTO recharges(store_id,amount_cents,note,created_at) VALUES(?,?,?,?)", (store_id, amount, note, created_at)); rid = cur.lastrowid
                    conn.execute("INSERT INTO movements(store_id,movement_type,amount_cents,ref_id,note,created_at) VALUES(?,'recharge',?,?,?,?)", (store_id, amount, rid, note, created_at))
                return self._send_json({"ok":True,"id":rid}, HTTPStatus.CREATED)

            if path == "/api/fixed-expenses":
                store_id = parse_store_id(payload.get("store_id"))
                description = (payload.get("description") or "").strip()
                if not description:
                    raise ValueError("Inserisci una descrizione")
                amount = money_to_cents(payload.get("amount"))
                try:
                    frequency_months = int(payload.get("frequency_months"))
                except Exception:
                    raise ValueError("Frequenza non valida")
                if frequency_months not in FREQUENCY_LABELS:
                    raise ValueError("Frequenza non valida")
                next_due_date = (payload.get("next_due_date") or "").strip()
                try:
                    due = date.fromisoformat(next_due_date)
                except ValueError:
                    raise ValueError("Inserisci una prossima scadenza valida")
                category = (payload.get("category") or "").strip()[:80]
                notes = (payload.get("notes") or "").strip()[:1000]
                with get_db() as conn:
                    get_store(conn, store_id)
                    cur = conn.execute(
                        """INSERT INTO fixed_expenses(store_id,description,amount_cents,frequency_months,next_due_date,category,notes,active,created_at)
                           VALUES(?,?,?,?,?,?,?,1,?)""",
                        (store_id, description, amount, frequency_months, due.isoformat(), category, notes, now_iso()),
                    )
                    fid = cur.lastrowid
                return self._send_json({"ok": True, "id": fid}, HTTPStatus.CREATED)

            if path.startswith("/api/fixed-expenses/") and path.endswith("/pay"):
                parts = path.strip("/").split("/")
                if len(parts) != 4:
                    raise ValueError("Percorso non valido")
                fid = int(parts[2])
                paid_date = (payload.get("paid_date") or today_iso()).strip()
                try:
                    paid_day = date.fromisoformat(paid_date)
                except ValueError:
                    raise ValueError("Data pagamento non valida")
                paid_at = f"{paid_date} {datetime.now().strftime('%H:%M:%S')}"
                actual_amount = money_to_cents(payload.get("actual_amount"))
                with get_db() as conn:
                    row = conn.execute("SELECT * FROM fixed_expenses WHERE id=? AND active=1", (fid,)).fetchone()
                    if not row:
                        return self._send_json({"error": "Spesa fissa non trovata"}, HTTPStatus.NOT_FOUND)
                    old_due = date.fromisoformat(row["next_due_date"])
                    next_due = add_months(old_due, int(row["frequency_months"]))
                    # Se la rata era molto arretrata, mantiene un ciclo alla volta: il programma
                    # rende visibile subito l'eventuale rata successiva ancora scaduta.
                    cur = conn.execute(
                        """INSERT INTO payments(store_id,description,amount_cents,estimated_amount_cents,due_date,category,priority,notes,status,created_at,paid_at,fixed_expense_id)
                           VALUES(?,?,?,?,?,?,95,?,'paid',?,?,?)""",
                        (row["store_id"], row["description"], actual_amount, row["amount_cents"], old_due.isoformat(),
                         row["category"] or "Spesa fissa", row["notes"] or "Rata spesa fissa", paid_at, paid_at, fid),
                    )
                    pid = cur.lastrowid
                    conn.execute(
                        "INSERT INTO movements(store_id,movement_type,amount_cents,ref_id,note,created_at) VALUES(?,'payment',?,?,?,?)",
                        (row["store_id"], -actual_amount, pid, f"Spesa fissa: {row['description']}", paid_at),
                    )
                    conn.execute("UPDATE fixed_expenses SET next_due_date=? WHERE id=?", (next_due.isoformat(), fid))
                return self._send_json({"ok": True, "next_due_date": next_due.isoformat()})

            if path == "/api/variable-budgets":
                store_id=parse_store_id(payload.get("store_id")); description=(payload.get("description") or "").strip()
                if not description: raise ValueError("Inserisci il nome del fornitore o della spesa")
                budget=money_to_cents(payload.get("monthly_budget")); category=(payload.get("category") or "").strip()[:80]; notes=(payload.get("notes") or "").strip()[:1000]
                with get_db() as conn:
                    get_store(conn,store_id); cur=conn.execute("INSERT INTO variable_budgets(store_id,description,monthly_budget_cents,category,notes,active,created_at) VALUES(?,?,?,?,?,1,?)",(store_id,description,budget,category,notes,now_iso())); bid=cur.lastrowid
                return self._send_json({"ok":True,"id":bid},HTTPStatus.CREATED)

            if path.startswith("/api/variable-budgets/") and (path.endswith("/purchases") or path.endswith("/orders")):
                parts=path.strip('/').split('/'); bid=int(parts[2])
                amount=money_to_cents(payload.get("amount"))
                purchase_date=(payload.get("purchase_date") or payload.get("order_date") or today_iso()).strip()
                note=(payload.get("note") or "").strip()[:500]
                payment_status=(payload.get("payment_status") or "paid").strip().lower()
                if payment_status not in {"paid","pending"}:
                    raise ValueError("Stato pagamento non valido")
                try: order_day=date.fromisoformat(purchase_date)
                except ValueError: raise ValueError("Data ordine non valida")
                due_date=(payload.get("due_date") or "").strip() or None
                if payment_status=="pending":
                    if not due_date: raise ValueError("Inserisci la data di scadenza")
                    try: due_day=date.fromisoformat(due_date)
                    except ValueError: raise ValueError("Data di scadenza non valida")
                    if due_day < order_day: raise ValueError("La scadenza non può essere prima della data dell'ordine")
                created_at=f"{purchase_date} {datetime.now().strftime('%H:%M:%S')}"
                with get_db() as conn:
                    b=conn.execute("SELECT * FROM variable_budgets WHERE id=? AND active=1",(bid,)).fetchone()
                    if not b: return self._send_json({"error":"Spesa variabile non trovata"},HTTPStatus.NOT_FOUND)
                    cur=conn.execute(
                        """INSERT INTO variable_purchases(budget_id,store_id,amount_cents,purchase_date,note,created_at,status,due_date,paid_at)
                           VALUES(?,?,?,?,?,?,?,?,?)""",
                        (bid,b["store_id"],amount,purchase_date,note,created_at,payment_status,due_date,created_at if payment_status=="paid" else None)
                    )
                    oid=cur.lastrowid
                    payment_id=None
                    if payment_status=="paid":
                        conn.execute(
                            "INSERT INTO movements(store_id,movement_type,amount_cents,ref_id,note,created_at) VALUES(?,'payment',?,?,?,?)",
                            (b["store_id"],-amount,-oid,f"Ordine variabile: {b['description']}",created_at)
                        )
                    else:
                        payment_note=(f"Ordine da {b['description']}" + (f" • {note}" if note else ""))[:1000]
                        pcur=conn.execute(
                            """INSERT INTO payments(store_id,description,amount_cents,due_date,category,priority,notes,status,created_at)
                               VALUES(?,?,?,?,?,70,?,'pending',?)""",
                            (b["store_id"],f"{b['description']} - ordine",amount,due_date,b["category"] or "Fornitore",payment_note,created_at)
                        )
                        payment_id=pcur.lastrowid
                        conn.execute("UPDATE variable_purchases SET payment_id=? WHERE id=?",(payment_id,oid))
                return self._send_json({"ok":True,"id":oid,"payment_id":payment_id,"status":payment_status},HTTPStatus.CREATED)

            if path == "/api/payments":
                store_id = parse_store_id(payload.get("store_id")); description = (payload.get("description") or "").strip()
                if not description: raise ValueError("Inserisci una descrizione")
                amount = money_to_cents(payload.get("amount")); due_date = (payload.get("due_date") or "").strip() or None
                if due_date:
                    try: date.fromisoformat(due_date)
                    except ValueError: raise ValueError("Data di scadenza non valida")
                category = (payload.get("category") or "").strip()[:80]; notes = (payload.get("notes") or "").strip()[:1000]
                try: priority = int(payload.get("priority",50))
                except Exception: priority = 50
                if not 1 <= priority <= 100: raise ValueError("La priorità deve essere tra 1 e 100")
                with get_db() as conn:
                    get_store(conn, store_id)
                    cur = conn.execute("INSERT INTO payments(store_id,description,amount_cents,due_date,category,priority,notes,status,created_at) VALUES(?,?,?,?,?,?,?,'pending',?)", (store_id,description,amount,due_date,category,priority,notes,now_iso())); pid = cur.lastrowid
                return self._send_json({"ok":True,"id":pid}, HTTPStatus.CREATED)

            if path.startswith("/api/payments/") and path.endswith("/pay"):
                parts = path.strip("/").split("/")
                if len(parts) != 4: raise ValueError("Percorso non valido")
                pid = int(parts[2]); paid_date = (payload.get("paid_date") or today_iso()).strip()
                try: date.fromisoformat(paid_date)
                except ValueError: raise ValueError("Data pagamento non valida")
                paid_at = f"{paid_date} {datetime.now().strftime('%H:%M:%S')}"
                with get_db() as conn:
                    row = conn.execute("SELECT * FROM payments WHERE id=?", (pid,)).fetchone()
                    if not row: return self._send_json({"error":"Pagamento non trovato"}, HTTPStatus.NOT_FOUND)
                    if row["status"] == "paid": return self._send_json({"error":"Pagamento già segnato come pagato"}, HTTPStatus.CONFLICT)
                    actual_amount = money_to_cents(payload.get("actual_amount")) if payload.get("actual_amount") not in (None, "") else int(row["amount_cents"])
                    estimated = row["estimated_amount_cents"] if "estimated_amount_cents" in row.keys() and row["estimated_amount_cents"] else (int(row["amount_cents"]) if actual_amount != int(row["amount_cents"]) else None)
                    conn.execute("UPDATE payments SET status='paid', paid_at=?, amount_cents=?, estimated_amount_cents=? WHERE id=?", (paid_at,actual_amount,estimated,pid))
                    conn.execute("INSERT INTO movements(store_id,movement_type,amount_cents,ref_id,note,created_at) VALUES(?,'payment',?,?,?,?)", (row["store_id"],-actual_amount,pid,row["description"],paid_at))
                    conn.execute("UPDATE variable_purchases SET status='paid', paid_at=? WHERE payment_id=?", (paid_at,pid))
                return self._send_json({"ok":True})

            self.send_error(HTTPStatus.NOT_FOUND)
        except ValueError as exc:
            self._send_json({"error":str(exc)}, HTTPStatus.BAD_REQUEST)
        except Exception as exc:
            print("Errore POST:", repr(exc)); self._send_json({"error":"Errore interno del programma"}, HTTPStatus.INTERNAL_SERVER_ERROR)

    def do_DELETE(self):
        path = urlparse(self.path).path
        try:
            if path.startswith("/api/stores/"):
                parts=path.strip('/').split('/'); sid=int(parts[2])
                with get_db() as conn:
                    if not conn.execute("SELECT 1 FROM stores WHERE id=?",(sid,)).fetchone(): return self._send_json({"error":"Negozio non trovato"},HTTPStatus.NOT_FOUND)
                    conn.execute("DELETE FROM spending_limits WHERE store_id=?",(sid,)); conn.execute("DELETE FROM variable_purchases WHERE store_id=?",(sid,)); conn.execute("DELETE FROM variable_budgets WHERE store_id=?",(sid,)); conn.execute("DELETE FROM fixed_expenses WHERE store_id=?",(sid,)); conn.execute("DELETE FROM payments WHERE store_id=?",(sid,)); conn.execute("DELETE FROM recharges WHERE store_id=?",(sid,)); conn.execute("DELETE FROM movements WHERE store_id=?",(sid,)); conn.execute("DELETE FROM baseline_expenses WHERE store_id=?",(sid,)); conn.execute("DELETE FROM stores WHERE id=?",(sid,))
                return self._send_json({"ok":True})

            if path.startswith("/api/variable-purchases/") or path.startswith("/api/variable-orders/"):
                parts=path.strip('/').split('/'); oid=int(parts[2])
                with get_db() as conn:
                    row=conn.execute("SELECT * FROM variable_purchases WHERE id=?",(oid,)).fetchone()
                    if not row: return self._send_json({"error":"Ordine non trovato"},HTTPStatus.NOT_FOUND)
                    if row["payment_id"]:
                        pay=conn.execute("SELECT * FROM payments WHERE id=?",(row["payment_id"],)).fetchone()
                        if pay and pay["status"]=="paid":
                            conn.execute("DELETE FROM movements WHERE movement_type='payment' AND ref_id=?",(pay["id"],))
                        conn.execute("DELETE FROM payments WHERE id=?",(row["payment_id"],))
                    else:
                        conn.execute("DELETE FROM movements WHERE movement_type='payment' AND ref_id=?",(-oid,))
                    conn.execute("DELETE FROM variable_purchases WHERE id=?",(oid,))
                return self._send_json({"ok":True})

            if path.startswith("/api/variable-budgets/"):
                parts=path.strip('/').split('/'); bid=int(parts[2])
                with get_db() as conn:
                    row=conn.execute("SELECT id FROM variable_budgets WHERE id=? AND active=1",(bid,)).fetchone()
                    if not row: return self._send_json({"error":"Spesa variabile non trovata"},HTTPStatus.NOT_FOUND)
                    conn.execute("UPDATE variable_budgets SET active=0 WHERE id=?",(bid,))
                return self._send_json({"ok":True})

            if path.startswith("/api/fixed-expenses/"):
                parts = path.strip("/").split("/")
                if len(parts) != 3:
                    raise ValueError("Percorso non valido")
                fid = int(parts[2])
                with get_db() as conn:
                    row = conn.execute("SELECT id FROM fixed_expenses WHERE id=? AND active=1", (fid,)).fetchone()
                    if not row:
                        return self._send_json({"error": "Spesa fissa non trovata"}, HTTPStatus.NOT_FOUND)
                    conn.execute("UPDATE fixed_expenses SET active=0 WHERE id=?", (fid,))
                return self._send_json({"ok": True})

            if path.startswith("/api/payments/"):
                parts = path.strip("/").split("/")
                if len(parts) != 3: raise ValueError("Percorso non valido")
                pid = int(parts[2])
                with get_db() as conn:
                    row = conn.execute("SELECT status FROM payments WHERE id=?", (pid,)).fetchone()
                    if not row: return self._send_json({"error":"Pagamento non trovato"}, HTTPStatus.NOT_FOUND)
                    if row["status"] == "paid":
                        conn.execute("DELETE FROM movements WHERE movement_type='payment' AND ref_id=?", (pid,))
                    linked=conn.execute("SELECT id FROM variable_purchases WHERE payment_id=?",(pid,)).fetchone()
                    if linked:
                        conn.execute("DELETE FROM variable_purchases WHERE id=?",(linked["id"],))
                    conn.execute("DELETE FROM payments WHERE id=?", (pid,))
                return self._send_json({"ok":True})
            self.send_error(HTTPStatus.NOT_FOUND)
        except ValueError as exc:
            self._send_json({"error":str(exc)}, HTTPStatus.BAD_REQUEST)
        except Exception as exc:
            print("Errore DELETE:", repr(exc)); self._send_json({"error":"Errore interno del programma"}, HTTPStatus.INTERNAL_SERVER_ERROR)


def open_browser(port: int):
    webbrowser.open(f"http://{HOST}:{port}/?fresh=control")


def create_server_on_free_port():
    last_error = None
    for port in range(PORT_START, PORT_END + 1):
        try:
            return ThreadingHTTPServer((HOST, port), AppHandler), port
        except OSError as exc:
            last_error = exc
    raise OSError(f"Nessuna porta libera tra {PORT_START} e {PORT_END}") from last_error


def main():
    init_db()
    server, port = create_server_on_free_port()
    print("=" * 62)
    print("GESTIONE PAGAMENTI")
    print("PAGAMENTI | SPESE FISSE | FORNITORI | BUDGET MENSILI | STORICO")
    print(f"Aperto su: http://{HOST}:{port}/?fresh=control")
    print("Per chiudere il programma: chiudi questa finestra o premi CTRL+C")
    print("=" * 62)
    threading.Timer(0.7, lambda: open_browser(port)).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nChiusura programma...")
    finally:
        server.server_close()


