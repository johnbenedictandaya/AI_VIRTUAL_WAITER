"""restaurant_vocab.py - restaurant vocabulary for the Perpetual Bistro voice pipeline.

Does two jobs:
  1. build_vocab(model)   -> word list for Vosk's grammar mode (biases the recogniser toward ordering speech)
  2. apply_corrections(t) -> fixes known mishearings ("fish a business" -> "fishy business") on the transcript

The menu part is built from the database, so new items added in Manager mode are picked up automatically.

Usage
    python restaurant_vocab.py                 # print the vocabulary and write vocab_preview.txt
    python restaurant_vocab.py --model model   # also report which words the Vosk model does NOT know

Optional files next to this one:
    vocab_extra.txt        one extra word or phrase per line (your own additions)
    vocab_corrections.txt  lines like:  fish a business => fishy business
"""
import re
from pathlib import Path

HERE = Path(__file__).parent

# ---------------------------------------------------------------- fixed ordering vocabulary
NUMBER_WORDS = ("zero one two three four five six seven eight nine ten eleven twelve thirteen fourteen fifteen "
                "sixteen seventeen eighteen nineteen twenty thirty forty fifty sixty seventy eighty ninety "
                "hundred").split()

ORDERING_WORDS = """
i would like want need get have take give bring me us we i'd i'll i'm please thanks thank you
a an the some another more one extra add remove delete cancel change instead of and with without no
only just also plus or to for at in on is are it that this those these
table number seat sitting
order orders tab bill check checkout review confirm confirmed yes yeah yep nope okay ok correct proceed
send kitchen done finished all that's finish ready wait stop forget everything
menu what do you have recommend recommendation suggest popular special price cost much how many
allergy allergies allergic allergen allergens contain contains ingredients
gluten dairy egg eggs soy fish sesame nuts peanut peanuts shellfish milk wheat
vegetarian vegan halal spicy
less light hold ice sauce onions onion pickles cheese lettuce tomato mayo ketchup mustard salt sugar cream
hot cold large small medium regular size meal combo
burgers burger chicken rice pasta breakfast sides drinks drink desserts dessert
""".split()

# nicknames people actually say that may not be in the SQL aliases
EXTRA_NICKNAMES = [
    "cheeseburger", "hamburger", "mcdo", "chickenjoy", "fries", "coke", "sprite", "royal", "float",
    "pancakes", "sundae", "sundaes", "milkshake", "iced tea", "nuggets",
]

# ---------------------------------------------------------------- known mishearings -> what the customer meant
# Left side is a regex (word-bounded automatically), right side the replacement. Add to this as your logs show new slips.
CORRECTIONS = {
    r"i was a": "i want a",                    # real log: "i was a cheesy does it"
    r"i won't a": "i want a",
    r"i want to a": "i want a",
    r"fish a business": "fishy business",      # real log: "i have some fish a business sandwich"
    r"fish he business": "fishy business",
    r"fishy bus ness": "fishy business",
    r"cheese he does it": "cheesy does it",
    r"big mac daddy": "big mack daddy",
    r"cluck north is": "cluck norris",
    r"fried hard": "fry hard",
    r"hashtag brown": "hash tag brown",
    r"swirl me may be": "swirl me maybe",
    r"sundae fun day": "sundae funday",
    r"cone boy age": "cone voyage",
}


def _tokens(text):
    return re.findall(r"[a-z0-9']+", (text or "").lower())


def _file_lines(name):
    p = HERE / name
    if not p.exists():
        return []
    return [l.strip() for l in p.read_text(encoding="utf-8").splitlines() if l.strip() and not l.startswith("#")]


# ---------------------------------------------------------------- corrections
def _compiled():
    pairs = dict(CORRECTIONS)
    for line in _file_lines("vocab_corrections.txt"):
        if "=>" in line:
            a, b = line.split("=>", 1)
            pairs[re.escape(a.strip().lower())] = b.strip().lower()
    # longest pattern first so specific fixes win over short ones
    return [(re.compile(rf"\b{pat}\b"), rep) for pat, rep in sorted(pairs.items(), key=lambda kv: -len(kv[0]))]


def apply_corrections(text: str) -> str:
    """Fix known speech-to-text slips. Call this first thing in normalize_speech()."""
    t = (text or "").lower()
    for rx, rep in _compiled():
        t = rx.sub(rep, t)
    return t


# ---------------------------------------------------------------- vocabulary
def menu_words():
    """Every word of every menu name and nickname, read from the database."""
    import db_tools as db
    words = set()
    for it in db.menu_names_and_aliases():
        words.update(_tokens(re.sub(r"\(.*?\)", " ", it["name"])))
        for a in it["aliases"]:
            words.update(_tokens(a))
    return words


def build_vocab(model=None, include_unk=True):
    """Word list for vosk.KaldiRecognizer(model, 16000, json.dumps(vocab)).

    If a Vosk model is given, words it does not know are dropped (Vosk ignores them anyway and
    complains in the log). '[unk]' stays so speech outside the list does not get forced onto menu words.
    """
    words = set(NUMBER_WORDS) | set(ORDERING_WORDS)
    for n in EXTRA_NICKNAMES:
        words.update(_tokens(n))
    for line in _file_lines("vocab_extra.txt"):
        words.update(_tokens(line))
    try:
        words |= menu_words()
    except Exception as exc:  # noqa: BLE001 - database not ready: still return the fixed vocabulary
        print(f"[vocab] menu words unavailable ({exc}); using fixed vocabulary only")
    if model is not None:
        words = {w for w in words if _known(model, w)}
    words = {w for w in words if len(w) > 1 or w in ("a", "i")}      # drop fragments like "y" from "Corn-y"
    vocab = sorted(words)
    return vocab + ["[unk]"] if include_unk else vocab


def _known(model, word):
    """True if the Vosk model has this word (older bindings without find_word: assume yes)."""
    finder = getattr(model, "find_word", None)
    if finder is None:
        return True
    try:
        return finder(word) >= 0
    except Exception:  # noqa: BLE001
        return True


def unknown_words(model):
    """Words we want but the model's dictionary lacks. Replace them with real-world aliases."""
    wanted = build_vocab(None, include_unk=False)
    return [w for w in wanted if not _known(model, w)]


if __name__ == "__main__":
    import argparse
    p = argparse.ArgumentParser(description="Preview the restaurant vocabulary")
    p.add_argument("--model", help="path to the Vosk model folder, to report words it does not know")
    args = p.parse_args()

    vocab = build_vocab(None)
    out = HERE / "vocab_preview.txt"
    out.write_text("\n".join(vocab) + "\n", encoding="utf-8")
    print(f"{len(vocab)} words written to {out.name}")

    if args.model:
        import vosk
        vosk.SetLogLevel(-1)
        m = vosk.Model(args.model)
        missing = unknown_words(m)
        print(f"{len(missing)} words are not in the model dictionary and will be ignored:")
        print(", ".join(missing) or "(none)")
    print("\nCorrections test:")
    for s in ("i was a cheesy does it", "i have some fish a business sandwich"):
        print(f"  {s!r} -> {apply_corrections(s)!r}")
