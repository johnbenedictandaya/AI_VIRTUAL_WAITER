"""bench_models.py - which Ollama model is the best waiter ON YOUR MACHINE?

    python bench_models.py                              # every candidate that is already pulled
    python bench_models.py --models qwen3:4b qwen3:8b   # pick your own
    python bench_models.py --models gemma4:e4b -v       # show every reply

For each model it runs the same realistic customer sentences through the waiter's LLM turn (the fast path is
bypassed on purpose) and reports
    LLM ok   the MODEL itself made the right tool calls / answer (the rule-based safety net was not needed)
    final ok the cart ends up right (safety net included): what the customer really experiences
    time     seconds per turn (the first, model-loading call is excluded)
A throw-away database is used, so restaurant.db is never touched.
Pull candidates first, e.g.:  ollama pull qwen3:4b   ollama pull qwen3:8b   ollama pull gemma4:e4b
"""
import argparse
import statistics
import tempfile
import time
from pathlib import Path

import db_tools as db
import fsm_controller as f

CANDIDATES = ["qwen3:4b", "qwen3:8b", "gemma4:e4b", "qwen2.5:7b", "qwen2.5:3b", "llama3.1:8b"]


def lines(agent):
    return {l["item"]: (l["qty"], l["notes"].lower()) for l in db.view_current_order(agent.session_id)["lines"]}


def qty(cart, name):
    return cart.get(name, (0, ""))[0]


# (label, customer says, items already on the tab, outcome check(cart, reply, llm_tool_names))
CASES = [
    ("quantity + note", "I'd like two cheeseburgers, no onions", {},
     lambda c, r, t: qty(c, "Cheesy Does It") == 2 and "onion" in c["Cheesy Does It"][1]),
    ("two nicknames", "can I get a coke and some fries", {},
     lambda c, r, t: qty(c, "Bubble Trouble Cola") == 1 and qty(c, "Fry Hard") == 1),
    ("brand nickname", "gimme a big mac and an apple pie", {},
     lambda c, r, t: qty(c, "Big Mack Daddy") == 1 and qty(c, "Apple of My Pie") == 1),
    ("garbled speech", "i want fish a business sandwich", {},
     lambda c, r, t: qty(c, "Fishy Business Sandwich") == 1),
    ("remove", "take off the nuggets", {"Nugget Life Chose Me (6pc)": 2},
     lambda c, r, t: qty(c, "Nugget Life Chose Me (6pc)") == 0),
    ("reduce", "make it just one fries", {"Fry Hard": 3},
     lambda c, r, t: qty(c, "Fry Hard") == 1),
    ("swap vegetarian", "make that vegetarian please", {"Big Mack Daddy": 1},
     lambda c, r, t: qty(c, "Big Mack Daddy") == 0 and qty(c, "Lettuce Be Friends Veggie Burger") == 1),
    ("allergen question", "does the fried chicken have gluten?", {},
     lambda c, r, t: not c and "gluten" in r.lower() and any(x in r.lower() for x in ("yes", "contain", "has")),),
    ("menu question", "what drinks do you have?", {},
     lambda c, r, t: not c and "Brew-tiful" in r and "Wake-Up" in r),
    ("not on menu", "I'd like a mojito", {},
     lambda c, r, t: not c and any(x in r.lower() for x in ("sorry", "not", "don't", "do not", "unfortunately", "menu"))),
    ("sold out item", "one mango pie please", {},
     lambda c, r, t: qty(c, "Mango Tango Pie") == 0 and any(x in r.lower() for x in ("out", "sorry", "unavailable", "stock"))),
    ("off-topic", "who won the basketball game last night?", {},
     lambda c, r, t: not c and not t),
]


def installed():
    try:
        import ollama
        return {m.model for m in ollama.list().models}
    except Exception:  # noqa: BLE001
        return None


def run_case(model, case):
    label, text, seed, check = case
    sid = db.create_session(1)
    for item, n in seed.items():
        db.add_item_to_order(sid, item, n)
    agent = f.WaiterAgent(model=model)
    agent.table_no, agent.session_id, agent.state = 1, sid, f.State.TAKING_ORDER
    t0 = time.perf_counter()
    try:
        reply = agent._llm_turn(text)
    except Exception as exc:  # noqa: BLE001
        return {"label": label, "ok": False, "llm_ok": False, "secs": time.perf_counter() - t0, "reply": f"ERROR {exc!r}"}
    secs = time.perf_counter() - t0
    calls = [e for e in agent.log if e["event"] == "tool_call"]
    llm_tools = [e["tool"] for e in calls if e.get("source") == "llm"]
    used_fallback = any(e.get("source") == "rule_fallback" for e in calls)
    ok = bool(check(lines(agent), reply, llm_tools))
    return {"label": label, "ok": ok, "llm_ok": ok and not used_fallback, "secs": secs, "reply": reply,
            "fallback": used_fallback}


def bench(model, verbose):
    print(f"\n=== {model} " + "=" * (60 - len(model)))
    try:                                                   # load the model once so timings exclude loading
        f.ollama_chat([{"role": "user", "content": "hi"}], None, model)
    except Exception as exc:  # noqa: BLE001
        print(f"  cannot run: {exc}\n  (try: ollama pull {model})")
        return None
    results = [run_case(model, c) for c in CASES]
    for r in results:
        mark = "OK  " if r["llm_ok"] else ("SAFE" if r["ok"] else "FAIL")
        print(f"  {mark} {r['secs']:5.1f}s  {r['label']}")
        if verbose or not r["ok"]:
            print("       ->", r["reply"].replace("\n", " | ")[:150])
    secs = [r["secs"] for r in results]
    summary = {"model": model, "llm_ok": sum(r["llm_ok"] for r in results), "ok": sum(r["ok"] for r in results),
               "n": len(results), "median": statistics.median(secs), "worst": max(secs)}
    print(f"  LLM ok {summary['llm_ok']}/{summary['n']}   final ok {summary['ok']}/{summary['n']}   "
          f"median {summary['median']:.1f}s   worst {summary['worst']:.1f}s")
    return summary


def main():
    p = argparse.ArgumentParser(description="Benchmark Ollama models as the Perpetual Bistro waiter")
    p.add_argument("--models", nargs="+", help="models to test (default: every candidate already pulled)")
    p.add_argument("-v", "--verbose", action="store_true")
    args = p.parse_args()

    if not args.verbose:
        f.dbg = lambda *a, **k: None                        # silence the waiter's console debug prints
    db.DB_PATH = Path(tempfile.mkdtemp()) / "bench.db"      # throw-away database
    db.init_db()
    have = installed()
    models = args.models or [m for m in CANDIDATES if have is None or m in have or f"{m}:latest" in have]
    if not models:
        print("No candidate models found. Pull some first, e.g.  ollama pull qwen3:4b")
        return
    rows = [r for r in (bench(m, args.verbose) for m in models) if r]
    if rows:
        print("\n" + "=" * 64 + "\nRANKING  (LLM ok = model alone; then speed)")
        for r in sorted(rows, key=lambda r: (-r["llm_ok"], -r["ok"], r["median"])):
            print(f"  {r['model']:<16} LLM {r['llm_ok']:>2}/{r['n']}  final {r['ok']:>2}/{r['n']}  "
                  f"median {r['median']:5.1f}s  worst {r['worst']:5.1f}s")
        print("\nPick the first model whose median is under ~3 s (CPU) or ~1 s (GPU); then:  python app.py --model <name>")


if __name__ == "__main__":
    main()
