"""floor_plan.py - live restaurant floor map for the Perpetual Bistro dashboard.

Drop-in add-on: one import + one register call in app.py (see bottom of this docstring).

How it connects to what you already have
  * Occupancy is DERIVED from your existing `table_sessions` rows, so the kiosk / waiter agent drives the map:
        no session                -> free
        session OPEN              -> ordering   (customer is ordering with the waiter)
        session CONFIRMED         -> in kitchen -> dining (after the kitchen marks it served)
        session CANCELLED         -> free again
  * It only ADDS three tables (dining_tables, reservations, session_meta) with CREATE TABLE IF NOT EXISTS.
    Your menu, stock, sessions and the PRAGMA user_version rebuild logic are untouched.
  * Money uses db_tools.SERVICE_RATE, menu/stock come from menu_items, so numbers always match the receipt.

Wire-up (already done in app.py / templates/dashboard.html):
    from ui_dashboard.floor_plan import register_floor
    register_floor(app, on_close_session=callback_or_None)      # after `app = Flask(...)`
    <script src="/static/floor_plan.js"></script>               # before </body> in templates/dashboard.html

Optional demo data (writes sample orders/reservations into restaurant.db - use --reset-db afterwards to wipe):
    python -m ui_dashboard.floor_plan --demo
"""
import argparse
from datetime import datetime, timedelta, timezone
from pathlib import Path

from flask import Blueprint, jsonify, redirect, request

import db_tools as db

HERE = Path(__file__).parent
floor_bp = Blueprint("floor", __name__)
_on_close = None                       # callback(session_id) when staff close an OPEN session from the map

DINING_MAX_HOURS = 4                   # a CONFIRMED session stops occupying its table after this long
OPEN_MAX_HOURS = 3                     # same for abandoned OPEN sessions (kiosk closed mid-order)
RESERVE_AHEAD_MIN = 60                 # a booking turns its table "reserved" this long before the time...
RESERVE_GRACE_MIN = 30                 # ...and stays reserved this long after
LOW_STOCK = 5
KITCHEN_FLOW = ["QUEUED", "PREPARING", "READY", "SERVED"]
SHAPES = ("round", "square", "rect")

SCHEMA = """
CREATE TABLE IF NOT EXISTS dining_tables (
    table_no     INTEGER PRIMARY KEY,
    seats        INTEGER NOT NULL DEFAULT 4 CHECK (seats BETWEEN 1 AND 20),
    zone         TEXT    NOT NULL DEFAULT 'Main Hall',
    shape        TEXT    NOT NULL DEFAULT 'square' CHECK (shape IN ('round','square','rect')),
    x            REAL    NOT NULL DEFAULT 50,          -- centre, % of the floor canvas width
    y            REAL    NOT NULL DEFAULT 50,          -- centre, % of the floor canvas height
    state        TEXT    NOT NULL DEFAULT 'free' CHECK (state IN ('free','seated','cleaning','blocked')),
    party_size   INTEGER,                              -- set when staff seat a party
    seated_at    TEXT,                                 -- UTC
    cleared_upto INTEGER NOT NULL DEFAULT 0            -- sessions with id <= this no longer occupy the table
);
CREATE TABLE IF NOT EXISTS reservations (
    res_id     INTEGER PRIMARY KEY AUTOINCREMENT,
    table_no   INTEGER NOT NULL REFERENCES dining_tables(table_no) ON DELETE CASCADE,
    guest_name TEXT    NOT NULL,
    party_size INTEGER NOT NULL DEFAULT 2,
    res_at     TEXT    NOT NULL,                       -- local time 'YYYY-MM-DD HH:MM'
    status     TEXT    NOT NULL DEFAULT 'BOOKED' CHECK (status IN ('BOOKED','SEATED','CANCELLED','NO_SHOW'))
);
CREATE TABLE IF NOT EXISTS session_meta (
    session_id     INTEGER PRIMARY KEY REFERENCES table_sessions(session_id),
    kitchen_status TEXT NOT NULL DEFAULT 'QUEUED' CHECK (kitchen_status IN ('QUEUED','PREPARING','READY','SERVED')),
    confirmed_at   TEXT                                -- UTC, first time the map saw the confirmed order
);
"""

# (table_no, seats, zone, shape, x%, y%)
DEFAULT_LAYOUT = [
    (1, 2, "Window", "round", 11, 25), (2, 2, "Window", "round", 11, 49), (3, 2, "Window", "round", 11, 73),
    (4, 4, "Main Hall", "square", 33, 24), (5, 4, "Main Hall", "square", 33, 49),
    (6, 4, "Main Hall", "square", 53, 24), (7, 4, "Main Hall", "square", 53, 49),
    (8, 6, "Main Hall", "rect", 43, 76),
    (9, 4, "Patio", "round", 78, 25), (10, 4, "Patio", "round", 78, 49), (11, 6, "Patio", "rect", 78, 76),
]


# ---------------------------------------------------------------- small helpers
def _utc(s):
    return datetime.strptime(s[:19], "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc) if s else None


def _iso(dt):
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ") if dt else None


class FloorError(Exception):
    pass


def _int(v, name, lo, hi):
    try:
        n = int(v)
    except (TypeError, ValueError):
        raise FloorError(f"{name} must be a whole number")
    if not lo <= n <= hi:
        raise FloorError(f"{name} must be between {lo} and {hi}")
    return n


def _pct(v, name):
    try:
        f = float(v)
    except (TypeError, ValueError):
        raise FloorError(f"{name} must be a number")
    return max(2.0, min(98.0, f))


def ensure_schema():
    """Create the add-on tables (and a default floor layout) if they do not exist yet. Cheap + idempotent."""
    with db.db() as c:
        if c.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='dining_tables'").fetchone():
            return
        c.executescript(SCHEMA)
        c.executemany("INSERT INTO dining_tables (table_no, seats, zone, shape, x, y) VALUES (?,?,?,?,?,?)",
                      DEFAULT_LAYOUT)


def _adopt_unmapped(c):
    """The kiosk accepts any table 1-99: if a live session uses a table that is not on the map, add it."""
    known = {r[0] for r in c.execute("SELECT table_no FROM dining_tables")}
    live = c.execute("SELECT DISTINCT table_no FROM table_sessions WHERE status IN ('OPEN','CONFIRMED') "
                     "AND created_at > datetime('now', ?)", (f"-{DINING_MAX_HOURS} hours",)).fetchall()
    for no in sorted(r[0] for r in live if r[0] not in known):
        n = c.execute("SELECT COUNT(*) FROM dining_tables WHERE zone='Overflow'").fetchone()[0]
        c.execute("INSERT INTO dining_tables (table_no, seats, zone, shape, x, y) VALUES (?,4,'Overflow','square',?,90)",
                  (no, 22 + (n % 7) * 11))


# ---------------------------------------------------------------- the one read model the UI polls
def snapshot() -> dict:
    ensure_schema()
    now_utc, now = datetime.now(timezone.utc), datetime.now()
    rate = db.SERVICE_RATE
    with db.db() as c:
        _adopt_unmapped(c)
        tables = [dict(r) for r in c.execute("SELECT * FROM dining_tables ORDER BY table_no")]
        cleared = {t["table_no"]: t["cleared_upto"] for t in tables}

        active = {}                                              # table_no -> newest live session
        for r in c.execute("SELECT session_id, table_no, status, created_at FROM table_sessions "
                           "WHERE status IN ('OPEN','CONFIRMED') ORDER BY session_id"):
            limit = DINING_MAX_HOURS if r["status"] == "CONFIRMED" else OPEN_MAX_HOURS
            if r["session_id"] > cleared.get(r["table_no"], 0) and now_utc - _utc(r["created_at"]) < timedelta(hours=limit):
                active[r["table_no"]] = dict(r)

        sids = [s["session_id"] for s in active.values()]
        lines = {sid: [] for sid in sids}
        if sids:
            q = ",".join("?" * len(sids))
            for r in c.execute(
                    f"""SELECT o.session_id, m.name, m.price, o.quantity, o.special_instructions AS notes
                        FROM order_line_items o JOIN menu_items m ON m.id = o.item_id
                        WHERE o.session_id IN ({q}) ORDER BY o.line_id""", sids):
                lines[r["session_id"]].append({"item": r["name"], "qty": r["quantity"], "notes": r["notes"],
                                               "line_total_php": r["price"] * r["quantity"]})
            for s in active.values():
                if s["status"] == "CONFIRMED":
                    c.execute("INSERT OR IGNORE INTO session_meta (session_id, confirmed_at) VALUES (?, datetime('now'))",
                              (s["session_id"],))
        meta = {r["session_id"]: dict(r) for r in c.execute("SELECT * FROM session_meta")}

        today = now.strftime("%Y-%m-%d")
        reservations = []
        for r in c.execute("SELECT * FROM reservations WHERE status='BOOKED' AND res_at >= ? ORDER BY res_at", (today,)):
            r = dict(r)
            r["in_min"] = round((datetime.strptime(r["res_at"], "%Y-%m-%d %H:%M") - now).total_seconds() / 60)
            r["time"] = r["res_at"][11:16]
            r["date"] = r["res_at"][:10]
            reservations.append(r)

        today_ids = [r[0] for r in c.execute(
            "SELECT session_id FROM table_sessions WHERE status='CONFIRMED' "
            "AND date(created_at, 'localtime') = date('now', 'localtime')")]
        sales = {"orders": 0, "sub": 0}
        top = [{"name": r["name"], "qty": r["q"]} for r in c.execute(
            """SELECT m.name, SUM(o.quantity) AS q FROM table_sessions s
               JOIN order_line_items o ON o.session_id = s.session_id JOIN menu_items m ON m.id = o.item_id
               WHERE s.status = 'CONFIRMED' AND date(s.created_at, 'localtime') = date('now', 'localtime')
               GROUP BY m.id ORDER BY q DESC, m.name LIMIT 5""")]
        low = [{"name": r["name"], "stock": r["stock_qty"]} for r in c.execute(
            "SELECT name, stock_qty FROM menu_items WHERE stock_qty <= ? ORDER BY stock_qty, name", (LOW_STOCK,))]

    for sid in today_ids:                                       # grand totals include discounts, same as the receipt
        o = db.view_current_order(sid)
        if o["lines"]:
            sales["orders"] += 1
            sales["sub"] += o["grand_total_php"]
    out, tickets = [], []
    for t in tables:
        no, s = t["table_no"], active.get(t["table_no"])
        res = next((r for r in reservations if r["table_no"] == no
                    and -RESERVE_GRACE_MIN <= r["in_min"] <= RESERVE_AHEAD_MIN), None)
        row = {"table_no": no, "seats": t["seats"], "zone": t["zone"], "shape": t["shape"], "x": t["x"], "y": t["y"],
               "session_id": None, "party_size": None, "since": None, "lines": [], "item_count": 0,
               "subtotal_php": 0, "total_php": 0, "kitchen_status": None, "reservation": res}
        if s:
            ls = lines[s["session_id"]]
            sub = sum(l["line_total_php"] for l in ls)
            seated = _utc(t["seated_at"]) if t["state"] == "seated" and t["seated_at"] else None
            row.update(session_id=s["session_id"], party_size=t["party_size"] if t["state"] == "seated" else None,
                       since=_iso(seated or _utc(s["created_at"])), lines=ls, item_count=sum(l["qty"] for l in ls),
                       subtotal_php=sub, total_php=db.view_current_order(s["session_id"])["grand_total_php"])
            if s["status"] == "OPEN":
                row["status"] = "ordering"
            else:
                m = meta.get(s["session_id"], {})
                row["kitchen_status"] = m.get("kitchen_status", "QUEUED")
                row["status"] = "dining" if row["kitchen_status"] == "SERVED" else "preparing"
                if row["kitchen_status"] != "SERVED":
                    tickets.append({"session_id": s["session_id"], "table_no": no, "status": row["kitchen_status"],
                                    "confirmed_at": _iso(_utc(m.get("confirmed_at"))), "items": ls,
                                    "item_count": row["item_count"]})
        elif t["state"] == "seated":
            row.update(status="seated", party_size=t["party_size"], since=_iso(_utc(t["seated_at"])))
        elif t["state"] in ("cleaning", "blocked"):
            row["status"] = t["state"]
        elif res:
            row["status"] = "reserved"
        else:
            row["status"] = "free"
        out.append(row)
    tickets.sort(key=lambda k: k["confirmed_at"] or "")

    usable = [t for t in out if t["status"] != "blocked"]
    busy = [t for t in out if t["status"] in ("seated", "ordering", "preparing", "dining")]
    orders = sales["orders"]
    revenue = round(sales["sub"], 2)
    stats = {"tables": len(out), "usable": len(usable), "occupied": len(busy),
             "free": sum(t["status"] == "free" for t in out), "reserved": sum(t["status"] == "reserved" for t in out),
             "cleaning": sum(t["status"] == "cleaning" for t in out),
             "occupancy_pct": round(100 * len(busy) / len(usable)) if usable else 0,
             "seats_total": sum(t["seats"] for t in usable), "seats_in_use": sum(t["seats"] for t in busy),
             "guests": sum(t["party_size"] or 0 for t in busy),
             "kitchen_queue": len(tickets), "open_orders": sum(t["status"] == "ordering" for t in out),
             "sales_today_php": revenue, "orders_today": orders,
             "avg_ticket_php": round(revenue / orders, 2) if orders else 0}
    return {"server_now": _iso(now_utc), "tables": out, "stats": stats, "reservations": reservations,
            "kitchen": tickets, "low_stock": low, "top_sellers": top,
            "zones": sorted({t["zone"] for t in out}), "low_stock_threshold": LOW_STOCK}


def _table(c, no):
    row = c.execute("SELECT * FROM dining_tables WHERE table_no=?", (no,)).fetchone()
    if not row:
        raise FloorError(f"Table {no} does not exist")
    return row


def _status_of(no):
    return next(t["status"] for t in snapshot()["tables"] if t["table_no"] == no)


def unavailable_tables(own_session=None) -> dict:
    """{table_no: message} for every table a customer may not pick right now.
    own_session: the caller's session id, so a customer's own table never blocks them."""
    out = {}
    for t in snapshot()["tables"]:
        no, status = t["table_no"], t["status"]
        if t["session_id"] and t["session_id"] != own_session:
            out[no] = f"Table {no} is already taken."
        elif status == "reserved":
            out[no] = f"Table {no} is reserved."
        elif status in ("cleaning", "blocked"):
            out[no] = f"Table {no} isn't available right now."
    return out


# ---------------------------------------------------------------- HTTP layer
def _ok():
    return jsonify({"ok": True, "floor": snapshot()})


def _guard(fn):
    def wrapper(*a, **kw):
        try:
            return fn(*a, **kw)
        except FloorError as e:
            return jsonify({"ok": False, "error": str(e)}), 400
    wrapper.__name__ = fn.__name__
    return wrapper


def _body():
    return request.get_json(silent=True) or {}


@floor_bp.get("/api/floor")
def api_floor():
    return jsonify(snapshot())


@floor_bp.post("/api/floor/seat")
@_guard
def api_seat():
    d = _body()
    no = _int(d.get("table_no"), "table_no", 1, 99)
    status = _status_of(no)
    if status not in ("free", "reserved", "seated"):
        raise FloorError(f"Table {no} is not available ({status}).")
    with db.db() as c:
        t = _table(c, no)
        party = _int(d.get("party_size", 2), "party size", 1, 20)
        if party > t["seats"]:
            raise FloorError(f"Table {no} only seats {t['seats']}.")
        c.execute("UPDATE dining_tables SET state='seated', party_size=?, seated_at=COALESCE(seated_at, datetime('now')) "
                  "WHERE table_no=?", (party, no))
        if d.get("res_id"):
            c.execute("UPDATE reservations SET status='SEATED' WHERE res_id=? AND table_no=?", (d["res_id"], no))
    return _ok()


@floor_bp.post("/api/floor/clear")
@_guard
def api_clear():
    """Guests left (or staff cancel the seating): hide the finished session and mark the table for cleaning."""
    no = _int(_body().get("table_no"), "table_no", 1, 99)
    closed = None
    with db.db() as c:
        _table(c, no)
        last = c.execute("SELECT COALESCE(MAX(session_id),0) FROM table_sessions WHERE table_no=?", (no,)).fetchone()[0]
        open_row = c.execute("SELECT session_id FROM table_sessions WHERE table_no=? AND status='OPEN' "
                             "AND session_id > (SELECT cleared_upto FROM dining_tables WHERE table_no=?)", (no, no)).fetchall()
        for r in open_row:
            c.execute("UPDATE table_sessions SET status='CANCELLED' WHERE session_id=?", (r[0],))
            closed = r[0]
        c.execute("UPDATE dining_tables SET state='cleaning', cleared_upto=?, party_size=NULL, seated_at=NULL "
                  "WHERE table_no=?", (last, no))
    if closed and _on_close:
        _on_close(closed)
    return _ok()


@floor_bp.post("/api/floor/state")
@_guard
def api_state():
    d = _body()
    no, state = _int(d.get("table_no"), "table_no", 1, 99), d.get("state")
    if state not in ("free", "cleaning", "blocked"):
        raise FloorError("state must be free, cleaning or blocked")
    if _status_of(no) in ("ordering", "preparing", "dining"):
        raise FloorError(f"Table {no} has an active order - use 'Guests left' first.")
    with db.db() as c:
        _table(c, no)
        c.execute("UPDATE dining_tables SET state=?, party_size=NULL, seated_at=NULL WHERE table_no=?", (state, no))
    return _ok()


@floor_bp.post("/api/floor/kitchen")
@_guard
def api_kitchen():
    d = _body()
    sid, status = _int(d.get("session_id"), "session_id", 1, 10 ** 9), d.get("status")
    if status not in KITCHEN_FLOW:
        raise FloorError("Unknown kitchen status")
    with db.db() as c:
        if not c.execute("SELECT 1 FROM table_sessions WHERE session_id=? AND status='CONFIRMED'", (sid,)).fetchone():
            raise FloorError("That order is not confirmed yet.")
        c.execute("INSERT OR IGNORE INTO session_meta (session_id, confirmed_at) VALUES (?, datetime('now'))", (sid,))
        c.execute("UPDATE session_meta SET kitchen_status=? WHERE session_id=?", (status, sid))
    return _ok()


@floor_bp.post("/api/floor/reservations")
@_guard
def api_reserve():
    d = _body()
    no = _int(d.get("table_no"), "table_no", 1, 99)
    name = (d.get("guest_name") or "").strip()[:60]
    if not name:
        raise FloorError("Guest name is required")
    date = (d.get("date") or datetime.now().strftime("%Y-%m-%d")).strip()
    try:
        when = datetime.strptime(f"{date} {d.get('time', '')}", "%Y-%m-%d %H:%M")
    except ValueError:
        raise FloorError("Pick a valid time")
    if when < datetime.now() - timedelta(minutes=15):
        raise FloorError("That time has already passed")
    with db.db() as c:
        t = _table(c, no)
        party = _int(d.get("party_size", 2), "party size", 1, 20)
        if party > t["seats"]:
            raise FloorError(f"Table {no} only seats {t['seats']}.")
        for r in c.execute("SELECT res_at, guest_name FROM reservations WHERE table_no=? AND status='BOOKED'", (no,)):
            if abs((datetime.strptime(r["res_at"], "%Y-%m-%d %H:%M") - when).total_seconds()) < 90 * 60:
                raise FloorError(f"Table {no} is already booked at {r['res_at'][11:]} ({r['guest_name']}).")
        c.execute("INSERT INTO reservations (table_no, guest_name, party_size, res_at) VALUES (?,?,?,?)",
                  (no, name, party, when.strftime("%Y-%m-%d %H:%M")))
    return _ok()


@floor_bp.post("/api/floor/reservations/<int:rid>/status")
@_guard
def api_reservation_status(rid):
    status = _body().get("status")
    if status not in ("CANCELLED", "NO_SHOW"):
        raise FloorError("status must be CANCELLED or NO_SHOW")
    with db.db() as c:
        c.execute("UPDATE reservations SET status=? WHERE res_id=? AND status='BOOKED'", (status, rid))
    return _ok()


@floor_bp.post("/api/floor/tables")
@_guard
def api_table_create():
    d = _body()
    with db.db() as c:
        no = _int(d.get("table_no") or (c.execute("SELECT COALESCE(MAX(table_no),0)+1 FROM dining_tables").fetchone()[0]),
                  "table_no", 1, 99)
        if c.execute("SELECT 1 FROM dining_tables WHERE table_no=?", (no,)).fetchone():
            raise FloorError(f"Table {no} already exists")
        shape = d.get("shape", "square")
        if shape not in SHAPES:
            raise FloorError("shape must be round, square or rect")
        c.execute("INSERT INTO dining_tables (table_no, seats, zone, shape, x, y) VALUES (?,?,?,?,?,?)",
                  (no, _int(d.get("seats", 4), "seats", 1, 20), (d.get("zone") or "Main Hall").strip()[:24] or "Main Hall",
                   shape, _pct(d.get("x", 50), "x"), _pct(d.get("y", 50), "y")))
    return _ok()


@floor_bp.put("/api/floor/tables/<int:no>")
@_guard
def api_table_update(no):
    d, sets, vals = _body(), [], []
    if "seats" in d:
        sets.append("seats=?"); vals.append(_int(d["seats"], "seats", 1, 20))
    if "zone" in d:
        sets.append("zone=?"); vals.append((d["zone"] or "").strip()[:24] or "Main Hall")
    if "shape" in d:
        if d["shape"] not in SHAPES:
            raise FloorError("shape must be round, square or rect")
        sets.append("shape=?"); vals.append(d["shape"])
    for k in ("x", "y"):
        if k in d:
            sets.append(f"{k}=?"); vals.append(_pct(d[k], k))
    with db.db() as c:
        _table(c, no)
        if sets:
            c.execute(f"UPDATE dining_tables SET {', '.join(sets)} WHERE table_no=?", vals + [no])
    return _ok()


@floor_bp.delete("/api/floor/tables/<int:no>")
@_guard
def api_table_delete(no):
    if _status_of(no) in ("seated", "ordering", "preparing", "dining"):
        raise FloorError(f"Table {no} is in use - clear it first.")
    with db.db() as c:
        _table(c, no)
        c.execute("DELETE FROM dining_tables WHERE table_no=?", (no,))
    return _ok()


@floor_bp.get("/floor")
def floor_page():
    return redirect("/#floor")


def register_floor(app, on_close_session=None):
    """Attach the floor map to your Flask app. on_close_session(session_id) lets the app reset its waiter agent."""
    global _on_close
    _on_close = on_close_session
    app.register_blueprint(floor_bp)


# ---------------------------------------------------------------- optional demo data
def seed_demo():
    """Populate a believable lunch rush so the map has something to show."""
    ensure_schema()
    now_utc, now = datetime.now(timezone.utc), datetime.now()
    stamp = lambda m: (now_utc - timedelta(minutes=m)).strftime("%Y-%m-%d %H:%M:%S")

    def order(c, table, ago, items, status="CONFIRMED", kitchen=None):
        sid = c.execute("INSERT INTO table_sessions (table_no, status, created_at) VALUES (?,?,?)",
                        (table, status, stamp(ago))).lastrowid
        for name, qty, note in items:
            item = c.execute("SELECT id FROM menu_items WHERE name LIKE ?", (name + "%",)).fetchone()
            c.execute("INSERT INTO order_line_items (session_id,item_id,quantity,special_instructions) VALUES (?,?,?,?)",
                      (sid, item[0], qty, note))
        if kitchen:
            c.execute("INSERT OR REPLACE INTO session_meta (session_id, kitchen_status, confirmed_at) VALUES (?,?,?)",
                      (sid, kitchen, stamp(max(ago - 2, 0))))
        return sid

    with db.db() as c:
        for tbl, ago, items in [(1, 170, [("Big Mack", 2, ""), ("Fry Hard", 2, "")]),
                                (8, 150, [("Nugget Life", 3, ""), ("Bubble Trouble", 3, "")]),
                                (4, 130, [("Cheesy Does It", 2, "no onions"), ("Brew-tiful", 2, "")])]:
            order(c, tbl, ago, items, kitchen="SERVED")            # earlier turnover -> today's sales
        for tbl in (1, 4, 8):
            last = c.execute("SELECT MAX(session_id) FROM table_sessions WHERE table_no=?", (tbl,)).fetchone()[0]
            c.execute("UPDATE dining_tables SET cleared_upto=? WHERE table_no=?", (last, tbl))
        order(c, 2, 52, [("Quarter Pounder", 1, ""), ("Fry Hard", 1, ""), ("Shake Your", 1, "")], kitchen="SERVED")
        order(c, 6, 14, [("Cluck Yeah", 2, ""), ("Sweet Spaghet", 1, "extra cheese"), ("Lemon Lime", 3, "less ice")],
              kitchen="PREPARING")
        order(c, 9, 24, [("Lettuce Be Friends", 2, ""), ("Hash Tag", 2, ""), ("Wake-Up", 2, "")], kitchen="READY")
        order(c, 10, 6, [("Double Trouble", 3, ""), ("Fry Hard", 3, "")], kitchen="QUEUED")
        order(c, 5, 4, [("Egg-cellent", 2, ""), ("Aqua Man", 2, "")], status="OPEN")
        c.execute("UPDATE dining_tables SET state='seated', party_size=3, seated_at=? WHERE table_no=7", (stamp(3),))
        c.execute("UPDATE dining_tables SET state='cleaning' WHERE table_no=3")
        c.execute("UPDATE dining_tables SET state='blocked' WHERE table_no=11")
        for tbl, name, party, mins in [(8, "Reyes", 5, 25), (3, "Santos", 2, 70), (11, "Dela Cruz", 6, 150), (4, "Lim", 4, 45)]:
            when = (now + timedelta(minutes=mins)).strftime("%Y-%m-%d %H:%M")
            c.execute("INSERT INTO reservations (table_no, guest_name, party_size, res_at) VALUES (?,?,?,?)",
                      (tbl, name, party, when))
        c.execute("UPDATE menu_items SET stock_qty=3 WHERE name LIKE 'Shake Your%'")
    print("Demo data added. Start the dashboard and open the Floor tab. (python app.py --reset-db wipes it.)")


if __name__ == "__main__":
    p = argparse.ArgumentParser(description="Floor-plan add-on utilities")
    p.add_argument("--demo", action="store_true", help="insert demo sessions/reservations into restaurant.db")
    if p.parse_args().demo:
        db.init_db()
        seed_demo()
