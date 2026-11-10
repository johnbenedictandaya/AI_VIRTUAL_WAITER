"""fsm_controller.py - Finite State Machine controller + session memory for the virtual waiter.

Design:
  * The FSM (plain Python) decides WHICH state we are in and handles deterministic
    steps (table number, totals, checkout). Money and stock never pass through the LLM.
  * The LLM (Qwen via Ollama) is only used in STATE_TAKING_ORDER, where it can call tools.
  * `session_id` is injected by the code, never chosen by the model.
  * `self.messages` is the multi-turn memory; the system prompt is refreshed each turn
    with the live cart so the model cannot drift from the database.
  * Every order change is acknowledged by deterministic code built from the tool results,
    so the customer always sees exactly what changed (the LLM only adds friendly text).
  * Safety net: if Qwen ignores a clear order, a small rule-based parser applies it. These
    calls are logged with source="rule_fallback" so they are never mistaken for LLM calls.
"""
import difflib
import json
import os
import re
import sys
from datetime import datetime
from enum import Enum

import db_tools as db
import waiter_vocab as vocab

MAX_TOOL_ROUNDS = 5
DEFAULT_MODEL = os.environ.get("BISTRO_MODEL", "qwen3:4b")      # see bench_models.py to compare others
KEEP_TURNS = 3          # user turns of LLM memory kept (smaller prompt = faster model)


class State(str, Enum):
    GREETING = "STATE_GREETING"
    TAKING_ORDER = "STATE_TAKING_ORDER"
    REVIEW = "STATE_REVIEW"
    CONFIRMED = "STATE_CONFIRMED"
    CANCELLED = "STATE_CANCELLED"


def unavailable_tables(own_session=None) -> dict:
    """{table_no: message} for every table a customer may not take: a live order (ordering / in the kitchen / dining),
    plus - when the staff views are installed - reserved, cleaning or blocked tables from the floor map."""
    out = {}
    try:
        from ui_dashboard.floor_plan import unavailable_tables as floor_tables
        out.update(floor_tables(own_session))
    except ImportError:
        pass
    except Exception as exc:  # noqa: BLE001 - a map hiccup must never stop the order check below
        dbg(f"[floor check skipped] {exc!r}")
    out.update(db.occupied_tables(own_session))             # read straight from the sessions: always enforced
    return out


def table_blocked(table, own_session=None):
    """Message if this table may not be taken right now, else None."""
    return unavailable_tables(own_session).get(table)


def dbg(*parts):
    """Console debug print that never crashes on emoji / odd characters (Windows cmd)."""
    text = " ".join(str(p) for p in parts)
    enc = sys.stdout.encoding or "utf-8"
    print(text.encode(enc, "replace").decode(enc))


# ---------------------------------------------------------------- tool schemas (session_id hidden from LLM)
TOOLS = [
    {"type": "function", "function": {
        "name": "get_menu_by_category",
        "description": "List menu items (id, price, availability, allergens). Category: Burgers, Chicken, Rice & Pasta, Breakfast, Sides, Drinks, Desserts, or 'all'.",
        "parameters": {"type": "object", "properties": {"category": {"type": "string"}}, "required": ["category"]}}},
    {"type": "function", "function": {
        "name": "check_item_allergen",
        "description": "Check whether a menu item contains an allergen (e.g. dairy, gluten, egg, soy, fish).",
        "parameters": {"type": "object", "properties": {
            "item_id": {"type": "integer", "description": "id from get_menu_by_category"},
            "allergen": {"type": "string"}}, "required": ["item_id", "allergen"]}}},
    {"type": "function", "function": {
        "name": "add_item_to_order",
        "description": "Add a menu item to the customer's tab, with optional special instructions like 'no onions'.",
        "parameters": {"type": "object", "properties": {
            "item_name": {"type": "string"}, "qty": {"type": "integer"},
            "instructions": {"type": "string"}}, "required": ["item_name", "qty"]}}},
    {"type": "function", "function": {
        "name": "remove_item_from_order",
        "description": "Remove an item (or reduce its quantity) from the tab. Omit qty to remove all of it.",
        "parameters": {"type": "object", "properties": {
            "item_name": {"type": "string"}, "qty": {"type": "integer"}}, "required": ["item_name"]}}},
    {"type": "function", "function": {
        "name": "view_current_order",
        "description": "Get the current tab with line items and totals.",
        "parameters": {"type": "object", "properties": {}}}},
]
TOOL_NAMES = {t["function"]["name"] for t in TOOLS}

SYSTEM_PROMPT = """You are the virtual waiter at Perpetual Bistro, a campus dining facility. Table {table}.
Be friendly and brief (1-3 sentences).

STRICT RULES
- You ONLY talk about Perpetual Bistro: the menu, prices, allergens, the customer's order and their table.
  For anything else (news, weather, general knowledge, opinions, jokes, other restaurants) reply exactly:
  "Sorry, I can only help with our menu and your order."
- Talk like a friendly restaurant waiter: simple, polite words, no slang, no emojis, no technical terms.
- NEVER invent menu items, prices, ingredients or allergens. Look them up with tools first.
- To add, change or remove food you MUST call a tool. Do not claim something was added until the tool says ok.
- Whenever the customer names a menu item they want, call add_item_to_order IMMEDIATELY. Do not ask for
  confirmation first. If they give a quantity use it, otherwise use 1.
- Our menu items have FUNNY names. Customers may say a nickname ('fries', 'coke', 'big mac', 'nuggets'); use the
  menu name for it (Fry Hard, Bubble Trouble Cola, Big Mack Daddy, Nugget Life Chose Me). Never invent items.
- Put preferences such as 'no onions', 'extra cheese' or 'less ice' in the `instructions` argument.
- A modification like "make it vegetarian" means: check the menu (get_menu_by_category) and swap to a real
  item (e.g. Lettuce Be Friends Veggie Burger): remove the old item with remove_item_from_order, then add the new one.
- If a tool returns an error (out of stock, not on the menu), tell the customer politely and offer the alternatives it returns.
- After tool calls, reply with a short confirmation naming the item, quantity and any notes, then ask if they want anything else.
- Discounts and promo codes are handled by the system, never by you. Never invent or promise a discount. If asked,
  say the customer can type or say "promo code <CODE>", "senior discount" or "PWD discount".
- Do not quote totals yourself; the system shows the itemized tab when the customer is done.
- When the customer seems finished, tell them to say "that's all" to review their order.

EXAMPLES (follow this behaviour)
Customer: "I'd like 2 cheeseburgers, no onions"
  -> call add_item_to_order(item_name="Cheesy Does It", qty=2, instructions="no onions"), then say: "Added 2 Cheesy Does It with no onions. Anything else?"
Customer: "a coke and fries please"
  -> call add_item_to_order(item_name="Bubble Trouble Cola", qty=1) and add_item_to_order(item_name="Fry Hard", qty=1), then confirm both.
Customer: "make that vegetarian"
  -> call get_menu_by_category("Burgers"), remove_item_from_order(item_name="Big Mack Daddy"), add_item_to_order(item_name="Lettuce Be Friends Veggie Burger", qty=<same qty>), then confirm the swap.
Customer: "what drinks do you have?"
  -> call get_menu_by_category("Drinks") and list the names and prices it returns.
Customer: "does Fry Hard have gluten?"
  -> find its id with get_menu_by_category, then call check_item_allergen(item_id=<id>, allergen="gluten").
Customer: "take off the nuggets"
  -> call remove_item_from_order(item_name="Nugget Life Chose Me (6pc)").

CURRENT TAB (ground truth from the database):
{tab}
"""

# ---------------------------------------------------------------- language helpers
UNITS = ("zero one two three four five six seven eight nine ten eleven twelve thirteen "
         "fourteen fifteen sixteen seventeen eighteen nineteen").split()
TENS = {"twenty": 20, "thirty": 30, "forty": 40, "fifty": 50}
_T, _U = "|".join(TENS), "|".join(UNITS[1:10])
_W = "|".join(sorted(UNITS[1:] + list(TENS), key=len, reverse=True))
NUM = rf"(?:\d{{1,3}}|(?:{_T})(?:[\s-](?:{_U}))?|{_W})"          # 4, four, twenty-one ...


def to_int(s: str) -> int:
    parts = s.lower().replace("-", " ").split()
    if parts[0].isdigit():
        return int(parts[0])
    return sum(TENS[w] if w in TENS else UNITS.index(w) for w in parts)


TABLE_EXPLICIT = re.compile(rf"\btable\s*(?:no\.?|number|num|#)?\s*(?:is\s*|:)?\s*({NUM})\b", re.I)
TABLE_AT = re.compile(rf"\b(?:i'?m|im|i am|we'?re|we are)\s+(?:sitting\s+)?(?:at|in|on)\s+(?:table\s*)?({NUM})\b", re.I)
TABLE_BARE = re.compile(rf"\W*(?:number\s+)?({NUM})\W*", re.I)


def parse_table(text: str, bare: bool = False):
    """'table 4', 'my table is twelve', "I'm at 7", or (bare=True) just '4'. Returns 1-99 or None."""
    for rx in (TABLE_EXPLICIT, TABLE_AT):
        m = rx.search(text)
        if m:
            n = to_int(m.group(1))
            return n if 1 <= n <= 99 else None
    if bare:
        m = TABLE_BARE.fullmatch(text.strip())
        if m:
            n = to_int(m.group(1))
            return n if 1 <= n <= 99 else None
    return None


DONE_RE = re.compile(r"\b(that'?s (all|it)|i'?m (done|finished)|all done|that will be all|the bill|check ?out|review|ready to (order|confirm))\b", re.I)
CANCEL_RE = re.compile(r"\b(cancel (the |my )?(whole |entire )?order|cancel everything|forget it|i'?m leaving)\b", re.I)
YES_RE = re.compile(r"\b(yes|yep|yeah|confirm(ed)?|correct|proceed|send it|place (the |my )?order|looks good|ok(ay)?)\b", re.I)
EDIT_RE = re.compile(r"\b(no|nope|wait|change|edit|add|remove|more|modify)\b", re.I)
CLAIM_RE = re.compile(r"\b(i'?ve added|i have added|added|has been added|have been added|i'?ve removed|removed|i'?ll add|adding)\b", re.I)

# --- rule-based safety net for clear order / remove requests -------------------------------
QTY = {w: i for i, w in enumerate(UNITS[:11])}
QTY.update({"a": 1, "an": 1})
ORDER_RE = re.compile(r"\b(i'?d like|i would like|i want|i'?ll (have|take|get)|i will have|(can|could|may) i (get|have)|give me|get me|bring me|gimme|add|order|another|one more|let me (get|have))\b", re.I)
REMOVE_RE = re.compile(r"\b(remove|take off|take out|delete|get rid of|no more|don'?t want|cancel (?:the|my|that) (?!order))", re.I)
INFO_RE = re.compile(r"\b(allerg\w*|contains?|ingredients?|what'?s in|how much|price|cost|vegetarian|vegan|halal|gluten|dairy|nuts?|menu|recommend\w*|suggest\w*)\b", re.I)
CAT_RE = re.compile(r"\b(burgers?|chicken|rice|pasta|breakfast|sides?|drinks?|beverages?|desserts?|sweets?)\b", re.I)
CAT_ASK_RE = re.compile(r"\b(what|which|show|list|got|have|do you|menu)\b", re.I)
CAT_ALIAS = {"beverage": "Drinks", "beverages": "Drinks", "sweet": "Desserts", "sweets": "Desserts",
             "rice": "Rice & Pasta", "pasta": "Rice & Pasta"}
SMALLTALK = [   # (pattern, reply): handled in Python, no LLM needed
    (re.compile(r"^\W*(hello|hi|hey|good (morning|afternoon|evening)|how are you)\W*$", re.I),
     "Hello! What can I get for you today?"),
    (re.compile(r"^\W*(thanks?( you)?( very much)?|thank you so much)\W*$", re.I), "You're welcome! Anything else?"),
    (re.compile(r"^\W*(sorry|excuse me|pardon( me)?)\W*$", re.I), "No problem at all! What can I get you?"),
    (re.compile(r"^\W*(okay|ok|alright|sure|great|nice|good)\W*$", re.I), "Great! Anything else I can get you?"),
    (re.compile(r"^\W*(bye|goodbye|see you)\W*$", re.I), "Thank you for visiting Perpetual Bistro! Enjoy your meal."),
]
CONFIRM_RE = re.compile(r"\W*(confirm( (the |my )?order)?|place (the |my )?order|send it( to the kitchen)?)\W*", re.I)
DISCOUNT_RE = re.compile(r"\b(discounts?|promos?|promotions?|vouchers?|coupons?|senior(?: citizen)?s?|pwd|osca)\b", re.I)
DISCOUNT_REMOVE_RE = re.compile(r"\b(remove|cancel|clear|drop|no|without|never ?mind)\b.*\b(discounts?|promos?|vouchers?|coupons?)\b", re.I)
DISCOUNT_LIST_RE = re.compile(r"\b(what|which|any|list|available|do you (have|offer))\b.*\b(discounts?|promos?|promotions?|vouchers?)\b", re.I)
CODE_AFTER_RE = re.compile(r"\b(?:code|voucher|coupon)\s+(?:is\s+|number\s+|of\s+)?(?!code\b)([A-Za-z0-9-]{3,20})\b", re.I)
OFF_TOPIC_REPLY = "Sorry, I can only help with our menu and your order. What would you like?"
INSTR_RE = re.compile(r"\b(?:no|without|extra|less|light|hold the)\s+(?:the\s+)?(?!thanks|thank|more|problem)[a-z]+", re.I)


def _phrase_index():
    """tuple of words -> item name, for every menu name / nickname that identifies exactly one item."""
    seen = {}
    for it in db.menu_names_and_aliases():
        for ph in [re.sub(r"\s*\(.*?\)", "", it["name"])] + it["aliases"]:
            key = tuple(re.findall(r"[a-z0-9]+", ph.lower()))
            if key:
                seen.setdefault(key, set()).add(it["name"])
    return {k: next(iter(v)) for k, v in seen.items() if len(v) == 1}


def _match_phrases(words, index):
    """Longest-first, non-overlapping matches of menu names/nicknames (plural-tolerant) -> [(item, start, end)]."""
    maxlen = max((len(k) for k in index), default=0)
    out, i = [], 0
    while i < len(words):
        for length in range(min(maxlen, len(words) - i), 0, -1):
            cand = tuple(words[i:i + length])
            last, variants = cand[-1], [cand]
            if last.endswith("es"):
                variants.append(cand[:-1] + (last[:-2],))
            if last.endswith("s"):
                variants.append(cand[:-1] + (last[:-1],))
            hit = next((index[v] for v in variants if v in index), None)
            if hit:
                out.append((hit, i, i + length))
                i += length
                break
        else:
            i += 1
    return out


def fallback_parse(text: str):
    """Clear 'I want 2 fries no salt' / 'take off the nuggets' -> [(action, item, qty, instr)]."""
    if INFO_RE.search(text):
        return []                                           # questions are the LLM's job
    remove = bool(REMOVE_RE.search(text))
    intent = bool(ORDER_RE.search(text)) or bool(re.match(rf"\s*(?:{NUM}|an?)\b", text, re.I))
    if not (remove or intent):
        return []
    toks = [(m.group().lower(), m.start()) for m in re.finditer(r"[a-zA-Z0-9]+", text)]
    words = [w for w, _ in toks]
    matches = _match_phrases(words, _phrase_index())
    if not matches:
        return []
    found, occupied = {}, set()                             # item -> (first token, char position)
    for item, s0, e0 in matches:
        found.setdefault(item, (s0, toks[s0][1]))
        occupied.update(range(s0, e0))
    qty = {}
    for item, (s0, _) in found.items():
        for j in range(s0 - 1, max(s0 - 4, -1), -1):        # look up to 3 words back for a quantity
            w = words[j]
            if j in occupied or w in ("and", "plus", "with"):
                break
            if w.isdigit() or w in QTY:
                qty[item] = min(int(w) if w.isdigit() else QTY[w], 20)
                break
    instr = {item: [] for item in found}
    order = sorted(found, key=lambda k: found[k][1])
    for m in INSTR_RE.finditer(text):
        owner = [k for k in order if found[k][1] < m.start()]
        instr[owner[-1] if owner else order[0]].append(m.group().lower().replace("hold the", "no"))
    act = "remove" if remove else "add"
    return [(act, item, qty.get(item), ", ".join(instr[item])) for item in order]


SNAP_SKIP = set(UNITS) | set(TENS) | {"a", "an", "the", "and", "with", "no", "extra", "some", "more", "another"}


def snap_to_menu(t: str) -> str:
    """'fish a business sandwich' -> 'fishy business sandwich': fuzzy-match 2-4 word chunks to menu names/nicknames."""
    phrases = {" ".join(k): v for k, v in _phrase_index().items() if len(k) >= 2}
    words, out, i = t.split(), [], 0
    while i < len(words):
        for n in (4, 3, 2):
            chunk = words[i:i + n]
            if (len(chunk) < n or chunk[0] in SNAP_SKIP or chunk[-1] in SNAP_SKIP
                    or any(w.isdigit() or w in SNAP_SKIP - {"a", "an", "the"} for w in chunk)):
                continue                                     # never swallow a quantity, "no", "extra", "and"..., ("fish a business" is fine)
            hit = difflib.get_close_matches(" ".join(chunk), phrases, n=1, cutoff=0.82)
            if hit:
                out.append(hit[0])
                i += n
                break
        else:
            out.append(words[i])
            i += 1
    return " ".join(out)


def normalize_speech(text: str) -> str:
    """Fix typical speech-to-text slips before the text reaches the waiter."""
    t = text.lower().strip()
    t = re.sub(r"\bcheese burger(s?)\b", r"cheeseburger\1", t)       # Vosk splits compound words
    t = re.sub(r"\bice tea(s?)\b", r"iced tea\1", t)
    homophones = {"for": "four", "to": "two", "too": "two", "won": "one", "ate": "eight", "tree": "three"}
    t = re.sub(r"\b(table(?:\s+(?:number|no\.?))?|at)\s+(for|to|too|won|ate|tree)\b",       # "table for" -> "table four"
               lambda m: f"{m.group(1)} {homophones[m.group(2)]}", t)
    t = re.sub(r"\bi (?:was|wont|wan) (?=a\b|an\b|the\b|\d|one|two|three|four|five)", "i want ", t)   # "i was a cheesy..."
    t = re.sub(r"\bi have (?=some\b|a\b|an\b|\d|one|two|three|four|five)", "i'll have ", t)       # "i have some fishy..."
    firsts = "|".join(sorted({k[0] for k in _phrase_index()}, key=len, reverse=True))
    if firsts:                                                        # "to fries" -> "two fries"
        t = re.sub(rf"\b(?:to|too)\b(?=\s+(?:{firsts})(?:es|s)?\b)", "two", t)
    return snap_to_menu(t)


def parse_text_tool_calls(content):
    """Recover tool calls that the model emitted as plain JSON text instead of structured calls."""
    s = re.sub(r"</?tool_call>|```(?:json)?", "", content or "").strip()
    if not s.startswith(("{", "[")):
        return []
    try:
        data = json.loads(s)
    except ValueError:
        return []
    items = data if isinstance(data, list) else [data]
    return [{"name": d["name"], "arguments": d.get("arguments") or {}}
            for d in items if isinstance(d, dict) and d.get("name") in TOOL_NAMES]


# ---------------------------------------------------------------- default LLM backend
_NO_THINK_UNSUPPORTED = set()      # models that reject think=False (qwen2.5, llama3 ...): remembered after the first try


def ollama_chat(messages, tools, model=DEFAULT_MODEL):
    """Normalised wrapper: returns {"content": str, "tool_calls": [{"name", "arguments"}]}.
    Reasoning models (Qwen3, Gemma 4) are run with thinking OFF: a waiter must answer in seconds, not think for a minute."""
    import ollama
    kw = dict(model=model, messages=messages, tools=tools, keep_alive="30m",
              options={"temperature": 0, "num_ctx": 3072, "num_predict": 160})
    if model in _NO_THINK_UNSUPPORTED:
        r = ollama.chat(**kw)
    else:
        try:
            r = ollama.chat(think=False, **kw)
        except TypeError:                                    # old ollama python package without the think option
            _NO_THINK_UNSUPPORTED.add(model)
            r = ollama.chat(**kw)
        except Exception as e:  # noqa: BLE001
            if "think" not in str(e).lower():
                raise
            _NO_THINK_UNSUPPORTED.add(model)                 # model has no thinking mode: just call it normally
            r = ollama.chat(**kw)
    m = r.message
    calls = [{"name": tc.function.name, "arguments": dict(tc.function.arguments or {})}
             for tc in (m.tool_calls or [])]
    return {"content": m.content or "", "tool_calls": calls}


def format_tab(order: dict, with_total=True) -> str:
    if not order["lines"]:
        return "(empty)"
    out = []
    for l in order["lines"]:
        note = f"  [{l['notes']}]" if l["notes"] else ""
        out.append(f"  {l['qty']} x {l['item']:<22}{l['line_total_php']:>8.2f} PHP{note}")
    if with_total:
        d = order.get("discount")
        out += ["  " + "-" * 40, f"  {'Subtotal':<27}{order['subtotal_php']:>8.2f} PHP"]
        if d and d["valid"]:
            out.append(f"  {'Discount (' + d['code'] + ')':<27}{-order['discount_php']:>8.2f} PHP")
        out += [f"  {'Service Fee (10%)':<27}{order['service_fee_php']:>8.2f} PHP",
                f"  {'GRAND TOTAL':<27}{order['grand_total_php']:>8.2f} PHP"]
        if d and not d["valid"]:
            out.append(f"  ! {d['code']} not applied: {d['reason']}.")
        elif d and d["requires_id"]:
            out.append("  * Please show a valid ID to our staff for this discount.")
    return "\n".join(out)


def tool_note(name, args, r):
    """One plain-text line describing what a tool call did (built from the DB result, not the LLM)."""
    if name == "add_item_to_order":
        if r.get("ok"):
            extra = f" ({r['instructions']})" if r.get("instructions") else ""
            return f"Added {r['qty']} x {r['added']}{extra} - {r['qty'] * r['unit_price_php']:.2f} PHP"
        hint = f" Did you mean: {', '.join(r['did_you_mean'])}?" if r.get("did_you_mean") else ""
        return f"Couldn't add {args.get('item_name')}: {r.get('error')}.{hint}"
    if name == "remove_item_from_order":
        return (f"Removed {r['qty']} x {r['removed']}" if r.get("ok")
                else f"Couldn't remove {args.get('item_name')}: {r.get('error')}")
    return None


class WaiterAgent:
    def __init__(self, chat_fn=None, model=DEFAULT_MODEL):
        self.chat_fn = chat_fn or (lambda msgs, tools: ollama_chat(msgs, tools, model))
        self.model = model
        self.state = State.GREETING
        self.table_no = None
        self.session_id = None
        self.messages = []          # multi-turn LLM memory
        self.log = []               # for sample_conversation_log.json

    # ------------------------------------------------------------ logging / transitions
    def _log(self, event, **data):
        self.log.append({"time": datetime.now().isoformat(timespec="seconds"),
                         "state": self.state.value, "event": event, **data})

    def _go(self, new_state: State):
        self._log("transition", from_state=self.state.value, to_state=new_state.value)
        self.state = new_state

    def save_log(self, path):
        snap = db.db_snapshot(self.session_id) if self.session_id else None
        with open(path, "w", encoding="utf-8") as f:
            json.dump({"model": self.model, "table_no": self.table_no, "session_id": self.session_id,
                       "final_state": self.state.value, "events": self.log, "database_state": snap},
                      f, indent=2, ensure_ascii=False)

    # ------------------------------------------------------------ public API
    def start(self) -> str:
        msg = "Welcome to Perpetual Bistro! What's your table number?"
        self._log("assistant", text=msg)
        return msg

    @property
    def finished(self) -> bool:
        return self.state in (State.CONFIRMED, State.CANCELLED)

    def handle(self, text: str) -> str:
        self._log("user", text=text)
        reply = None
        if self.state in (State.TAKING_ORDER, State.REVIEW):
            reply = self._maybe_change_table(text) or self._maybe_discount(text)
        if reply is None:
            handler = {State.GREETING: self._greeting, State.TAKING_ORDER: self._taking_order,
                       State.REVIEW: self._review}.get(self.state)
            reply = handler(text) if handler else "This order is closed. Please start a new session."
        self._log("assistant", text=reply)
        return reply

    # ------------------------------------------------------------ discounts (deterministic, never the LLM)
    def _maybe_discount(self, text):
        """'promo code SAVE50', 'I have a senior discount', 'remove the discount', 'what discounts are there'.
        Returns a reply, or None if the message is not about discounts."""
        low = text.lower()
        has_word = bool(DISCOUNT_RE.search(text))
        if has_word and DISCOUNT_REMOVE_RE.search(text):
            res = db.remove_discount(self.session_id)
            self._log("tool_call", tool="remove_discount", arguments={}, result=res, source="discount")
            return self._discount_reply("Okay, I removed the discount." if res["ok"] else "There's no discount on your tab.")
        if has_word and DISCOUNT_LIST_RE.search(text):
            rows = db.list_discounts()
            lines = [f"- {r['code']}: {r['description']}" + (f" (min. spend {r['min_spend']:.0f} PHP)" if r["min_spend"] else "")
                     for r in rows]
            return ("Here are today's discounts:\n" + "\n".join(lines) +
                    "\n\nSay 'promo code' followed by the code to use one.") if lines else "There are no discounts available today."
        known = db.match_discount_in_text(text)
        m = CODE_AFTER_RE.search(text)
        guess = None if known else (m.group(1) if m else None)
        if not (known or guess):
            if has_word:     # mentions discounts but gave no code
                return "Sure! Please tell me the promo code, or say 'senior discount' or 'PWD discount'."
            return None
        if fallback_parse(text) and not (m or has_word):    # "2 big macs" must never be read as a discount
            return None
        res = db.apply_discount(self.session_id, known or guess)
        self._log("tool_call", tool="apply_discount", arguments={"code": known or guess}, result=res, source="discount")
        if not res["ok"]:
            return self._discount_reply(f"Sorry, {res['error']}.")
        note = f"{res['code']} applied: {res['description']}."
        if res["requires_id"]:
            note += " Please show a valid ID to our staff."
        return self._discount_reply(note)

    def _discount_reply(self, note):
        if self.state == State.REVIEW:
            return note + "\n\n" + self._review_prompt(db.view_current_order(self.session_id))
        o = db.view_current_order(self.session_id)
        saved = f" (saving {o['discount_php']:.2f} PHP)" if o["discount_php"] else ""
        return f"{note}\nYour tab: subtotal {o['subtotal_php']:.2f} PHP{saved}.\n\nAnything else I can get you?"

    # ------------------------------------------------------------ STATE_GREETING
    def _greeting(self, text):
        table = parse_table(text, bare=True)
        if not table:
            return ("Sorry, I didn't catch your table number. Which table are you at? "
                    "(for example: 'table 4')")
        busy = table_blocked(table)
        if busy:
            self._log("guard", text=f"table {table} refused: {busy}")
            return f"Sorry, {busy[0].lower()}{busy[1:]} Please choose another table."
        self.table_no = table
        self.session_id = db.create_session(table)
        self._go(State.TAKING_ORDER)
        welcome = (f"Great, table {table}! I can tell you about our menu, check allergens, and take "
                   "your order. What would you like?")
        if fallback_parse(text):                            # "table 4, I'd like 2 cheeseburgers"
            return f"Great, table {table}!\n" + self._taking_order(text)
        return welcome

    def _maybe_change_table(self, text):
        n = parse_table(text)                               # explicit mentions only ("table 7")
        if not n:
            return None
        if n == self.table_no:
            return f"You're already at table {n}. What can I get you?"
        busy = table_blocked(n, self.session_id)
        if busy:
            self._log("guard", text=f"table change to {n} refused: {busy}")
            return f"Sorry, {busy[0].lower()}{busy[1:]} You're still at table {self.table_no}; please choose another table."
        old, self.table_no = self.table_no, n
        db.set_table(self.session_id, n)
        self._log("table_change", from_table=old, to_table=n)
        return f"No problem, I've moved your order from table {old} to table {n}."

    # ------------------------------------------------------------ STATE_TAKING_ORDER
    def _taking_order(self, text):
        if CANCEL_RE.search(text):
            db.set_session_status(self.session_id, "CANCELLED")
            self._go(State.CANCELLED)
            return "No problem, your order has been cancelled. Come back anytime!"
        if DONE_RE.search(text) or CONFIRM_RE.fullmatch(text):
            order = db.view_current_order(self.session_id)
            if not order["lines"]:
                return "Your tab is empty so far. What would you like to order?"
            self._go(State.REVIEW)
            return self._review_prompt(order)
        return self._respond(text)

    def _respond(self, text):
        """small talk and off-topic guard in Python, then the fast path, and the LLM only as the last resort."""
        for rx, reply in SMALLTALK:
            if rx.match(text):
                return reply
        if not vocab.in_domain(text):                       # no restaurant word at all: do not even ask the LLM
            self._log("guard", text="off-topic message answered without the LLM")
            return OFF_TOPIC_REPLY
        return self._fast_path(text) or self._llm_turn(text)

    def _fast_path(self, text):
        """Answer clear orders and plain menu browsing with plain Python (milliseconds). None -> ask the LLM."""
        actions = fallback_parse(text)                      # "2 fries no salt", "take off the nuggets"
        if actions:
            return self._compose(self._apply_fallback(actions), "Anything else I can get you?")
        m = CAT_RE.search(text)
        if m and CAT_ASK_RE.search(text) and not INFO_RE.search(text.replace("menu", "")):
            word = m.group(1).lower()
            res = db.get_menu_by_category(CAT_ALIAS.get(word, word))
            if res["ok"] and res["items"]:
                self._log("tool_call", tool="get_menu_by_category", arguments={"category": word}, result=res,
                          source="fast_path")
                lines = [f"- {i['name']}: {i['price_php']:.0f} PHP" + ("" if i["available"] else " (sold out)")
                         for i in res["items"]]
                return "Here's what we have:\n" + "\n".join(lines) + "\n\nWhat would you like?"
        return None

    def _trim(self):
        """Keep only the last few user turns so the prompt stays small (cuts at a user message: no orphan tool msgs)."""
        idx = [i for i, m in enumerate(self.messages)
               if m["role"] == "user" and not m["content"].startswith("SYSTEM NOTICE")]
        if len(idx) > KEEP_TURNS:
            self.messages = [self.messages[0]] + self.messages[idx[-KEEP_TURNS]:]

    def _compose(self, notes, reply):
        """Deterministic acknowledgement (from tool results) + the model's friendly text."""
        o = db.view_current_order(self.session_id)
        count = sum(l["qty"] for l in o["lines"])
        tab = f"Your tab: {count} item(s), subtotal {o['subtotal_php']:.2f} PHP."
        return "\n".join(notes + [tab]) + (f"\n\n{reply}" if reply else "")

    def _apply_fallback(self, actions):
        notes = []
        for act, item, qty, instr in actions:
            if act == "add":
                name, args = "add_item_to_order", {"item_name": item, "qty": qty or 1, "instructions": instr}
            else:
                name, args = "remove_item_from_order", {"item_name": item, "qty": qty}
            result = self._run_tool(name, args)
            dbg(f"[FALLBACK] {name}({args}) -> {json.dumps(result)[:200]}")
            self._log("tool_call", tool=name, arguments=args, result=result, source="rule_fallback")
            note = tool_note(name, args, result)
            if note:
                notes.append(note)
        return notes

    def _llm_turn(self, text):
        tab = format_tab(db.view_current_order(self.session_id), with_total=False)
        system = {"role": "system", "content": SYSTEM_PROMPT.format(table=self.table_no, tab=tab)}
        if self.messages and self.messages[0]["role"] == "system":
            self.messages[0] = system            # refresh ground truth each turn
        else:
            self.messages.insert(0, system)
        self.messages.append({"role": "user", "content": text})
        self._trim()

        expected = fallback_parse(text)          # what a clear reading of the request would do
        called_any, nudged, notes = False, False, []
        for _ in range(MAX_TOOL_ROUNDS):
            try:
                resp = self.chat_fn(self.messages, TOOLS)
            except Exception as e:               # Ollama down, model missing, etc.
                self._log("error", error=repr(e))
                dbg(f"[ERROR] LLM call failed: {e!r}")
                return ("Sorry, my kitchen assistant is unavailable right now. "
                        f"(Is Ollama running? Try `ollama serve` and `ollama pull {self.model}`.)")
            calls = resp["tool_calls"]
            if not calls:                        # Qwen sometimes writes the tool call as plain JSON text
                calls = parse_text_tool_calls(resp["content"])
                if calls:
                    resp["content"] = ""
            self.messages.append({"role": "assistant", "content": resp["content"],
                                  **({"tool_calls": [{"function": c} for c in calls]} if calls else {})})
            if not calls:
                reply = resp["content"].strip()
                dbg(f"[LLM] no tool call. reply={reply!r}")
                # guard: request needed an order change (or the model claims one) but no tool was called
                if not called_any and (CLAIM_RE.search(reply) or expected):
                    if not nudged:
                        nudged = True
                        self._log("guard", text="No tool call for an order request; retrying")
                        self.messages.append({"role": "user", "content":
                            "SYSTEM NOTICE: The customer's request needs an order change, but you did not call "
                            "a tool, so nothing changed. Call add_item_to_order or remove_item_from_order now, "
                            "using the exact menu item name (use get_menu_by_category first if unsure)."})
                        continue
                    if expected:                 # still nothing: apply the clear request ourselves
                        final = self._compose(self._apply_fallback(expected), "Anything else I can get you?")
                        self.messages.append({"role": "assistant", "content": final})
                        return final
                    return ("Sorry, I couldn't update your order just now. Could you repeat that, "
                            "for example: 'add 1 Mango Smoothie'?")
                return self._compose(notes, reply) if notes else (reply or "Anything else I can get for you?")
            all_ok = True
            for c in calls:
                called_any = True
                result = self._run_tool(c["name"], c["arguments"])
                dbg(f"[TOOL] {c['name']}({c['arguments']}) -> {json.dumps(result)[:300]}")
                self._log("tool_call", tool=c["name"], arguments=c["arguments"], result=result, source="llm")
                note = tool_note(c["name"], c["arguments"], result)
                if note:
                    notes.append(note)
                all_ok = all_ok and result.get("ok", False) and c["name"] in ("add_item_to_order", "remove_item_from_order")
                self.messages.append({"role": "tool", "tool_name": c["name"], "content": json.dumps(result)})
            if all_ok:                           # plain add/remove succeeded: skip the second (slow) LLM round
                final = self._compose(notes, "Anything else I can get you?")
                self.messages.append({"role": "assistant", "content": final})
                return final
        return self._compose(notes, "") if notes else "Sorry, I got a bit tangled up there. Could you say that again?"

    def _run_tool(self, name, args):
        args = dict(args or {})
        try:
            if name == "get_menu_by_category":
                return db.get_menu_by_category(args.get("category", "all"))
            if name == "check_item_allergen":
                return db.check_item_allergen(args.get("item_id"), args.get("allergen", ""))
            if name == "add_item_to_order":
                return db.add_item_to_order(self.session_id, args.get("item_name", ""),
                                            args.get("qty", 1), args.get("instructions", ""))
            if name == "remove_item_from_order":
                return db.remove_item_from_order(self.session_id, args.get("item_name", ""), args.get("qty"))
            if name == "view_current_order":
                return db.view_current_order(self.session_id)
            return {"ok": False, "error": f"Unknown tool '{name}'"}
        except Exception as e:                   # never let a bad tool call crash the app
            return {"ok": False, "error": f"Tool failed: {e}"}

    # ------------------------------------------------------------ STATE_REVIEW
    def _review_prompt(self, order):
        return ("Here's your order:\n" + format_tab(order) +
                "\n\nSay 'confirm' to send it to the kitchen, or tell me what to change.")

    def _review(self, text):
        if CANCEL_RE.search(text):
            db.set_session_status(self.session_id, "CANCELLED")
            self._go(State.CANCELLED)
            return "Order cancelled. Thanks for stopping by!"
        if YES_RE.search(text) and not re.search(r"\b(add|remove|change)\b", text, re.I):
            return self._confirm()
        if EDIT_RE.search(text):
            self._go(State.TAKING_ORDER)
            if re.fullmatch(r"\W*(no|nope|wait)\W*", text.strip(), re.I):
                return "Sure, what would you like to change?"
            return self._respond(text)
        return "Please say 'confirm' to place the order, or tell me what you'd like to change."

    # ------------------------------------------------------------ UI hook (dashboard CONFIRM button)
    def confirm_from_ui(self) -> str:
        """Button press: TAKING_ORDER -> REVIEW -> CONFIRMED (or REVIEW -> CONFIRMED)."""
        if self.state not in (State.TAKING_ORDER, State.REVIEW):
            return "There's no open order to confirm yet."
        if not db.view_current_order(self.session_id)["lines"]:
            return "Your tab is empty. Add something first!"
        self._log("ui_action", text="Pressed CONFIRM ORDER")
        if self.state == State.TAKING_ORDER:
            self._go(State.REVIEW)
        reply = self._confirm()
        self._log("assistant", text=reply)
        return reply

    # ------------------------------------------------------------ STATE_CONFIRMED
    def _confirm(self):
        result = db.commit_order(self.session_id)
        if not result["ok"]:
            self._go(State.TAKING_ORDER)
            return f"Sorry, I couldn't place the order: {result['error']} What would you like to change?"
        self._go(State.CONFIRMED)
        return ("ORDER CONFIRMED - sent to the kitchen!\n"
                f"RECEIPT  Table {self.table_no}  Session #{self.session_id}\n" + "=" * 44 + "\n"
                + format_tab(result["receipt"]) + "\n" + "=" * 44 + "\nThank you, enjoy your meal!")
