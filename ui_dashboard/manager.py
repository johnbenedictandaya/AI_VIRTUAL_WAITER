"""manager.py - PIN-protected Manager mode for the Perpetual Bistro dashboard.

Adds, on top of the floor map and your existing tables:
  * inventory control : set / restock / waste / 86 an item, prices, reorder levels, unit costs, stock history
  * sales reports      : by day, hour, category, item and table, cancelled/voided orders, CSV export
  * order desk         : browse every order, void a confirmed one (stock is put back and logged)
  * team & costs       : staff list with clock in/out, labour cost, expenses, and a simple profit & loss
  * alerts + settings  : low stock, slow kitchen tickets, long-sitting tables, late bookings, PIN change

It only ADDS tables (CREATE TABLE IF NOT EXISTS) and never changes your menu/session schema.

Wire-up (done in app.py / templates/dashboard.html):
    from ui_dashboard.manager import register_manager
    register_manager(app)
    <script src="/static/manager.js"></script>        # after /static/floor_plan.js

Default PIN is 1234 (override the first-run value with env BISTRO_MANAGER_PIN) - change it in Manager > Settings.
Heads-up: db_tools.init_db() rebuilds restaurant.db when PRAGMA user_version in the .sql changes; that resets the menu
to the SQL seed and therefore discards manager edits to items/stock. Do not bump it casually once you manage live data.
"""
import csv
import hashlib
import io
import os
import secrets
import time
from datetime import date, datetime, timedelta
from functools import wraps

from flask import Blueprint, Response, jsonify, redirect, request

import db_tools as db
from ui_dashboard import floor_plan as floor
from ui_dashboard.floor_plan import FloorError, _int

mgr_bp = Blueprint("manager", __name__)

TOKEN_IDLE_SECONDS = 60 * 60
MAX_FAILS, LOCK_SECONDS = 5, 30
_tokens: dict = {}                      # token -> last-seen epoch
_fails = {"n": 0, "until": 0.0}

SCHEMA = """
CREATE TABLE IF NOT EXISTS mgr_settings (key TEXT PRIMARY KEY, value TEXT);
CREATE TABLE IF NOT EXISTS item_settings (
    item_id       INTEGER PRIMARY KEY REFERENCES menu_items(id) ON DELETE CASCADE,
    reorder_level INTEGER NOT NULL DEFAULT 5 CHECK (reorder_level >= 0),
    par_level     INTEGER CHECK (par_level IS NULL OR par_level >= 0),
    unit_cost     REAL    CHECK (unit_cost IS NULL OR unit_cost >= 0)
);
CREATE TABLE IF NOT EXISTS stock_log (
    log_id      INTEGER PRIMARY KEY AUTOINCREMENT,
    item_id     INTEGER NOT NULL REFERENCES menu_items(id),
    delta       INTEGER NOT NULL,
    reason      TEXT    NOT NULL,
    note        TEXT    NOT NULL DEFAULT '',
    stock_after INTEGER NOT NULL,
    at          TEXT    NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE TABLE IF NOT EXISTS staff (
    staff_id    INTEGER PRIMARY KEY AUTOINCREMENT,
    name        TEXT NOT NULL,
    role        TEXT NOT NULL DEFAULT 'Server',
    hourly_rate REAL NOT NULL DEFAULT 0 CHECK (hourly_rate >= 0),
    active      INTEGER NOT NULL DEFAULT 1
);
CREATE TABLE IF NOT EXISTS shifts (
    shift_id  INTEGER PRIMARY KEY AUTOINCREMENT,
    staff_id  INTEGER NOT NULL REFERENCES staff(staff_id) ON DELETE CASCADE,
    clock_in  TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    clock_out TEXT
);
CREATE TABLE IF NOT EXISTS expenses (
    exp_id   INTEGER PRIMARY KEY AUTOINCREMENT,
    spent_on TEXT NOT NULL,                              -- local date YYYY-MM-DD
    category TEXT NOT NULL,
    note     TEXT NOT NULL DEFAULT '',
    amount   REAL NOT NULL CHECK (amount >= 0)
);
CREATE TABLE IF NOT EXISTS void_log (
    session_id INTEGER PRIMARY KEY REFERENCES table_sessions(session_id),
    reason     TEXT NOT NULL DEFAULT '',
    amount     REAL NOT NULL DEFAULT 0,
    at         TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
"""


# ---------------------------------------------------------------- auth
def _hash(pin, salt):
    return hashlib.pbkdf2_hmac("sha256", pin.encode(), salt.encode(), 120_000).hex()


def ensure_schema():
    floor.ensure_schema()
    with db.db() as c:
        if c.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='mgr_settings'").fetchone():
            return
        c.executescript(SCHEMA)
        salt = secrets.token_hex(8)
        pin = os.environ.get("BISTRO_MANAGER_PIN", "1234")
        c.executemany("INSERT INTO mgr_settings VALUES (?,?)",
                      [("pin_salt", salt), ("pin_hash", _hash(pin, salt)), ("pin_is_default", "1" if pin == "1234" else "0")])


def _setting(c, key, default=""):
    r = c.execute("SELECT value FROM mgr_settings WHERE key=?", (key,)).fetchone()
    return r[0] if r else default


def manager_only(fn):
    @wraps(fn)
    def wrapper(*a, **kw):
        ensure_schema()
        tok, now = request.headers.get("X-Manager-Token", ""), time.time()
        if tok not in _tokens or now - _tokens[tok] > TOKEN_IDLE_SECONDS:
            _tokens.pop(tok, None)
            return jsonify({"ok": False, "error": "Manager sign-in required", "auth": False}), 401
        _tokens[tok] = now
        try:
            return fn(*a, **kw)
        except FloorError as e:
            return jsonify({"ok": False, "error": str(e)}), 400
    return wrapper


def _body():
    return request.get_json(silent=True) or {}


@mgr_bp.post("/api/manager/login")
def login():
    ensure_schema()
    now = time.time()
    if now < _fails["until"]:
        return jsonify({"ok": False, "error": f"Too many attempts. Wait {int(_fails['until'] - now) + 1}s."}), 429
    pin = str(_body().get("pin", ""))
    with db.db() as c:
        good = secrets.compare_digest(_hash(pin, _setting(c, "pin_salt")), _setting(c, "pin_hash"))
        default = _setting(c, "pin_is_default") == "1"
    if not good:
        _fails["n"] += 1
        if _fails["n"] >= MAX_FAILS:
            _fails.update(n=0, until=now + LOCK_SECONDS)
        return jsonify({"ok": False, "error": "Wrong PIN"}), 401
    _fails["n"] = 0
    tok = secrets.token_urlsafe(24)
    _tokens[tok] = now
    return jsonify({"ok": True, "token": tok, "default_pin": default})


@mgr_bp.post("/api/manager/logout")
def logout():
    _tokens.pop(request.headers.get("X-Manager-Token", ""), None)
    return jsonify({"ok": True})


@mgr_bp.post("/api/manager/pin")
@manager_only
def change_pin():
    d = _body()
    new = str(d.get("new", ""))
    if not (4 <= len(new) <= 12 and new.isdigit()):
        raise FloorError("New PIN must be 4-12 digits")
    with db.db() as c:
        if not secrets.compare_digest(_hash(str(d.get("old", "")), _setting(c, "pin_salt")), _setting(c, "pin_hash")):
            raise FloorError("Current PIN is wrong")
        salt = secrets.token_hex(8)
        for k, v in (("pin_salt", salt), ("pin_hash", _hash(new, salt)), ("pin_is_default", "0")):
            c.execute("INSERT OR REPLACE INTO mgr_settings VALUES (?,?)", (k, v))
    return jsonify({"ok": True})


# ---------------------------------------------------------------- shared queries
LOCAL_DAY = "date(s.created_at,'localtime')"


def _range(key):
    today = date.today()
    days = {"today": 0, "7d": 6, "30d": 29}.get(key)
    start = (today - timedelta(days=days)).isoformat() if days is not None else "0000-01-01"
    return start, today.isoformat()


def _disc_rows(c, start, end):
    """Confirmed sessions in [start, end] that used a discount: [(day, hour, table_no, amount_php)].
    Menu prices x quantity are the gross; the discount comes off before the service fee, same as the receipt."""
    out = []
    for r in c.execute(f"""SELECT {LOCAL_DAY} AS d, CAST(strftime('%H', s.created_at,'localtime') AS INTEGER) AS h, s.table_no,
                                  CASE d.kind WHEN 'percent' THEN ROUND(t.sub * d.value / 100.0, 2) ELSE MIN(d.value, t.sub) END AS amt
                           FROM table_sessions s JOIN session_discounts sd ON sd.session_id = s.session_id
                           JOIN discounts d ON d.code = sd.code
                           JOIN (SELECT o.session_id, SUM(o.quantity * m.price) AS sub FROM order_line_items o
                                 JOIN menu_items m ON m.id = o.item_id GROUP BY o.session_id) t ON t.session_id = s.session_id
                           WHERE s.status = 'CONFIRMED' AND {LOCAL_DAY} BETWEEN ? AND ?""", (start, end)):
        out.append((r["d"], r["h"], r["table_no"], r["amt"] or 0.0))
    return out


def _disc_by(rows, idx):
    """Sum the discount amounts by day (0), hour (1) or table (2)."""
    tot = {}
    for r in rows:
        tot[r[idx]] = tot.get(r[idx], 0.0) + r[3]
    return tot


def _labor(c, start, end):
    """Wage cost of shifts that began in [start, end] (open shifts run until now)."""
    total = 0.0
    for r in c.execute("""SELECT st.hourly_rate AS rate, sh.clock_in, sh.clock_out FROM shifts sh
                          JOIN staff st ON st.staff_id = sh.staff_id
                          WHERE date(sh.clock_in,'localtime') BETWEEN ? AND ?""", (start, end)):
        a = datetime.strptime(r["clock_in"], "%Y-%m-%d %H:%M:%S")
        b = datetime.strptime(r["clock_out"], "%Y-%m-%d %H:%M:%S") if r["clock_out"] else datetime.utcnow()
        total += max((b - a).total_seconds(), 0) / 3600 * r["rate"]
    return round(total, 2)


def pnl(c, start, end):
    rate = db.SERVICE_RATE
    r = c.execute(f"""SELECT COUNT(DISTINCT s.session_id) AS orders, COALESCE(SUM(o.quantity*m.price),0) AS food,
                             COALESCE(SUM(CASE WHEN i.unit_cost IS NOT NULL THEN o.quantity*i.unit_cost END),0) AS cogs,
                             COALESCE(SUM(CASE WHEN i.unit_cost IS NOT NULL THEN o.quantity*m.price END),0) AS costed_sales
                      FROM table_sessions s JOIN order_line_items o ON o.session_id=s.session_id
                      JOIN menu_items m ON m.id=o.item_id LEFT JOIN item_settings i ON i.item_id=m.id
                      WHERE s.status='CONFIRMED' AND {LOCAL_DAY} BETWEEN ? AND ?""", (start, end)).fetchone()
    food, cogs = r["food"], r["cogs"]
    disc = round(sum(x[3] for x in _disc_rows(c, start, end)), 2)
    labor = _labor(c, start, end)
    exp = c.execute("SELECT COALESCE(SUM(amount),0) FROM expenses WHERE spent_on BETWEEN ? AND ?", (start, end)).fetchone()[0]
    gross = food - disc - cogs                         # discounts are given-away revenue
    return {"orders": r["orders"], "food_sales": round(food, 2), "discounts": disc,
            "service_fee": round((food - disc) * rate, 2),
            "billed": round((food - disc) * (1 + rate), 2), "cogs": round(cogs, 2),
            "cost_coverage_pct": round(100 * r["costed_sales"] / food) if food else 100,
            "gross_profit": round(gross, 2), "labor": labor, "expenses": round(exp, 2),
            "net_profit": round(gross - labor - exp, 2),
            "avg_ticket": round((food - disc) * (1 + rate) / r["orders"], 2) if r["orders"] else 0}


def _items(c):
    """Every menu item with its settings, sales velocity and a status flag."""
    sold = {r["item_id"]: r for r in c.execute(
        f"""SELECT o.item_id, SUM(CASE WHEN {LOCAL_DAY}=date('now','localtime') THEN o.quantity ELSE 0 END) AS today,
                   SUM(o.quantity) AS week
            FROM table_sessions s JOIN order_line_items o ON o.session_id=s.session_id
            WHERE s.status='CONFIRMED' AND {LOCAL_DAY} >= date('now','localtime','-6 days') GROUP BY o.item_id""")}
    out = []
    for r in c.execute("""SELECT m.*, i.reorder_level, i.par_level, i.unit_cost FROM menu_items m
                          LEFT JOIN item_settings i ON i.item_id=m.id ORDER BY m.category, m.id"""):
        rl = r["reorder_level"] if r["reorder_level"] is not None else 5
        par = r["par_level"] if r["par_level"] is not None else max(rl * 4, 10)
        s = sold.get(r["id"])
        week = s["week"] if s else 0
        per_day = week / 7
        status = "out" if r["stock_qty"] <= 0 else "low" if r["stock_qty"] <= rl else "ok"
        cost = r["unit_cost"]
        out.append({"id": r["id"], "name": r["name"], "category": r["category"], "price": r["price"], "stock": r["stock_qty"],
                    "reorder_level": rl, "par_level": par, "unit_cost": cost,
                    "margin_pct": round(100 * (r["price"] - cost) / r["price"]) if cost is not None and r["price"] else None,
                    "status": status, "sold_today": s["today"] if s else 0, "sold_7d": week,
                    "days_left": round(r["stock_qty"] / per_day, 1) if per_day > 0 and r["stock_qty"] > 0 else None,
                    "suggest": max(par - r["stock_qty"], 0) if status != "ok" else 0})
    return out


def _log_stock(c, item_id, delta, reason, note=""):
    after = c.execute("SELECT stock_qty FROM menu_items WHERE id=?", (item_id,)).fetchone()[0]
    c.execute("INSERT INTO stock_log (item_id, delta, reason, note, stock_after) VALUES (?,?,?,?,?)",
              (item_id, delta, reason, note, after))


# ---------------------------------------------------------------- overview
@mgr_bp.get("/api/manager/overview")
@manager_only
def overview():
    snap = floor.snapshot()
    today = date.today().isoformat()
    with db.db() as c:
        t = pnl(c, today, today)
        week_start = (date.today() - timedelta(days=6)).isoformat()
        by_day = {r["d"]: r for r in c.execute(
            f"""SELECT {LOCAL_DAY} AS d, COUNT(DISTINCT s.session_id) AS orders, SUM(o.quantity*m.price) AS food
                FROM table_sessions s JOIN order_line_items o ON o.session_id=s.session_id JOIN menu_items m ON m.id=o.item_id
                WHERE s.status='CONFIRMED' AND {LOCAL_DAY} >= ? GROUP BY d""", (week_start,))}
        day_disc = _disc_by(_disc_rows(c, week_start, today), 0)
        hour_disc = _disc_by(_disc_rows(c, today, today), 1)
        days = []
        for i in range(7):
            d = (date.today() - timedelta(days=6 - i)).isoformat()
            r = by_day.get(d)
            days.append({"date": d, "billed": round(((r["food"] if r else 0) - day_disc.get(d, 0)) * (1 + db.SERVICE_RATE), 2),
                         "orders": r["orders"] if r else 0})
        hours = {int(r["h"]): r for r in c.execute(
            f"""SELECT strftime('%H', s.created_at,'localtime') AS h, SUM(o.quantity*m.price) AS food, COUNT(DISTINCT s.session_id) AS n
                FROM table_sessions s JOIN order_line_items o ON o.session_id=s.session_id JOIN menu_items m ON m.id=o.item_id
                WHERE s.status='CONFIRMED' AND {LOCAL_DAY}=? GROUP BY h""", (today,))}
        items = _items(c)
        staff_on = c.execute("SELECT COUNT(*) FROM shifts WHERE clock_out IS NULL").fetchone()[0]
        staff_total = c.execute("SELECT COUNT(*) FROM staff WHERE active=1").fetchone()[0]
        default_pin = _setting(c, "pin_is_default") == "1"
        cancelled = c.execute(
            f"""SELECT COUNT(DISTINCT s.session_id) AS n, COALESCE(SUM(o.quantity*m.price),0) AS v FROM table_sessions s
                JOIN order_line_items o ON o.session_id=s.session_id JOIN menu_items m ON m.id=o.item_id
                WHERE s.status='CANCELLED' AND {LOCAL_DAY}=?""", (today,)).fetchone()
        voids = c.execute("SELECT COUNT(*), COALESCE(SUM(amount),0) FROM void_log WHERE date(at,'localtime')=?", (today,)).fetchone()
    alerts = []
    out_of = [i["name"] for i in items if i["status"] == "out"]
    low = [i for i in items if i["status"] == "low"]
    if out_of:
        alerts.append({"level": "bad", "tab": "inventory", "text": f"Sold out: {', '.join(out_of[:4])}" + (f" +{len(out_of) - 4} more" if len(out_of) > 4 else "")})
    if low:
        alerts.append({"level": "warn", "tab": "inventory", "text": f"{len(low)} item(s) at or below reorder level: " + ", ".join(f"{i['name']} ({i['stock']})" for i in low[:3])})
    now = datetime.utcnow()
    for k in snap["kitchen"]:
        mins = (now - datetime.strptime(k["confirmed_at"], "%Y-%m-%dT%H:%M:%SZ")).total_seconds() / 60 if k["confirmed_at"] else 0
        if mins >= 15 and k["status"] != "READY":
            alerts.append({"level": "warn", "tab": "floor", "text": f"Table {k['table_no']}'s order has waited {int(mins)} min in the kitchen"})
        if mins >= 10 and k["status"] == "READY":
            alerts.append({"level": "warn", "tab": "floor", "text": f"Table {k['table_no']}'s food is ready but not served ({int(mins)} min)"})
    for tb in snap["tables"]:
        if tb["status"] == "dining" and tb["since"]:
            mins = (now - datetime.strptime(tb["since"], "%Y-%m-%dT%H:%M:%SZ")).total_seconds() / 60
            if mins >= 90:
                alerts.append({"level": "info", "tab": "floor", "text": f"Table {tb['table_no']} has been seated {int(mins)} min"})
    for r in snap["reservations"]:
        if r["in_min"] < -10:
            alerts.append({"level": "warn", "tab": "floor", "text": f"{r['guest_name']} (table {r['table_no']}) is {-r['in_min']} min late"})
    if staff_total and not staff_on and snap["stats"]["occupied"]:
        alerts.append({"level": "warn", "tab": "team", "text": "Tables are occupied but nobody is clocked in"})
    if default_pin:
        alerts.append({"level": "bad", "tab": "settings", "text": "Manager PIN is still the default 1234 - change it"})
    return jsonify({"ok": True, "today": t, "days": days,
                    "hours": [{"hour": h, "billed": round(((hours[h]["food"] if h in hours else 0) - hour_disc.get(h, 0)) * (1 + db.SERVICE_RATE), 2),
                               "orders": hours[h]["n"] if h in hours else 0} for h in range(24)],
                    "floor": snap["stats"], "alerts": alerts, "top_sellers": snap["top_sellers"],
                    "stock": {"out": len(out_of), "low": len(low), "items": len(items)},
                    "staff": {"on": staff_on, "total": staff_total},
                    "cancelled": {"orders": cancelled["n"], "value": round(cancelled["v"] * (1 + db.SERVICE_RATE), 2)},
                    "voided": {"orders": voids[0], "value": round(voids[1], 2)}})


# ---------------------------------------------------------------- inventory
@mgr_bp.get("/api/manager/inventory")
@manager_only
def inventory():
    with db.db() as c:
        items = _items(c)
        log = [dict(r) for r in c.execute(
            """SELECT l.at, m.name, l.delta, l.reason, l.note, l.stock_after FROM stock_log l
               JOIN menu_items m ON m.id=l.item_id ORDER BY l.log_id DESC LIMIT 40""")]
    costed = [i for i in items if i["unit_cost"] is not None]
    return jsonify({"ok": True, "items": items, "log": log, "categories": sorted({i["category"] for i in items}),
                    "summary": {"stock_cost_value": round(sum(i["stock"] * i["unit_cost"] for i in costed), 2),
                                "stock_retail_value": round(sum(i["stock"] * i["price"] for i in items), 2),
                                "uncosted": len(items) - len(costed),
                                "reorder_cost": round(sum(i["suggest"] * (i["unit_cost"] or 0) for i in items), 2)}})


@mgr_bp.post("/api/manager/stock")
@manager_only
def stock():
    d = _body()
    iid, mode = _int(d.get("item_id"), "item", 1, 10 ** 9), d.get("mode")
    note = (d.get("note") or "").strip()[:80]
    with db.db() as c:
        row = c.execute("SELECT name, stock_qty FROM menu_items WHERE id=?", (iid,)).fetchone()
        if not row:
            raise FloorError("Unknown item")
        cur = row["stock_qty"]
        if mode == "add":
            delta, reason = _int(d.get("qty"), "quantity", 1, 100000), "restock"
        elif mode == "waste":
            delta, reason = -min(_int(d.get("qty"), "quantity", 1, 100000), cur), "waste"
            if cur == 0:
                raise FloorError("Nothing in stock to write off")
        elif mode == "set":
            delta, reason = _int(d.get("qty"), "count", 0, 100000) - cur, "count"
        elif mode == "eighty":
            delta, reason = -cur, "86'd"
        else:
            raise FloorError("Unknown stock action")
        c.execute("UPDATE menu_items SET stock_qty = stock_qty + ? WHERE id=?", (delta, iid))
        if delta:
            _log_stock(c, iid, delta, reason, note)
    return jsonify({"ok": True})


@mgr_bp.post("/api/manager/item")
@manager_only
def item_update():
    d = _body()
    iid = _int(d.get("item_id"), "item", 1, 10 ** 9)
    with db.db() as c:
        if not c.execute("SELECT 1 FROM menu_items WHERE id=?", (iid,)).fetchone():
            raise FloorError("Unknown item")
        c.execute("INSERT OR IGNORE INTO item_settings (item_id) VALUES (?)", (iid,))
        if "price" in d:
            try:
                p = float(d["price"])
            except (TypeError, ValueError):
                raise FloorError("Price must be a number")
            if p < 0:
                raise FloorError("Price cannot be negative")
            c.execute("UPDATE menu_items SET price=? WHERE id=?", (round(p, 2), iid))
        if "reorder_level" in d:
            c.execute("UPDATE item_settings SET reorder_level=? WHERE item_id=?", (_int(d["reorder_level"], "reorder level", 0, 100000), iid))
        if "par_level" in d:
            c.execute("UPDATE item_settings SET par_level=? WHERE item_id=?",
                      (None if d["par_level"] in ("", None) else _int(d["par_level"], "par level", 0, 100000), iid))
        if "unit_cost" in d:
            v = None
            if d["unit_cost"] not in ("", None):
                try:
                    v = float(d["unit_cost"])
                except (TypeError, ValueError):
                    raise FloorError("Cost must be a number")
                if v < 0:
                    raise FloorError("Cost cannot be negative")
            c.execute("UPDATE item_settings SET unit_cost=? WHERE item_id=?", (v, iid))
    return jsonify({"ok": True})


@mgr_bp.post("/api/manager/items")
@manager_only
def item_create():
    d = _body()
    name, cat = (d.get("name") or "").strip()[:60], (d.get("category") or "").strip()[:30]
    if not name or not cat:
        raise FloorError("Name and category are required")
    try:
        price = float(d.get("price"))
    except (TypeError, ValueError):
        raise FloorError("Price must be a number")
    if price < 0:
        raise FloorError("Price cannot be negative")
    qty = _int(d.get("stock", 0), "stock", 0, 100000)
    allergens = ",".join(a.strip().lower() for a in (d.get("allergens") or "").split(",") if a.strip())
    aliases = ",".join(a.strip().lower() for a in (d.get("aliases") or "").split(",") if a.strip())
    with db.db() as c:
        if c.execute("SELECT 1 FROM menu_items WHERE lower(name)=lower(?)", (name,)).fetchone():
            raise FloorError("An item with that name already exists")
        iid = c.execute("INSERT INTO menu_items (name, category, price, stock_qty, allergens, aliases) VALUES (?,?,?,?,?,?)",
                        (name, cat, round(price, 2), qty, allergens, aliases)).lastrowid
        cost = d.get("unit_cost")
        c.execute("INSERT INTO item_settings (item_id, unit_cost) VALUES (?,?)", (iid, float(cost) if cost not in ("", None) else None))
        if qty:
            _log_stock(c, iid, qty, "new item")
    return jsonify({"ok": True})


@mgr_bp.post("/api/manager/items/estimate-costs")
@manager_only
def estimate_costs():
    pct = _int(_body().get("pct", 35), "percent", 5, 95)
    with db.db() as c:
        ids = [r[0] for r in c.execute("SELECT m.id FROM menu_items m LEFT JOIN item_settings i ON i.item_id=m.id WHERE i.unit_cost IS NULL")]
        for iid in ids:
            c.execute("INSERT OR IGNORE INTO item_settings (item_id) VALUES (?)", (iid,))
            c.execute("UPDATE item_settings SET unit_cost = ROUND((SELECT price FROM menu_items WHERE id=?) * ? / 100.0, 2) WHERE item_id=?",
                      (iid, pct, iid))
    return jsonify({"ok": True, "updated": len(ids)})


# ---------------------------------------------------------------- sales
@mgr_bp.get("/api/manager/sales")
@manager_only
def sales():
    start, end = _range(request.args.get("range", "7d"))
    rate = db.SERVICE_RATE
    base = f"""FROM table_sessions s JOIN order_line_items o ON o.session_id=s.session_id
               JOIN menu_items m ON m.id=o.item_id LEFT JOIN item_settings i ON i.item_id=m.id
               WHERE s.status='CONFIRMED' AND {LOCAL_DAY} BETWEEN ? AND ?"""
    with db.db() as c:
        p = pnl(c, start, end)
        drows = _disc_rows(c, start, end)
        d_day, d_hour, d_tbl = _disc_by(drows, 0), _disc_by(drows, 1), _disc_by(drows, 2)
        daily = [{"date": r["d"], "orders": r["n"], "billed": round((r["f"] - d_day.get(r["d"], 0)) * (1 + rate), 2)} for r in c.execute(
            f"SELECT {LOCAL_DAY} AS d, COUNT(DISTINCT s.session_id) AS n, SUM(o.quantity*m.price) AS f {base} GROUP BY d ORDER BY d", (start, end))]
        hourly = {int(r["h"]): r for r in c.execute(
            f"SELECT strftime('%H', s.created_at,'localtime') AS h, COUNT(DISTINCT s.session_id) AS n, SUM(o.quantity*m.price) AS f {base} GROUP BY h", (start, end))}
        cats = [{"name": r["category"], "qty": r["q"], "sales": round(r["f"], 2)} for r in c.execute(
            f"SELECT m.category, SUM(o.quantity) AS q, SUM(o.quantity*m.price) AS f {base} GROUP BY m.category ORDER BY f DESC", (start, end))]
        its = [{"name": r["name"], "category": r["category"], "qty": r["q"], "sales": round(r["f"], 2),
                "cost": round(r["cost"], 2) if r["cost"] is not None else None,
                "profit": round(r["f"] - r["cost"], 2) if r["cost"] is not None else None} for r in c.execute(
            f"""SELECT m.name, m.category, SUM(o.quantity) AS q, SUM(o.quantity*m.price) AS f,
                       CASE WHEN i.unit_cost IS NULL THEN NULL ELSE SUM(o.quantity*i.unit_cost) END AS cost
                {base} GROUP BY m.id ORDER BY f DESC""", (start, end))]
        tables = [{"table_no": r["table_no"], "orders": r["n"], "billed": round((r["f"] - d_tbl.get(r["table_no"], 0)) * (1 + rate), 2)} for r in c.execute(
            f"SELECT s.table_no, COUNT(DISTINCT s.session_id) AS n, SUM(o.quantity*m.price) AS f {base} GROUP BY s.table_no ORDER BY f DESC", (start, end))]
        canc = c.execute(
            f"""SELECT COUNT(DISTINCT s.session_id) AS n, COALESCE(SUM(o.quantity*m.price),0) AS v FROM table_sessions s
                JOIN order_line_items o ON o.session_id=s.session_id JOIN menu_items m ON m.id=o.item_id
                WHERE s.status='CANCELLED' AND {LOCAL_DAY} BETWEEN ? AND ?""", (start, end)).fetchone()
        voids = c.execute("SELECT COUNT(*), COALESCE(SUM(amount),0) FROM void_log WHERE date(at,'localtime') BETWEEN ? AND ?", (start, end)).fetchone()
    return jsonify({"ok": True, "range": request.args.get("range", "7d"), "start": start, "end": end, "totals": p,
                    "daily": daily, "hours": [{"hour": h, "billed": round(((hourly[h]["f"] if h in hourly else 0) - d_hour.get(h, 0)) * (1 + rate), 2),
                                               "orders": hourly[h]["n"] if h in hourly else 0} for h in range(24)],
                    "categories": cats, "items": its, "tables": tables,
                    "cancelled": {"orders": canc["n"], "value": round(canc["v"] * (1 + rate), 2)},
                    "voided": {"orders": voids[0], "value": round(voids[1], 2)}})


# ---------------------------------------------------------------- orders
@mgr_bp.get("/api/manager/orders")
@manager_only
def orders():
    status = request.args.get("status", "ALL")
    limit = max(1, min(int(request.args.get("limit", 60)), 300))
    rate = db.SERVICE_RATE
    where, args = "", []
    if status in ("OPEN", "CONFIRMED", "CANCELLED"):
        where, args = "WHERE s.status=?", [status]
    with db.db() as c:
        rows = c.execute(f"""SELECT s.session_id, s.table_no, s.status, s.created_at, v.reason AS void_reason
                             FROM table_sessions s LEFT JOIN void_log v ON v.session_id=s.session_id {where}
                             ORDER BY s.session_id DESC LIMIT ?""", args + [limit]).fetchall()
        disc_of = {r["session_id"]: r["code"] for r in c.execute("SELECT session_id, code FROM session_discounts")}
        dinfo = {r["code"]: r for r in c.execute("SELECT * FROM discounts")}
        lines = {}
        for r in c.execute(f"""SELECT o.session_id, m.name, o.quantity, o.special_instructions AS notes, m.price FROM order_line_items o
                               JOIN menu_items m ON m.id=o.item_id WHERE o.session_id IN
                               (SELECT session_id FROM (SELECT s.session_id FROM table_sessions s {where} ORDER BY s.session_id DESC LIMIT ?))
                               ORDER BY o.line_id""", args + [limit]):
            lines.setdefault(r["session_id"], []).append({"item": r["name"], "qty": r["quantity"], "notes": r["notes"], "total": r["price"] * r["quantity"]})
    out = []
    for r in rows:
        ls = lines.get(r["session_id"], [])
        sub = sum(l["total"] for l in ls)
        code = disc_of.get(r["session_id"])
        d = dinfo.get(code)
        damt = 0.0
        if d and r["status"] in ("CONFIRMED", "CANCELLED") and (r["status"] == "CONFIRMED" or r["void_reason"] is not None):
            damt = round(sub * d["value"] / 100, 2) if d["kind"] == "percent" else min(d["value"], sub)
        sub -= damt
        out.append({"discount_code": code if damt else None, "discount": damt, "session_id": r["session_id"], "table_no": r["table_no"], "status": "VOIDED" if r["void_reason"] is not None else r["status"],
                    "created_at": r["created_at"].replace(" ", "T") + "Z", "lines": ls, "items": sum(l["qty"] for l in ls),
                    "total": round(sub * (1 + rate), 2), "void_reason": r["void_reason"]})
    return jsonify({"ok": True, "orders": out})


@mgr_bp.post("/api/manager/orders/<int:sid>/void")
@manager_only
def void_order(sid):
    reason = (_body().get("reason") or "").strip()[:100]
    if not reason:
        raise FloorError("Give a reason for the void")
    with db.db() as c:
        s = c.execute("SELECT status FROM table_sessions WHERE session_id=?", (sid,)).fetchone()
        if not s or s["status"] != "CONFIRMED":
            raise FloorError("Only confirmed orders can be voided")
        rows = c.execute("SELECT o.item_id, o.quantity, m.price FROM order_line_items o JOIN menu_items m ON m.id=o.item_id WHERE o.session_id=?", (sid,)).fetchall()
        gross = sum(r["quantity"] * r["price"] for r in rows)
        d = c.execute("""SELECT d.code, d.kind, d.value FROM session_discounts sd JOIN discounts d ON d.code = sd.code
                         WHERE sd.session_id=?""", (sid,)).fetchone()
        saved = (round(gross * d["value"] / 100, 2) if d["kind"] == "percent" else min(d["value"], gross)) if d else 0
        amount = (gross - saved) * (1 + db.SERVICE_RATE)
        if d:                                                    # the promo was not really used: give the redemption back
            c.execute("UPDATE discounts SET used_count = MAX(used_count - 1, 0) WHERE code=?", (d["code"],))
        for r in rows:                                           # put the stock back, and say why
            c.execute("UPDATE menu_items SET stock_qty = stock_qty + ? WHERE id=?", (r["quantity"], r["item_id"]))
            _log_stock(c, r["item_id"], r["quantity"], "void", f"order #{sid}: {reason}"[:80])
        c.execute("UPDATE table_sessions SET status='CANCELLED' WHERE session_id=?", (sid,))
        c.execute("INSERT INTO void_log (session_id, reason, amount) VALUES (?,?,?)", (sid, reason, round(amount, 2)))
    return jsonify({"ok": True})


# ---------------------------------------------------------------- team & costs
def _team_payload(rng):
    start, end = _range(rng)
    with db.db() as c:
        staff = []
        for r in c.execute("SELECT * FROM staff ORDER BY active DESC, name"):
            sh = c.execute("SELECT clock_in FROM shifts WHERE staff_id=? AND clock_out IS NULL ORDER BY shift_id DESC LIMIT 1", (r["staff_id"],)).fetchone()
            hrs = 0.0
            for s in c.execute("SELECT clock_in, clock_out FROM shifts WHERE staff_id=? AND date(clock_in,'localtime') BETWEEN ? AND ?", (r["staff_id"], start, end)):
                a = datetime.strptime(s["clock_in"], "%Y-%m-%d %H:%M:%S")
                b = datetime.strptime(s["clock_out"], "%Y-%m-%d %H:%M:%S") if s["clock_out"] else datetime.utcnow()
                hrs += max((b - a).total_seconds(), 0) / 3600
            staff.append({"staff_id": r["staff_id"], "name": r["name"], "role": r["role"], "hourly_rate": r["hourly_rate"], "active": bool(r["active"]),
                          "on_shift": bool(sh), "since": sh["clock_in"].replace(" ", "T") + "Z" if sh else None,
                          "hours": round(hrs, 2), "wages": round(hrs * r["hourly_rate"], 2)})
        exp = [dict(r) for r in c.execute("SELECT * FROM expenses WHERE spent_on BETWEEN ? AND ? ORDER BY spent_on DESC, exp_id DESC LIMIT 100", (start, end))]
        by_cat = [{"category": r[0], "amount": round(r[1], 2)} for r in c.execute(
            "SELECT category, SUM(amount) FROM expenses WHERE spent_on BETWEEN ? AND ? GROUP BY category ORDER BY 2 DESC", (start, end))]
        p = pnl(c, start, end)
    return {"ok": True, "range": rng, "start": start, "end": end, "staff": staff, "expenses": exp, "expense_categories": by_cat, "pnl": p}


@mgr_bp.get("/api/manager/team")
@manager_only
def team():
    return jsonify(_team_payload(request.args.get("range", "7d")))


@mgr_bp.post("/api/manager/staff")
@manager_only
def staff_add():
    d = _body()
    name = (d.get("name") or "").strip()[:40]
    if not name:
        raise FloorError("Name is required")
    try:
        rate = float(d.get("hourly_rate", 0))
    except (TypeError, ValueError):
        raise FloorError("Hourly rate must be a number")
    if rate < 0:
        raise FloorError("Hourly rate cannot be negative")
    with db.db() as c:
        c.execute("INSERT INTO staff (name, role, hourly_rate) VALUES (?,?,?)", (name, (d.get("role") or "Server").strip()[:30], rate))
    return jsonify({"ok": True})


@mgr_bp.post("/api/manager/staff/<int:sid>")
@manager_only
def staff_update(sid):
    d, sets, vals = _body(), [], []
    if "active" in d:
        sets.append("active=?"); vals.append(1 if d["active"] else 0)
    if "hourly_rate" in d:
        try:
            r = float(d["hourly_rate"])
        except (TypeError, ValueError):
            raise FloorError("Hourly rate must be a number")
        if r < 0:
            raise FloorError("Hourly rate cannot be negative")
        sets.append("hourly_rate=?"); vals.append(r)
    if "role" in d:
        sets.append("role=?"); vals.append((d["role"] or "Server").strip()[:30])
    with db.db() as c:
        if sets:
            c.execute(f"UPDATE staff SET {', '.join(sets)} WHERE staff_id=?", vals + [sid])
        if "active" in d and not d["active"]:
            c.execute("UPDATE shifts SET clock_out=datetime('now') WHERE staff_id=? AND clock_out IS NULL", (sid,))
    return jsonify({"ok": True})


@mgr_bp.post("/api/manager/staff/<int:sid>/clock")
@manager_only
def staff_clock(sid):
    action = _body().get("action")
    with db.db() as c:
        st = c.execute("SELECT active FROM staff WHERE staff_id=?", (sid,)).fetchone()
        if not st:
            raise FloorError("Unknown staff member")
        open_shift = c.execute("SELECT shift_id FROM shifts WHERE staff_id=? AND clock_out IS NULL", (sid,)).fetchone()
        if action == "in":
            if not st["active"]:
                raise FloorError("That staff member is inactive")
            if open_shift:
                raise FloorError("Already clocked in")
            c.execute("INSERT INTO shifts (staff_id) VALUES (?)", (sid,))
        elif action == "out":
            if not open_shift:
                raise FloorError("Not clocked in")
            c.execute("UPDATE shifts SET clock_out=datetime('now') WHERE shift_id=?", (open_shift[0],))
        else:
            raise FloorError("action must be in or out")
    return jsonify({"ok": True})


@mgr_bp.post("/api/manager/expenses")
@manager_only
def expense_add():
    d = _body()
    cat = (d.get("category") or "").strip()[:30]
    if not cat:
        raise FloorError("Category is required")
    try:
        amt = float(d.get("amount"))
    except (TypeError, ValueError):
        raise FloorError("Amount must be a number")
    if amt < 0:
        raise FloorError("Amount cannot be negative")
    try:
        day = datetime.strptime(d.get("date") or date.today().isoformat(), "%Y-%m-%d").date().isoformat()
    except ValueError:
        raise FloorError("Invalid date")
    with db.db() as c:
        c.execute("INSERT INTO expenses (spent_on, category, note, amount) VALUES (?,?,?,?)", (day, cat, (d.get("note") or "").strip()[:80], round(amt, 2)))
    return jsonify({"ok": True})


@mgr_bp.delete("/api/manager/expenses/<int:eid>")
@manager_only
def expense_delete(eid):
    with db.db() as c:
        c.execute("DELETE FROM expenses WHERE exp_id=?", (eid,))
    return jsonify({"ok": True})


# ---------------------------------------------------------------- CSV export
@mgr_bp.get("/api/manager/export/<kind>.csv")
@manager_only
def export_csv(kind):
    buf = io.StringIO()
    w = csv.writer(buf)
    with db.db() as c:
        if kind == "inventory":
            w.writerow(["item", "category", "price", "stock", "reorder_level", "par_level", "unit_cost", "status", "sold_7d", "suggested_order"])
            for i in _items(c):
                w.writerow([i["name"], i["category"], i["price"], i["stock"], i["reorder_level"], i["par_level"], i["unit_cost"], i["status"], i["sold_7d"], i["suggest"]])
        elif kind == "reorder":
            w.writerow(["item", "category", "stock", "reorder_level", "order_qty", "est_cost"])
            for i in _items(c):
                if i["suggest"]:
                    w.writerow([i["name"], i["category"], i["stock"], i["reorder_level"], i["suggest"], round(i["suggest"] * (i["unit_cost"] or 0), 2)])
        elif kind == "stock_log":
            w.writerow(["time_utc", "item", "change", "reason", "note", "stock_after"])
            for r in c.execute("SELECT l.at, m.name, l.delta, l.reason, l.note, l.stock_after FROM stock_log l JOIN menu_items m ON m.id=l.item_id ORDER BY l.log_id DESC"):
                w.writerow(list(r))
        elif kind in ("sales", "orders"):
            w.writerow(["order_id", "table", "status", "created_utc", "item", "qty", "unit_price", "line_total", "notes", "discount_code"])
            for r in c.execute("""SELECT s.session_id, s.table_no, s.status, s.created_at, m.name, o.quantity, m.price, o.quantity*m.price, o.special_instructions,
                                         sd.code
                                  FROM table_sessions s JOIN order_line_items o ON o.session_id=s.session_id JOIN menu_items m ON m.id=o.item_id
                                  LEFT JOIN session_discounts sd ON sd.session_id=s.session_id
                                  ORDER BY s.session_id DESC, o.line_id"""):
                w.writerow(list(r))
        else:
            raise FloorError("Unknown export")
    return Response(buf.getvalue(), mimetype="text/csv", headers={"Content-Disposition": f"attachment; filename={kind}.csv"})


@mgr_bp.get("/manager")
def manager_page():
    return redirect("/#manager")


def register_manager(app):
    app.register_blueprint(mgr_bp)


# ---------------------------------------------------------------- optional demo data
def seed_demo():
    """Staff, expenses, unit costs and a week of history so every manager chart has something to show."""
    ensure_schema()
    import random
    rnd = random.Random(7)
    with db.db() as c:
        if c.execute("SELECT COUNT(*) FROM staff").fetchone()[0]:
            print("Manager demo data already present - skipping."); return
        for n, r, rate in [("Ana Reyes", "Manager", 120), ("Ben Cruz", "Server", 70), ("Carla Lim", "Server", 70), ("Dino Santos", "Cook", 85), ("Ella Tan", "Cashier", 70)]:
            c.execute("INSERT INTO staff (name, role, hourly_rate) VALUES (?,?,?)", (n, r, rate))
        for sid in (1, 2, 4):
            c.execute("INSERT INTO shifts (staff_id, clock_in) VALUES (?, datetime('now','-3 hours'))", (sid,))
        for back in range(1, 7):                               # closed shifts earlier in the week
            for sid in (1, 2, 3, 4):
                c.execute("INSERT INTO shifts (staff_id, clock_in, clock_out) VALUES (?, datetime('now', ?), datetime('now', ?))",
                          (sid, f"-{back * 24 + 8} hours", f"-{back * 24} hours"))
        for back, (cat, note, amt) in enumerate([("Rent", "Monthly", 18000), ("Utilities", "Electric", 2400), ("Supplies", "Packaging", 950),
                                                 ("Marketing", "Flyers", 600), ("Supplies", "Cleaning", 380), ("Utilities", "Water", 520)]):
            c.execute("INSERT INTO expenses (spent_on, category, note, amount) VALUES (date('now','localtime',?),?,?,?)", (f"-{back + 1} days", cat, note, amt))
        for r in c.execute("SELECT id, price FROM menu_items").fetchall():
            c.execute("INSERT OR IGNORE INTO item_settings (item_id) VALUES (?)", (r["id"],))
            if r["id"] % 5:                                     # leave a few uncosted so the UI shows the nudge
                c.execute("UPDATE item_settings SET unit_cost=? WHERE item_id=?", (round(r["price"] * rnd.uniform(.28, .42), 2), r["id"]))
        ids = [r[0] for r in c.execute("SELECT id FROM menu_items WHERE stock_qty > 0")]
        for back in range(1, 7):                               # past orders (already cleared from the floor)
            for _ in range(rnd.randint(8, 16)):
                mins = back * 1440 + rnd.randint(0, 600) - 300
                sid = c.execute("INSERT INTO table_sessions (table_no, status, created_at) VALUES (?, 'CONFIRMED', datetime('now', ?))",
                                (rnd.randint(1, 11), f"-{mins} minutes")).lastrowid
                for iid in rnd.sample(ids, rnd.randint(1, 4)):
                    c.execute("INSERT INTO order_line_items (session_id,item_id,quantity) VALUES (?,?,?)", (sid, iid, rnd.randint(1, 3)))
        for iid in rnd.sample(ids, 3):
            c.execute("INSERT INTO stock_log (item_id, delta, reason, note, stock_after) VALUES (?,?,?,?,(SELECT stock_qty FROM menu_items WHERE id=?))",
                      (iid, 20, "restock", "weekly delivery", iid))
    print("Manager demo data added (staff, expenses, costs, 6 days of orders).")


if __name__ == "__main__":
    import argparse
    p = argparse.ArgumentParser()
    p.add_argument("--demo", action="store_true", help="add staff, costs, expenses and past orders for the demo")
    if p.parse_args().demo:
        db.init_db(); floor.ensure_schema(); seed_demo()
