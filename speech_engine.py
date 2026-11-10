"""speech_engine.py - offline voice for the Perpetual Bistro waiter.

STT engine order: faster-whisper (best accuracy, primed with the menu names) -> Vosk (fallback).
  * BISTRO_STT=vosk          force Vosk            * BISTRO_WHISPER=<model>    force a Whisper model
  * default model: large-v3-turbo on an NVIDIA GPU, small.en (int8) on CPU, base.en if that cannot load
  * passing --vosk-model     also forces Vosk      * first run downloads the Whisper model once, then it is offline

Adapted from the Jarvis voice_pipeline.py:
  * STT: Vosk (via speech_recognition for mic capture), loaded once at startup.
  * TTS: pyttsx3 on ONE dedicated worker thread, with a fresh engine per utterance
    (the SAPI5 workaround from the Jarvis project).
Differences from Jarvis: no wake word (the Mic toggle is the on/off switch), a CLOSED WAITER VOCABULARY
(waiter_vocab.py: menu names + nicknames + waiter words, a few hundred words, not a tiny grammar) enforced three ways:
Vosk grammar, a word filter on whatever Whisper/Vosk heard, and an off-topic reject; and the mic is ignored while
the waiter is speaking so it never hears itself.

Needs:  pip install vosk SpeechRecognition PyAudio pyttsx3
Model:  copy your Vosk "model" folder next to app.py (or set VOSK_MODEL / --vosk-model).
"""
import json
import logging
import os
import queue
import re
import threading
import time
from pathlib import Path
from typing import Callable, Optional

try:
    import pyttsx3
except ImportError:  # pragma: no cover
    pyttsx3 = None
try:
    import speech_recognition as sr
except ImportError:  # pragma: no cover
    sr = None
try:
    import vosk
except ImportError:  # pragma: no cover
    vosk = None
try:
    import waiter_vocab as wv          # closed waiter vocabulary (menu words + waiter words)
except Exception:  # pragma: no cover
    wv = None
try:
    from faster_whisper import WhisperModel
except ImportError:  # pragma: no cover
    WhisperModel = None

log = logging.getLogger("bistro.voice")
HERE = Path(__file__).parent
NOISE_WORDS = {"the", "a", "uh", "um", "huh", "hmm", "oh", "eh", "ah"}
MIN_KEEP = 0.5          # if fewer than half the heard words are restaurant words, treat it as off-topic
HALLUCINATIONS = {"you", "thank you", "thanks", "thanks for watching", "bye", "so", "okay", "the end"}   # Whisper on silence


def _valid_model(p: Path) -> Optional[Path]:
    """A Vosk model folder contains 'am' and 'conf'. Also accept one level of nesting."""
    if (p / "am").is_dir() and (p / "conf").is_dir():
        return p
    for sub in (p.iterdir() if p.is_dir() else []):
        if sub.is_dir() and (sub / "am").is_dir() and (sub / "conf").is_dir():
            return sub
    return None


def find_vosk_model(explicit: Optional[str] = None) -> Optional[Path]:
    cands = [Path(explicit)] if explicit else []
    if os.environ.get("VOSK_MODEL"):
        cands.append(Path(os.environ["VOSK_MODEL"]))
    cands.append(HERE / "model")
    cands += sorted(HERE.glob("vosk-model*"))
    for c in cands:
        if c.is_dir() and _valid_model(c):
            return _valid_model(c)
    return None


# ---------------------------------------------------------------- TTS
# Female voices that ship with Windows (SAPI5), macOS and common Linux/espeak setups, best first.
FEMALE_HINTS = ["zira", "aria", "jenny", "hazel", "susan", "heather", "linda", "eva", "catherine", "helena", "sabina",
                "samantha", "ava", "allison", "victoria", "karen", "moira", "tessa", "fiona", "serena", "libby", "sonia",
                "female", "+f1", "+f2", "+f3", "+f4", "+f5"]
MALE_HINTS = ["david", "mark", "george", "james", "richard", "ravi", "guy", "daniel", "alex", "fred", "male", "+m"]


def pick_female_voice(voices, forced: str = ""):
    """Voice id of the best English female voice in pyttsx3's voice list, or None (keep the system default).
    BISTRO_VOICE=<part of a voice name> overrides the choice (e.g. BISTRO_VOICE=hazel)."""
    def label(v):
        return f"{getattr(v, 'name', '')} {getattr(v, 'id', '')}".lower()

    def gender(v):
        g = getattr(v, "gender", None)
        return str(g).lower() if g else ""

    if forced:
        for v in voices:
            if forced.lower() in label(v):
                return v.id
    best, best_rank = None, 10 ** 6
    for v in voices:
        text, g = label(v), gender(v)
        if "male" in g and "female" not in g:
            continue
        if any(m in text for m in MALE_HINTS) and "female" not in text and "female" not in g:
            continue
        rank = next((i for i, h in enumerate(FEMALE_HINTS) if h in text), None)
        if rank is None and "female" in g:
            rank = len(FEMALE_HINTS)
        if rank is None:
            continue
        english = any(t in text for t in ("en-", "en_", "english", "enus", "engb", "(en", "en+")) or not any(
            t in text for t in ("de-", "fr-", "es-", "it-", "ja-", "zh-", "ru-", "pt-", "ko-"))
        rank += 0 if english else 100
        if rank < best_rank:
            best, best_rank = v.id, rank
    return best


class TextToSpeech:
    """Queue + single worker thread that owns pyttsx3 (thread-safe speak())."""

    def __init__(self, rate: int = 175, volume: float = 1.0):
        self.rate, self.volume = rate, volume
        self.ready = pyttsx3 is not None
        self._q: "queue.Queue[str]" = queue.Queue()
        self._pending = 0
        self._lock = threading.Lock()
        self.voice_id, self.voice_name, self._voice_checked = None, "", False
        if self.ready:
            threading.Thread(target=self._run, daemon=True).start()

    @property
    def busy(self) -> bool:
        return self._pending > 0

    def speak(self, text: str):
        if not self.ready or not text.strip():
            return
        with self._lock:
            self._pending += 1
        self._q.put(text)

    def clear(self):
        while True:
            try:
                self._q.get_nowait()
            except queue.Empty:
                return
            with self._lock:
                self._pending -= 1

    def _use_female_voice(self, engine):
        """The waiter speaks with a female voice. The choice is made once, then reused for every utterance."""
        try:
            if not self._voice_checked:
                self._voice_checked = True
                voices = engine.getProperty("voices") or []
                self.voice_id = pick_female_voice(voices, os.environ.get("BISTRO_VOICE", ""))
                self.voice_name = next((v.name for v in voices if v.id == self.voice_id), "")
                if self.voice_id:
                    log.info("TTS voice: %s", self.voice_name or self.voice_id)
                else:
                    log.warning("No female voice found among %d installed voices: using the system default. "
                                "Install one in Windows Settings > Time & language > Speech, "
                                "or set BISTRO_VOICE=<voice name>.", len(voices))
            if self.voice_id:
                engine.setProperty("voice", self.voice_id)
        except Exception as exc:  # noqa: BLE001 - a voice problem must never silence the waiter
            log.warning("could not select a voice: %s", exc)

    def _run(self):
        try:                                   # SAPI5 needs COM initialised on non-main threads (Windows)
            import pythoncom
            pythoncom.CoInitialize()
        except ImportError:
            pass
        while True:
            text = self._q.get()
            try:
                log.info("TTS: %s", text)
                engine = pyttsx3.init()        # fresh engine per utterance: SAPI5 goes silent otherwise
                self._use_female_voice(engine)
                engine.setProperty("rate", self.rate)
                engine.setProperty("volume", self.volume)
                engine.say(text)
                engine.runAndWait()
                engine.stop()
                del engine
            except Exception as exc:  # noqa: BLE001
                log.error("TTS failed: %s", exc)
            finally:
                with self._lock:
                    self._pending -= 1


# ---------------------------------------------------------------- STT + service
class VoiceService:
    def __init__(self, on_text: Callable[[str], None], model_path: Optional[str] = None):
        self.on_text = on_text
        self.tts = TextToSpeech()
        self.mic_on = False
        self.speak_on = False
        self.heard, self.heard_id = "", 0
        self.last_error = ""
        self._status = "off"
        self._thread: Optional[threading.Thread] = None
        self.model = None                 # Vosk
        self.whisper = None               # faster-whisper
        self.engine = "none"
        self.menu_prompt = ""
        self.beam = 1
        self.off_topic = ""               # last utterance rejected as not restaurant talk
        self.last_dropped = []            # words removed from the last accepted utterance
        self.stt_error = ""
        self.recognizer = None

        if sr is None:
            self.stt_error = "missing packages - run: pip install SpeechRecognition PyAudio faster-whisper"
        else:
            if not model_path and os.environ.get("BISTRO_STT", "").lower() != "vosk":
                self._load_whisper()
            if self.whisper is None:
                self._load_vosk(model_path)
            if self.whisper is not None or self.model is not None:
                r = self.recognizer = sr.Recognizer()
                r.dynamic_energy_threshold = True
                r.pause_threshold = 0.6       # was 0.9: the reply starts 0.3 s sooner
                r.non_speaking_duration = 0.35
                r.phrase_threshold = 0.3
                log.info("STT engine: %s", self.engine)

    @staticmethod
    def whisper_candidates():
        """[(model, device, compute_type)] best first. GPU -> large-v3-turbo; CPU -> small.en (int8).
        Override the model with BISTRO_WHISPER (e.g. distil-small.en, base.en, medium.en, large-v3-turbo)."""
        gpu = False
        try:
            import ctranslate2
            gpu = ctranslate2.get_cuda_device_count() > 0
        except Exception:  # noqa: BLE001
            pass
        forced = os.environ.get("BISTRO_WHISPER")
        out = []
        if gpu:
            out.append((forced or "large-v3-turbo", "cuda", "float16"))
        out.append((forced or "small.en", "cpu", "int8"))
        if not forced or forced != "base.en":
            out.append(("base.en", "cpu", "int8"))                 # last resort on a weak machine
        return out

    def _load_whisper(self):
        if WhisperModel is None:
            self.stt_error = "faster-whisper not installed (pip install faster-whisper numpy)"
            return
        try:
            import numpy as np
            import db_tools
            names = [re.sub(r"\s*\(.*?\)", "", i["name"]) for i in db_tools.menu_names_and_aliases()]
            self.menu_prompt = "Restaurant order. Menu: " + ", ".join(names) + "."
        except Exception as exc:  # noqa: BLE001
            self.stt_error = f"could not prepare Whisper: {exc}"
            return
        errors = []
        for size, device, ctype in self.whisper_candidates():
            try:
                model = WhisperModel(size, device=device, compute_type=ctype, cpu_threads=os.cpu_count() or 4)
                segs, _ = model.transcribe(np.zeros(16000, dtype=np.float32), language="en", beam_size=1)
                list(segs)                       # transcribe() is lazy: consume it so the warm-up really runs
                self.whisper, self.engine = model, f"whisper:{size}/{device}"
                self.beam = 5 if device == "cuda" else 1          # GPU has room for a wider (more accurate) search
                self.stt_error = ""
                return
            except Exception as exc:  # noqa: BLE001
                errors.append(f"{size}/{device}: {exc}")
                log.warning("Whisper %s on %s failed: %s", size, device, exc)
        self.stt_error = "could not load Whisper (" + "; ".join(errors) + ")"

    def _load_vosk(self, model_path):
        if vosk is None:
            self.stt_error = (self.stt_error + "; " if self.stt_error else "") + "vosk not installed"
            return
        path = find_vosk_model(model_path)
        if path is None:
            self.stt_error = (self.stt_error + "; " if self.stt_error else "") + \
                "Vosk model folder not found - copy your 'model' folder next to app.py"
            return
        try:
            vosk.SetLogLevel(-1)
            self.model = vosk.Model(str(path))
            self.engine = "vosk"
            self.stt_error = ""
            log.info("Vosk model loaded from %s", path)
        except Exception as exc:  # noqa: BLE001
            self.stt_error = f"could not load Vosk model: {exc}"

    @property
    def stt_ready(self) -> bool:
        return (self.whisper is not None or self.model is not None) and self.recognizer is not None

    # ------------------------------------------------------------ public controls
    def info(self) -> dict:
        err, self.last_error = self.last_error, ""            # report a runtime error once
        status = "speaking" if self.tts.busy else self._status
        return {"stt_ready": self.stt_ready, "tts_ready": self.tts.ready, "stt_error": self.stt_error,
                "engine": self.engine, "voice": self.tts.voice_name, "mic_on": self.mic_on, "speak_on": self.speak_on, "status": status,
                "heard": self.heard, "heard_id": self.heard_id, "last_error": err}

    def set_speak(self, on: bool):
        self.speak_on = bool(on) and self.tts.ready
        if self.speak_on:
            self.tts.speak("Voice replies are on.")
        else:
            self.tts.clear()

    def say(self, text: str):
        if self.speak_on:
            self.tts.speak(text)

    def set_mic(self, on: bool):
        if not on:
            self.mic_on = False
            self._status = "off"
            return
        if not self.stt_ready:
            return
        self.mic_on, self.last_error = True, ""
        if self._thread is None or not self._thread.is_alive():
            self._thread = threading.Thread(target=self._listen_loop, daemon=True)
            self._thread.start()

    # ------------------------------------------------------------ internals
    def _restrict(self, text: str) -> str:
        """Closed vocabulary: drop non-restaurant words, reject the utterance if little of it is restaurant talk."""
        if wv is None or not text:
            return text
        try:
            clean, dropped = wv.filter_text(text)
        except Exception as exc:  # noqa: BLE001
            log.warning("vocabulary filter failed: %s", exc)
            return text
        kept = len(clean.split())
        if not kept or kept / (kept + len(dropped)) < MIN_KEEP or not wv.in_domain(clean):
            self.off_topic = text
            log.info("Rejected as off-topic: %r", text)
            return ""
        self.last_dropped = dropped
        if dropped:
            log.info("Dropped non-restaurant words %s from %r", dropped, text)
        return clean

    def _transcribe_whisper(self, audio) -> str:
        try:
            import numpy as np
            raw = audio.get_raw_data(convert_rate=16000, convert_width=2)
            pcm = np.frombuffer(raw, np.int16).astype(np.float32) / 32768.0
            segs, _ = self.whisper.transcribe(pcm, language="en", beam_size=self.beam, vad_filter=True,
                                              initial_prompt=self.menu_prompt, condition_on_previous_text=False,
                                              no_speech_threshold=0.6)
            text = re.sub(r"[^\w\s'-]", "", " ".join(s.text for s in segs).lower()).strip()
        except Exception as exc:  # noqa: BLE001
            log.warning("Whisper transcription failed: %s", exc)
            return ""
        if not text or text in HALLUCINATIONS or (len(text.split()) == 1 and text in NOISE_WORDS):
            return ""
        return self._restrict(text)

    def _transcribe(self, audio) -> str:
        if self.whisper is not None:
            return self._transcribe_whisper(audio)
        try:
            raw = audio.get_raw_data(convert_rate=16000, convert_width=2)
            rec = None
            if wv is not None:                                  # restricted to the waiter vocabulary
                try:
                    rec = vosk.KaldiRecognizer(self.model, 16000, wv.grammar_json())
                except Exception as exc:  # noqa: BLE001        # big static models cannot take a grammar
                    log.warning("Vosk grammar not supported by this model (%s): using open vocabulary + filter", exc)
            rec = rec or vosk.KaldiRecognizer(self.model, 16000)
            rec.AcceptWaveform(raw)
            text = json.loads(rec.FinalResult()).get("text", "")
        except Exception as exc:  # noqa: BLE001
            log.warning("Vosk transcription failed: %s", exc)
            return ""
        words = [w for w in text.split() if w != "[unk]"]
        if not words or (len(words) == 1 and (words[0] in NOISE_WORDS or len(words[0]) < 2)):
            return ""
        return self._restrict(" ".join(words))

    def _listen_loop(self):
        try:
            with sr.Microphone() as source:
                self._status = "calibrating"
                self.recognizer.adjust_for_ambient_noise(source, duration=1.0)
                while self.mic_on:
                    while self.tts.busy and self.mic_on:       # never listen while the waiter talks
                        self._status = "speaking"
                        time.sleep(0.1)
                    self._status = "listening"
                    spoke_before = self.tts.busy
                    try:
                        audio = self.recognizer.listen(source, timeout=4, phrase_time_limit=10)
                    except sr.WaitTimeoutError:
                        continue
                    if not self.mic_on:
                        break
                    if spoke_before or self.tts.busy:          # captured the waiter's own voice: discard
                        continue
                    self._status = "transcribing"
                    self.off_topic, self.last_dropped = "", []
                    text = self._transcribe(audio)
                    log.info("Heard: %r", text)
                    if not text:
                        if len(self.off_topic.split()) >= 2:       # real speech, but not restaurant talk
                            self.last_error = "I only understand restaurant requests. Please say your order."
                            self.say("Sorry, I can only help with our menu and your order.")
                        continue
                    note = f" (ignored: {', '.join(self.last_dropped)})" if self.last_dropped else ""
                    self.heard, self.heard_id = text + note, self.heard_id + 1       # shown on screen so nothing is silently lost
                    self._status = "thinking"
                    try:
                        self.on_text(text)
                    except Exception:  # noqa: BLE001
                        log.exception("on_text handler failed")
        except Exception as exc:  # noqa: BLE001
            self.last_error = f"Microphone problem: {exc}"
            log.error(self.last_error)
        finally:
            self.mic_on = False
            self._status = "off"
