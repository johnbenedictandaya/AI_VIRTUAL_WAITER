"""app.py - main entry point: web dashboard for the Perpetual Bistro virtual waiter (Option 1).

    pip install flask ollama
    python app.py                           # opens http://localhost:5000
    python app.py --port 8000 --reset-db

Same FSM + SQLite tools as the CLI; this file only adds a thin HTTP layer.
Single-table demo: one agent/session at a time (use "New Table" to start over).
"""
import argparse
import os
import re
import threading
import webbrowser
from datetime import datetime, timezone
from pathlib import Path

from flask import Flask, abort, jsonify, render_template, request

import db_tools as db
from fsm_controller import DEFAULT_MODEL, State, WaiterAgent, normalize_speech, unavailable_tables
try:                                     # staff views: dining-room map and PIN-protected manager mode
    from ui_dashboard.floor_plan import register_floor
    from ui_dashboard.manager import register_manager
except ImportError as exc:               # files missing: the kiosk still works on its own
    print(f"[staff views disabled] {exc}")
    register_floor = register_manager = None
try:
    from speech_engine import VoiceService
except ImportError:                      # speech_engine.py missing: the app still works (typing, buttons, browser voice)
    VoiceService = None

HERE = Path(__file__).parent
UI_DIR = HERE / "ui_dashboard"            # templates/ (HTML) and static/ (JS, fonts, images)
IMAGES_DIR = UI_DIR / "static" / "images"
IMAGE_EXTS = (".jpg", ".jpeg", ".png", ".webp", ".gif", ".avif")
LOG_PATH = HERE / "sample_conversation_log.json"
app = Flask(__name__, template_folder=str(UI_DIR / "templates"), static_folder=str(UI_DIR / "static"),
            static_url_path="/static")
lock = threading.RLock()          # one LLM call / DB mutation at a time
config = {"model": DEFAULT_MODEL}
agent: WaiterAgent = None
voice: VoiceService = None        # offline Vosk STT + pyttsx3 TTS (created in __main__)
NO_VOICE = {"stt_ready": False, "tts_ready": False, "stt_error": "voice service not started", "mic_on": False,
            "speak_on": False, "status": "off", "heard": "", "heard_id": 0, "last_error": ""}


def speakable(text: str) -> str:
    """Turn a waiter reply into something pleasant to say out loud (no tables, no symbols)."""
    total = re.search(r"GRAND TOTAL\s+([\d.]+)", text)
    if "RECEIPT" in text:
        return "Your order is confirmed." + (f" Your total is {total.group(1)} pesos." if total else "") + " Enjoy your meal!"
    if text.startswith("Here's your order"):
        return ("Here is your order." + (f" Your total is {total.group(1)} pesos." if total else "")
                + " Say confirm to send it to the kitchen, or tell me what to change.")
    lines = [l.strip() for l in text.split("\n") if l.strip() and not l.startswith("Your tab:")]
    out = " ".join(l if l[-1] in ".?!" else l + "." for l in lines)
    out = re.sub(r"(\d+) x ", r"\1 ", out).replace("PHP", "pesos").replace(" - ", ", ")
    out = re.sub(r"\s*\(([^)]*)\)", r", \1", out)
    return out[:320]


def say(text: str):
    if voice:
        voice.say(speakable(text))


def voice_text(text: str):
    """Called from the voice thread for every recognised utterance."""
    with lock:
        reply = agent.handle(normalize_speech(text))
        finish_if_done()
    say(reply)


def photo_slug(name: str) -> str:
    """'Nugget Life Chose Me (6pc)' -> 'nugget-life-chose-me': the photo filename (without extension)."""
    return re.sub(r"[^a-z0-9]+", "-", re.sub(r"\s*\(.*?\)", "", name).lower()).strip("-")


def menu_with_photos():
    """Menu items plus the photo URL (if a matching file exists in ./images) and the expected filename."""
    IMAGES_DIR.mkdir(parents=True, exist_ok=True)
    found = {f.stem.lower(): f for f in IMAGES_DIR.iterdir() if f.suffix.lower() in IMAGE_EXTS}
    items = db.get_menu_by_category("all")["items"]
    for m in items:
        slug = photo_slug(m["name"])
        f = found.get(slug)
        m["photo"] = slug
        m["image"] = f"/static/images/{f.name}?v={int(f.stat().st_mtime)}" if f else None
    return items


def write_photo_guide():
    """images/PHOTO_GUIDE.txt always matches the current menu."""
    IMAGES_DIR.mkdir(parents=True, exist_ok=True)
    lines = ["PHOTO GUIDE - drop your menu photos into this folder.",
             "Name each file exactly like the list below (.jpg, .jpeg, .png, .webp, .gif or .avif all work).",
             "Landscape 4:3 photos (for example 800x600) look best. Refresh the page after adding a photo.", ""]
    cat = ""
    for m in db.get_menu_by_category("all")["items"]:
        if m["category"] != cat:
            cat = m["category"]
            lines += ["", f"[{cat}]"]
        lines.append(f"  {photo_slug(m['name'])}.jpg    <-  {m['name']}")
    (IMAGES_DIR / "PHOTO_GUIDE.txt").write_text("\n".join(lines) + "\n", encoding="utf-8")


def close_session_from_floor(session_id):
    """Staff cleared a table on the floor map: if the kiosk waiter was serving it, start a fresh session."""
    with lock:
        if agent and agent.session_id == session_id and not agent.finished:
            new_agent()


def new_agent():
    global agent
    if agent and agent.session_id and not agent.finished:
        db.set_session_status(agent.session_id, "CANCELLED")
    agent = WaiterAgent(model=config["model"])
    agent.start()


def taken_tables():
    """Tables a customer may not pick right now (the kiosk greys them out); only needed before a table is chosen."""
    return sorted(unavailable_tables(agent.session_id))


def payload(error=None):
    a = agent
    order = db.view_current_order(a.session_id) if a.session_id else {
        "lines": [], "subtotal_php": 0, "service_fee_php": 0, "grand_total_php": 0}
    roles = {"user": "user", "assistant": "waiter", "ui_action": "system"}
    transcript = [{"who": roles[e["event"]], "text": e["text"]} for e in list(a.log) if e["event"] in roles]
    receipt = next((e["text"] for e in reversed(a.log) if e["event"] == "assistant"), "") \
        if a.state == State.CONFIRMED else None
    return {"state": a.state.value, "table": a.table_no, "session": a.session_id, "transcript": transcript,
            "order": order, "receipt": receipt, "error": error,
            "menu": menu_with_photos(), "discounts": db.list_discounts(),
            "taken": taken_tables() if a.state == State.GREETING else [],
            "voice": voice.info() if voice else NO_VOICE}


def finish_if_done():
    if agent.finished:
        agent.save_log(LOG_PATH)       # auto-save sample_conversation_log.json
        if voice:
            voice.set_mic(False)


@app.get("/")
def index():
    return render_template("dashboard.html")


@app.get("/receipt/<int:sid>")
def receipt_page(sid):
    """Printable receipt (80 mm slip) for a confirmed order. ?print=1 opens the browser's print dialog by itself."""
    with db.db() as c:
        row = c.execute("SELECT table_no, status, created_at FROM table_sessions WHERE session_id=?", (sid,)).fetchone()
    if not row or row["status"] != "CONFIRMED":
        abort(404)
    order = db.view_current_order(sid)
    started = datetime.strptime(row["created_at"][:19], "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc).astimezone()
    return render_template("receipt.html", sid=sid, table=row["table_no"], order=order, disc=order["discount"],
                           when=started.strftime("%Y-%m-%d %H:%M"), printed=datetime.now().strftime("%Y-%m-%d %H:%M"),
                           auto=request.args.get("print") == "1")


@app.get("/api/state")
def api_state():
    return jsonify(payload())          # read-only poll: no lock, so it never waits behind a slow LLM turn


@app.post("/api/chat")
def api_chat():
    text = (request.get_json(silent=True) or {}).get("text", "").strip()
    with lock:
        reply = None
        if text:
            reply = agent.handle(text)
            finish_if_done()
        out = payload()
    if reply:
        say(reply)
    return jsonify(out)


@app.post("/api/cart")
def api_cart():
    """Manual +/- override: {item, delta, notes}."""
    d = request.get_json(silent=True) or {}
    item, delta, notes = d.get("item", ""), int(d.get("delta", 0)), d.get("notes", "")
    with lock:
        if agent.state not in (State.TAKING_ORDER, State.REVIEW):
            return jsonify(payload("Tell the waiter your table number first."))
        if delta > 0:
            res = db.add_item_to_order(agent.session_id, item, delta, notes)
        else:
            res = db.remove_item_from_order(agent.session_id, item, abs(delta), notes)
        if res["ok"]:
            agent._log("ui_action", text=f"Cart {'+' if delta > 0 else '-'}{abs(delta)} {item}"
                                         + (f" [{notes}]" if notes else ""))
            if agent.state == State.REVIEW:
                agent._go(State.TAKING_ORDER)
        return jsonify(payload(None if res["ok"] else res["error"]))


@app.post("/api/discount")
def api_discount():
    """{code: "SENIOR20"} applies a discount; {remove: true} removes it. Same validation as the waiter's."""
    d = request.get_json(silent=True) or {}
    with lock:
        if agent.state not in (State.TAKING_ORDER, State.REVIEW):
            return jsonify(payload("Tell the waiter your table number first."))
        if d.get("remove"):
            res = db.remove_discount(agent.session_id)
            if res["ok"]:
                agent._log("ui_action", text="Removed discount")
                if agent.state == State.REVIEW:
                    agent._go(State.TAKING_ORDER)
            return jsonify(payload())            # removing when none is applied is harmless
        res = db.apply_discount(agent.session_id, d.get("code", ""))
        if res["ok"]:
            agent._log("ui_action", text=f"Applied discount {res['code']}")
            if agent.state == State.REVIEW:
                agent._go(State.TAKING_ORDER)
        return jsonify(payload(None if res["ok"] else res["error"]))


@app.get("/api/discounts")
def api_discounts():
    return jsonify({"discounts": db.list_discounts()})


@app.post("/api/clear")
def api_clear():
    with lock:
        if agent.session_id and agent.state in (State.TAKING_ORDER, State.REVIEW):
            for l in db.view_current_order(agent.session_id)["lines"]:
                db.remove_item_from_order(agent.session_id, l["item"], None, l["notes"])
            agent._log("ui_action", text="Cleared cart")
            if agent.state == State.REVIEW:
                agent._go(State.TAKING_ORDER)
        return jsonify(payload())


@app.post("/api/confirm")
def api_confirm():
    with lock:
        msg = agent.confirm_from_ui()
        finish_if_done()
        out = payload(None if agent.state == State.CONFIRMED else msg)
    say(msg)
    return jsonify(out)


@app.post("/api/reset")
def api_reset():
    with lock:
        new_agent()
        out = payload()
    say(agent.log[-1]["text"])
    return jsonify(out)


@app.post("/api/voice")
def api_voice():
    d = request.get_json(silent=True) or {}
    if voice:
        if "speak" in d:
            voice.set_speak(bool(d["speak"]))
        if "mic" in d:
            voice.set_mic(bool(d["mic"]))
    return jsonify(voice.info() if voice else NO_VOICE)


if register_floor:
    register_floor(app, on_close_session=close_session_from_floor)
    register_manager(app)


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--port", type=int, default=5000)
    p.add_argument("--model", default=DEFAULT_MODEL, help="Ollama model (default: qwen3:4b; try qwen3:8b or gemma4:e4b)")
    p.add_argument("--stt-model", default=None, help="Whisper model for speech-to-text (default: auto by hardware)")
    p.add_argument("--reset-db", action="store_true")
    p.add_argument("--no-warmup", action="store_true", help="do not pre-load the Ollama model at start-up")
    p.add_argument("--vosk-model", default=None, help="path to the Vosk model folder (default: ./model)")
    args = p.parse_args()
    config["model"] = args.model
    if args.stt_model:
        os.environ["BISTRO_WHISPER"] = args.stt_model
    db.init_db(reset=args.reset_db)
    write_photo_guide()
    new_agent()
    if not args.no_warmup:             # load the model into memory now so the first customer is not the one waiting
        def _warm():
            try:
                import ollama
                from fsm_controller import ollama_chat
                ollama_chat([{"role": "user", "content": "hi"}], None, args.model)
                print(f"[warm-up] {args.model} is loaded")
            except Exception as exc:  # noqa: BLE001
                print(f"[warm-up] skipped: {exc}")
        threading.Thread(target=_warm, daemon=True).start()
    if VoiceService:
        voice = VoiceService(on_text=voice_text, model_path=args.vosk_model)
        print("Offline voice - speech-to-text:", f"READY ({voice.engine})" if voice.stt_ready else f"unavailable ({voice.stt_error})",
              "| text-to-speech:", "READY" if voice.tts.ready else "unavailable (pip install pyttsx3)")
    else:
        print("Offline voice disabled: speech_engine.py was not found next to app.py (typing still works).")
    url = f"http://localhost:{args.port}"
    threading.Timer(1.0, lambda: webbrowser.open(url)).start()
    print(f"Perpetual Bistro dashboard running at {url}  (Ctrl+C to stop)")
    app.run(port=args.port, threaded=True)
