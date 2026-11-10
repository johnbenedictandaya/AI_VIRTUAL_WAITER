"""waiter_vocab.py - the closed vocabulary of the Perpetual Bistro waiter.

One place that answers "is this a word a customer would say to a waiter?". It is built from
  * the live menu (item names, nicknames, categories, allergens)  -> follows the database, so new items work at once
  * a fixed list of waiter words (numbers, table, ordering verbs, polite words, common toppings)

Used in three places:
  speech_engine.py   Vosk grammar (the recogniser can only output these words) + filter on Whisper/Vosk output
  fsm_controller.py  off-topic guard: a message with no restaurant word never reaches the LLM
  (tests)            python waiter_vocab.py "what is the capital of france"
"""
import difflib
import json
import re
import sys

import db_tools as db

TOKEN = re.compile(r"[a-z0-9']+")

NUMBERS = ("zero one two three four five six seven eight nine ten eleven twelve thirteen fourteen fifteen sixteen "
           "seventeen eighteen nineteen twenty thirty forty fifty sixty seventy eighty ninety hundred "
           "first second third").split()

# small words that glue sentences together; allowed, but they do NOT make a message "about the restaurant"
GLUE = set("""a an the and or with without of for to in on at it its it's this that these those is are was were be
been am do does did can could would will shall may might should i i'd i'll i'm i've me my mine we we're we'd we'll
we've us our you your you're you'll he she they them there here what what's whats which who how when where why some
any if but so just only also too very really maybe about then now not don't dont doesn't didn't isn't aren't won't
can't cannot let lets let's up down from by as into over under again still yet already ate won tree wont wan""".split())

# words that make a message "restaurant talk"
CONTENT = set(NUMBERS) | set("""
order orders ordering add remove take off out cancel change make swap instead replace confirm confirmed yes yeah yep
yup nope no okay ok wait done finished finish ready bill check checkout total subtotal price prices cost costs much
many menu recommend recommendation suggest popular best special specials favorite favourite allergy allergic
allergen allergens contain contains ingredient ingredients vegetarian vegan halal spicy sweet hot cold ice iced warm
fresh extra less light hold side sauce ketchup mayo mayonnaise mustard salt pepper sugar cream milk onion onions
pickles pickle cheese lettuce tomato tomatoes bacon egg eggs ham sausage gravy spice
table tables number seat seated sitting
hello hi hey thanks thank welcome goodbye bye please sorry pardon excuse
want get give bring have got serve sell offer need
drink drinks eat hungry thirsty food meal dish dishes plate another more same all everything nothing anything
tray cart tab receipt pay kitchen minutes minute long quick fast
delicious
discount discounts promo promos promotion code codes voucher coupon senior citizen citizens pwd osca disability person
applied apply welcome save
small medium large big regular
one's two's piece pieces pc pcs six pc
""".split())


def _forms(words):
    out = set()
    for w in words:
        w = w.lower().strip()
        if w:
            out.add(w)
            out.add(w.replace("'", ""))
    return out


GLUE_F, CONTENT_F = _forms(GLUE), _forms(CONTENT)
_cache = {"sig": None, "words": set(), "content": set(), "pairs": set(), "repair": [], "grammar": "[]"}


def _tokens(text):
    return TOKEN.findall((text or "").lower())


def _menu_words():
    """(all menu words, 'strong' words, name/nickname word pairs, menu signature).
    Strong = nicknames, categories, allergens. A word that only occurs inside a funny item name ('time', 'life',
    'business') is NOT enough to count as restaurant talk on its own; the full item name (word pairs) is."""
    words, strong, pairs = set(), set(), set()
    with db.db() as c:
        sig = tuple(c.execute("SELECT COUNT(*), COALESCE(SUM(LENGTH(name||category||aliases||allergens)),0) "
                              "FROM menu_items").fetchone())
        for r in c.execute("SELECT name, category, aliases, allergens FROM menu_items"):
            phrases = [r["name"]] + r["aliases"].split(",")
            for ph in phrases:
                toks = _tokens(re.sub(r"[-&]", " ", ph))
                words.update(toks)
                pairs.update(zip(toks, toks[1:]))
                joined = _tokens(ph.replace("-", ""))                     # "Egg-cellent" also as "eggcellent"
                words.update(joined)
            for field in (r["aliases"].replace(",", " "), r["category"], r["allergens"].replace(",", " ")):
                toks = _tokens(re.sub(r"[-&]", " ", field))
                words.update(toks)
                strong.update(toks)
    return words, strong, pairs, sig


def _load():
    try:
        menu, strong, pairs, sig = _menu_words()
    except Exception:                                        # no database yet: static words only
        menu, strong, pairs, sig = set(), set(), set(), ("none",)
    if _cache["sig"] == sig:
        return _cache
    words = GLUE_F | CONTENT_F | _forms(menu)
    content = CONTENT_F | (_forms(strong) - GLUE_F)
    _cache.update(sig=sig, words=words, content=content, pairs=pairs,
                  repair=sorted(w for w in (CONTENT_F | _forms(menu) - GLUE_F) if len(w) >= 5),
                  grammar=json.dumps(sorted(w for w in words if re.fullmatch(r"[a-z']+", w)) + ["[unk]"]))
    return _cache


def _known(tok, words):
    """Exact, or a plural / possessive of a known word ('fries', 'burgers', "burger's")."""
    if tok in words:
        return tok
    for suf in ("'s", "es", "s"):
        if tok.endswith(suf) and tok[: -len(suf)] in words:
            return tok
    return None


def grammar_json() -> str:
    """Word list for Vosk's restricted grammar (plus [unk] so noise has somewhere to go)."""
    return _load()["grammar"]


def filter_text(text):
    """Keep only waiter vocabulary. Returns (clean_text, dropped_words).
    Only near-identical misspellings are repaired ('chese' -> 'cheese'); look-alikes ('weather' / 'water') are dropped."""
    v = _load()
    words, kept, dropped = v["words"], [], []
    for tok in _tokens(text):
        if tok.isdigit() or _known(tok, words):
            kept.append(tok)
            continue
        close = difflib.get_close_matches(tok, v["repair"], n=1, cutoff=0.88) if len(tok) >= 5 else []
        if close:
            kept.append(close[0])
        else:
            dropped.append(tok)
    return " ".join(kept), dropped


ORDER_INTENT = re.compile(r"\b(i'?d like|i would like|i want|i'?ll (have|take|get)|can i (get|have)|give me|get me|bring me)\b")


def in_domain(text) -> bool:
    """True if the message has a restaurant word (nickname, number, ordering verb ...) or a full item-name phrase."""
    v = _load()
    toks = _tokens(text)
    if ORDER_INTENT.search((text or "").lower()):
        return True
    if any(t.isdigit() or _known(t, v["content"]) for t in toks):
        return True
    return any(p in v["pairs"] for p in zip(toks, toks[1:]))


if __name__ == "__main__":
    db.init_db()
    for line in sys.argv[1:] or ["i would like two cheeseburgers no onions please",
                                 "what is the capital of france", "play some music and tell me a joke"]:
        clean, dropped = filter_text(line)
        print(f"{line!r}\n   in_domain={in_domain(line)}  kept={clean!r}  dropped={dropped}")
