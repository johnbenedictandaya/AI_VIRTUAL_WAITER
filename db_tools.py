"""db_tools.py - SQLite tool functions the LLM may invoke (plus FSM-only helpers).

Every function returns a JSON-serialisable dict with an "ok" flag, so errors are
reported to the model as data instead of crashing the app.
"""
import difflib
import re
import sqlite3
from contextlib import contextmanager
from datetime import date
from pathlib import Path

DB_PATH = Path(__file__).with_name("restaurant.db")
SQL_PATH = Path(__file__).with_name("init_restaurant_db.sql")
SERVICE_RATE = 0.10


class OutOfStock(Exception):
    pass


@contextmanager
def db():
    """Connection that commits on success, rolls back on error, always closes."""
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def init_db(reset: bool = False) -> None:
    sql = SQL_PATH.read_text(encoding="utf-8")
    m = re.search(r"PRAGMA\s+user_version\s*=\s*(\d+)", sql)
    want = int(m.group(1)) if m else 0
    if DB_PATH.exists() and not reset:                      # old menu/schema? rebuild instead of mixing menus
        con = sqlite3.connect(DB_PATH)
        have = con.execute("PRAGMA user_version").fetchone()[0]
        con.close()
        if have != want:
            print(f"[DB] menu/schema version changed ({have} -> {want}): rebuilding restaurant.db")
            reset = True
    if reset and DB_PATH.exists():
        DB_PATH.unlink()
    with db() as c:
        c.executescript(sql)


# ---------------------------------------------------------------- helpers
EXTRA_ALIASES = {   # lowercase; speech-to-text slips and nicknames. Kept in code so the DB never needs a reset.
    "Fishy Business Sandwich": ["fishy business"],
    "Cheesy Does It": ["cheesy", "cheese does it", "cheesy dose it"],
    "Big Mack Daddy": ["big mac daddy", "mack daddy"],
    "Egg-cellent Muffin": ["egg excellent muffin", "excellent muffin"],
    "Fry Hard": ["fry heart", "fried hard"],
    "Hash Tag Brown": ["hashtag brown", "hashtag"],
    "Sundae Funday": ["sunday funday"],
    "Brew-tiful Iced Tea": ["beautiful iced tea"],
    "Cluck Norris Sandwich": ["clock norris"],
    "Flap Jacked Hotcakes": ["flap jacked", "flapjack"],
    "Aqua Man Bottled Water": ["aqua man", "aquaman"],
}


def _aliases(row):
    own = [a.strip() for a in (row["aliases"] or "").lower().split(",") if a.strip()]
    return own + [a for a in EXTRA_ALIASES.get(row["name"], []) if a not in own]


def _base(row):
    return re.sub(r"\s*\(.*?\)", "", row["name"]).lower()          # "Nugget Life Chose Me (6pc)" -> without "(6pc)"


def _find_item(conn, name: str):
    """Exact name / alias -> unique partial -> fuzzy. Returns (row|None, suggestions)."""
    q = (name or "").strip().lower()
    rows = conn.execute("SELECT * FROM menu_items").fetchall()
    forms = {q, q.rstrip("s")} - {""}                          # "fries" / "burgers" -> also try the singular
    for r in rows:
        if q in (r["name"].lower(), _base(r)) or any(f in _aliases(r) or f == _base(r) for f in forms):
            return r, []
    partial = [r for r in rows if any((len(f) >= 4 and f in _base(r)) or re.search(rf"\b{re.escape(f)}s?\b", _base(r))
                                      for f in forms)]
    if len(partial) == 1:
        return partial[0], []
    if len(partial) > 1:
        return None, [r["name"] for r in partial]              # ambiguous -> let the waiter ask
    keys = {}
    for r in rows:
        keys[_base(r)] = r
        for a in _aliases(r):
            keys.setdefault(a, r)
    close = difflib.get_close_matches(q, list(keys), n=3, cutoff=0.75)
    hits = {keys[c]["name"] for c in close}
    if len(hits) == 1:
        return keys[close[0]], []
    return None, sorted(hits)


def menu_names_and_aliases():
    with db() as c:
        return [{"name": r["name"], "aliases": _aliases(r)} for r in c.execute("SELECT name, aliases FROM menu_items")]


def _in_cart(conn, session_id: int, item_id: int) -> int:
    row = conn.execute(
        "SELECT COALESCE(SUM(quantity),0) AS q FROM order_line_items WHERE session_id=? AND item_id=?",
        (session_id, item_id)).fetchone()
    return row["q"]


# ---------------------------------------------------------------- sessions (FSM-only)
def create_session(table_no: int) -> int:
    with db() as c:
        cur = c.execute("INSERT INTO table_sessions (table_no) VALUES (?)", (table_no,))
        return cur.lastrowid


OPEN_MAX_HOURS = 3        # an abandoned OPEN session stops holding its table after this long (same as the floor map)
DINING_MAX_HOURS = 4      # a CONFIRMED order keeps its table until staff clear it, or after this long


def occupied_tables(own_session=None) -> dict:
    """{table_no: message} for every table that has a live order: someone still ordering, food in the kitchen, or guests
    dining (CONFIRMED and not yet cleared by staff with 'Guests left'). Read straight from table_sessions, so it works
    even when the staff views are not installed. own_session: the caller's own session never blocks them."""
    with db() as c:
        cleared = {}
        if c.execute("SELECT 1 FROM sqlite_master WHERE name='dining_tables'").fetchone():
            cleared = {r[0]: r[1] for r in c.execute("SELECT table_no, cleared_upto FROM dining_tables")}
        kitchen = {}
        if c.execute("SELECT 1 FROM sqlite_master WHERE name='session_meta'").fetchone():
            kitchen = {r[0]: r[1] for r in c.execute("SELECT session_id, kitchen_status FROM session_meta")}
        out = {}
        for r in c.execute("""SELECT session_id, table_no, status FROM table_sessions
                              WHERE (status='OPEN' AND created_at > datetime('now', ?))
                                 OR (status='CONFIRMED' AND created_at > datetime('now', ?)) ORDER BY session_id""",
                           (f"-{OPEN_MAX_HOURS} hours", f"-{DINING_MAX_HOURS} hours")):
            no, sid = r["table_no"], r["session_id"]
            if sid == own_session or sid <= cleared.get(no, 0):
                continue
            if r["status"] == "OPEN":
                out[no] = f"Table {no} already has a customer ordering."
            elif kitchen.get(sid, "QUEUED") == "SERVED":
                out[no] = f"Table {no} is currently dining."
            else:
                out[no] = f"Table {no} has an order in the kitchen."
        return out


def set_table(session_id: int, table_no: int) -> None:
    with db() as c:
        c.execute("UPDATE table_sessions SET table_no=? WHERE session_id=?", (table_no, session_id))


def set_session_status(session_id: int, status: str) -> None:
    with db() as c:
        c.execute("UPDATE table_sessions SET status=? WHERE session_id=?", (status, session_id))


# ---------------------------------------------------------------- LLM tools
def get_menu_by_category(category: str = "all") -> dict:
    with db() as c:
        cats = [r["category"] for r in c.execute("SELECT DISTINCT category FROM menu_items")]
        cat = (category or "all").strip().lower()
        if cat in ("all", "", "everything", "menu"):
            rows = c.execute("SELECT * FROM menu_items ORDER BY id").fetchall()
        else:
            match = [x for x in cats if x.lower() == cat or x.lower().rstrip("s") == cat.rstrip("s")
                     or (len(cat) >= 3 and cat in x.lower())]
            if not match:
                return {"ok": False, "error": f"Unknown category '{category}'", "valid_categories": cats}
            rows = c.execute(f"SELECT * FROM menu_items WHERE category IN ({','.join('?' * len(match))}) "
                             "ORDER BY id", match).fetchall()
        return {"ok": True, "items": [
            {"id": r["id"], "name": r["name"], "category": r["category"], "price_php": r["price"],
             "available": r["stock_qty"] > 0, "allergens": r["allergens"] or "none"} for r in rows]}


def check_item_allergen(item_id, allergen: str) -> dict:
    with db() as c:
        row = None
        try:
            row = c.execute("SELECT * FROM menu_items WHERE id=?", (int(item_id),)).fetchone()
        except (TypeError, ValueError):
            row, _ = _find_item(c, str(item_id))      # model passed a name instead of an id
        if not row:
            return {"ok": False, "error": f"No menu item with id/name '{item_id}'"}
        listed = [a.strip() for a in row["allergens"].split(",") if a.strip()]
        a = (allergen or "").strip().lower()
        return {"ok": True, "item": row["name"], "allergen": a,
                "contains_allergen": a in listed, "all_allergens": listed or ["none"]}


def add_item_to_order(session_id: int, item_name: str, qty=1, instructions: str = "") -> dict:
    try:
        qty = int(qty)
    except (TypeError, ValueError):
        return {"ok": False, "error": "qty must be a whole number"}
    if qty <= 0:
        return {"ok": False, "error": "qty must be at least 1"}
    instructions = (instructions or "").strip()
    with db() as c:
        item, suggestions = _find_item(c, item_name)
        if not item:
            return {"ok": False, "error": f"'{item_name}' is not on our menu",
                    "did_you_mean": suggestions}
        remaining = item["stock_qty"] - _in_cart(c, session_id, item["id"])
        if remaining <= 0:
            return {"ok": False, "error": f"{item['name']} is out of stock"}
        if qty > remaining:
            return {"ok": False, "error": f"Only {remaining} {item['name']} left", "remaining": remaining}
        line = c.execute(
            "SELECT line_id FROM order_line_items WHERE session_id=? AND item_id=? AND special_instructions=?",
            (session_id, item["id"], instructions)).fetchone()
        if line:
            c.execute("UPDATE order_line_items SET quantity = quantity + ? WHERE line_id=?", (qty, line["line_id"]))
        else:
            c.execute("INSERT INTO order_line_items (session_id,item_id,quantity,special_instructions) VALUES (?,?,?,?)",
                      (session_id, item["id"], qty, instructions))
        return {"ok": True, "added": item["name"], "qty": qty, "instructions": instructions,
                "unit_price_php": item["price"]}


def remove_item_from_order(session_id: int, item_name: str, qty=None, instructions=None) -> dict:
    with db() as c:
        item, suggestions = _find_item(c, item_name)
        if not item:
            return {"ok": False, "error": f"'{item_name}' is not on our menu", "did_you_mean": suggestions}
        lines = c.execute("SELECT * FROM order_line_items WHERE session_id=? AND item_id=? ORDER BY line_id DESC",
                          (session_id, item["id"])).fetchall()
        if instructions is not None:             # UI +/- buttons target one specific line
            lines = [l for l in lines if l["special_instructions"] == instructions]
        if not lines:
            return {"ok": False, "error": f"{item['name']} is not in the current order"}
        to_remove = sum(l["quantity"] for l in lines) if qty in (None, "", 0) else int(qty)
        if to_remove <= 0:
            return {"ok": False, "error": "qty must be at least 1"}
        in_cart = sum(l["quantity"] for l in lines)
        to_remove = min(to_remove, in_cart)
        left = to_remove
        for l in lines:
            if left <= 0:
                break
            if l["quantity"] <= left:
                c.execute("DELETE FROM order_line_items WHERE line_id=?", (l["line_id"],))
                left -= l["quantity"]
            else:
                c.execute("UPDATE order_line_items SET quantity=quantity-? WHERE line_id=?", (left, l["line_id"]))
                left = 0
        return {"ok": True, "removed": item["name"], "qty": to_remove, "remaining_in_order": in_cart - to_remove}


def view_current_order(session_id: int) -> dict:
    with db() as c:
        rows = c.execute(
            """SELECT m.name, m.price, o.quantity, o.special_instructions
               FROM order_line_items o JOIN menu_items m ON m.id = o.item_id
               WHERE o.session_id=? ORDER BY o.line_id""", (session_id,)).fetchall()
        lines = [{"item": r["name"], "qty": r["quantity"], "notes": r["special_instructions"],
                  "unit_price_php": r["price"], "line_total_php": r["price"] * r["quantity"]} for r in rows]
        subtotal = sum(l["line_total_php"] for l in lines)
        disc = _session_discount(c, session_id, subtotal)
    amount = disc["amount_php"] if disc and disc["valid"] else 0.0
    net = subtotal - amount
    fee = round(net * SERVICE_RATE, 2)                     # service fee is charged on the discounted amount
    return {"ok": True, "lines": lines, "subtotal_php": subtotal, "discount": disc, "discount_php": amount,
            "service_fee_php": fee, "grand_total_php": round(net + fee, 2)}


# ---------------------------------------------------------------- discounts
def _check_discount(d, subtotal: float):
    """None if the discount is valid for this subtotal right now, else a customer-friendly reason."""
    today = date.today().isoformat()
    if not d["active"]:
        return f"{d['code']} is no longer available"
    if d["valid_from"] and today < d["valid_from"]:
        return f"{d['code']} is not valid yet (starts {d['valid_from']})"
    if d["valid_until"] and today > d["valid_until"]:
        return f"{d['code']} has expired"
    if d["max_uses"] is not None and d["used_count"] >= d["max_uses"]:
        return f"{d['code']} has been fully redeemed"
    if subtotal < d["min_spend"]:
        return f"{d['code']} needs a minimum spend of {d['min_spend']:.0f} PHP (your subtotal is {subtotal:.2f} PHP)"
    return None


def _discount_amount(d, subtotal: float) -> float:
    amt = subtotal * d["value"] / 100 if d["kind"] == "percent" else d["value"]
    return round(min(amt, subtotal), 2)                    # never more than the bill


def _find_discount(conn, text: str):
    """Match a code or a nickname ('senior citizen') case-insensitively. Returns row or None."""
    q = re.sub(r"\s+", " ", (text or "").strip().lower())
    if not q:
        return None
    for r in conn.execute("SELECT * FROM discounts"):
        names = [r["code"].lower()] + [a.strip() for a in r["aliases"].lower().split(",") if a.strip()]
        if q in names or q.replace(" ", "") == r["code"].lower():
            return r
    return None


def match_discount_in_text(text: str):
    """Code of a known discount named anywhere in a sentence ('i have a senior citizen discount'), else None."""
    words = re.findall(r"[a-z0-9]+", (text or "").lower())
    with db() as c:
        names = {}
        for r in c.execute("SELECT * FROM discounts"):
            for n in [r["code"].lower()] + [a.strip() for a in r["aliases"].lower().split(",") if a.strip()]:
                names[tuple(re.findall(r"[a-z0-9]+", n))] = r["code"]
    for size in range(4, 0, -1):                           # longest phrase first
        for i in range(len(words) - size + 1):
            code = names.get(tuple(words[i:i + size]))
            if code:
                return code
    return None


def _session_discount(conn, session_id: int, subtotal: float):
    """The discount attached to a session, with its current validity and amount (None if no discount)."""
    d = conn.execute("""SELECT d.* FROM session_discounts s JOIN discounts d ON d.code = s.code
                        WHERE s.session_id=?""", (session_id,)).fetchone()
    if not d:
        return None
    # a discount already redeemed by THIS session (CONFIRMED) is not re-checked against max_uses / expiry
    done = conn.execute("SELECT status FROM table_sessions WHERE session_id=?", (session_id,)).fetchone()
    reason = None if (done and done["status"] == "CONFIRMED") else _check_discount(d, subtotal)
    return {"code": d["code"], "description": d["description"], "kind": d["kind"], "value": d["value"],
            "requires_id": bool(d["requires_id"]), "valid": reason is None, "reason": reason,
            "amount_php": _discount_amount(d, subtotal) if reason is None else 0.0}


def apply_discount(session_id: int, code: str) -> dict:
    """Attach a discount code to the session (replaces any previous one). Validates everything."""
    with db() as c:
        d = _find_discount(c, code)
        if not d:
            return {"ok": False, "error": f"'{(code or '').strip()}' is not a valid discount code"}
        subtotal = view_current_order(session_id)["subtotal_php"]
        reason = _check_discount(d, subtotal)
        if reason:
            return {"ok": False, "error": reason}
        c.execute("INSERT OR REPLACE INTO session_discounts (session_id, code) VALUES (?,?)", (session_id, d["code"]))
        return {"ok": True, "code": d["code"], "description": d["description"], "requires_id": bool(d["requires_id"]),
                "amount_php": _discount_amount(d, subtotal)}


def remove_discount(session_id: int) -> dict:
    with db() as c:
        cur = c.execute("DELETE FROM session_discounts WHERE session_id=?", (session_id,))
        return {"ok": cur.rowcount > 0, **({} if cur.rowcount else {"error": "No discount is applied"})}


def list_discounts() -> list:
    """Discounts a customer can use today (active, in date range, not fully redeemed)."""
    out = []
    with db() as c:
        for d in c.execute("SELECT * FROM discounts ORDER BY code"):
            if _check_discount(d, float("inf")) is None:
                out.append({"code": d["code"], "description": d["description"], "min_spend": d["min_spend"],
                            "requires_id": bool(d["requires_id"])})
    return out


# ---------------------------------------------------------------- FSM-only: checkout
def commit_order(session_id: int) -> dict:
    """Atomically re-check stock, decrement it, and mark the session CONFIRMED."""
    order = view_current_order(session_id)
    if not order["lines"]:
        return {"ok": False, "error": "Order is empty"}
    disc = order["discount"]
    if disc and not disc["valid"]:                         # lapsed since it was applied (spend dropped, expired ...)
        remove_discount(session_id)                        # separate transaction: must survive the refusal
        return {"ok": False, "error": f"Your discount no longer applies ({disc['reason']}), so it was removed. "
                                      "Please review your new total."}
    try:
        with db() as c:
            for r in c.execute(
                    """SELECT m.id, m.name, m.stock_qty, SUM(o.quantity) AS q
                       FROM order_line_items o JOIN menu_items m ON m.id=o.item_id
                       WHERE o.session_id=? GROUP BY m.id""", (session_id,)).fetchall():
                if r["q"] > r["stock_qty"]:
                    raise OutOfStock(f"Only {r['stock_qty']} {r['name']} left (you ordered {r['q']}).")
                c.execute("UPDATE menu_items SET stock_qty = stock_qty - ? WHERE id=?", (r["q"], r["id"]))
            disc = order["discount"]
            if disc:
                cur = c.execute("UPDATE discounts SET used_count = used_count + 1 WHERE code=? "
                                "AND (max_uses IS NULL OR used_count < max_uses)", (disc["code"],))
                if cur.rowcount == 0:
                    raise OutOfStock(f"{disc['code']} has just been fully redeemed.")
            c.execute("UPDATE table_sessions SET status='CONFIRMED' WHERE session_id=?", (session_id,))
    except OutOfStock as e:
        return {"ok": False, "error": str(e)}
    return {"ok": True, "receipt": order}


def db_snapshot(session_id: int) -> dict:
    """For sample_conversation_log.json: session row, its lines, and current stock."""
    with db() as c:
        sess = c.execute("SELECT * FROM table_sessions WHERE session_id=?", (session_id,)).fetchone()
        stock = c.execute("SELECT name, stock_qty FROM menu_items ORDER BY id").fetchall()
    return {"session": dict(sess) if sess else None,
            "order": view_current_order(session_id),
            "stock": {r["name"]: r["stock_qty"] for r in stock}}
