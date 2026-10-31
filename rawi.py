"""Rawi — local speech to text, pasted into whatever box has focus.

Hold the key, speak, release. Everything runs on this machine: mic -> GPU whisper -> clipboard -> Ctrl+V.

Usage:
  pythonw rawi.py            run in the background (no console)
  python rawi.py             run with console (see what it hears)
  python rawi.py --test f.wav   transcribe a file, print, do not paste
"""
from __future__ import annotations

import argparse
import collections
import datetime
import difflib
import re
import json
import os
import subprocess
import sys
import queue
import threading
import time

import numpy as np

try:
    from wave_overlay import Wave
except Exception:  # wave overlay is optional
    Wave = None

for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass


def _register_cuda_dlls() -> None:
    """cuBLAS/cuDNN 12 ship as pip packages (nvidia-*-cu12); put their bin dirs on the
    DLL search path before ctranslate2 loads, or it dies with 'cublas64_12.dll not found'."""
    import glob
    import site
    dirs = []
    for sp in list(site.getsitepackages()) + [site.getusersitepackages()]:
        for pat in ("nvidia/*/bin", "nvidia/*/lib"):
            dirs += glob.glob(os.path.join(sp, pat))
    for dp in dirs:
        try:
            os.add_dll_directory(dp)
        except Exception:
            pass
        os.environ["PATH"] = dp + os.pathsep + os.environ.get("PATH", "")


_register_cuda_dlls()

ROOT = os.path.dirname(os.path.abspath(__file__))
PIDFILE = os.path.join(ROOT, "daemon.pid")

# pythonw has no console: give every print somewhere to go, so the daemon stops being blind
if sys.stdout is None or sys.stderr is None:
    _logf = open(os.path.join(ROOT, "rawi.log"), "a", encoding="utf-8", buffering=1)
    if sys.stdout is None:
        sys.stdout = _logf
    if sys.stderr is None:
        sys.stderr = _logf
CREATE_NO_WINDOW = 0x08000000

# ---------- platform ----------
IS_WIN = sys.platform.startswith("win")
IS_MAC = sys.platform == "darwin"
NO_WINDOW = CREATE_NO_WINDOW if IS_WIN else 0     # subprocess flag; 0 everywhere else
PASTE_MOD = "command" if IS_MAC else "ctrl"      # the paste chord's modifier
MAC_LEFT = {"ctrl": "ctrl_l", "alt": "alt_l", "shift": "shift_l", "cmd": "cmd_l"}  # pynput aliases on macOS

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import rawi_mood  # one emoji per dictation, earned by words


def pid_alive(pid: int) -> bool:
    if not IS_WIN:
        try:
            os.kill(int(pid), 0)
            return True
        except PermissionError:
            return True                      # alive, owned by someone else
        except Exception:
            return False
    try:
        import ctypes
        k = ctypes.windll.kernel32
        h = k.OpenProcess(0x1000, False, int(pid))
        if not h:
            return False
        code = ctypes.c_ulong(0)
        k.GetExitCodeProcess(h, ctypes.byref(code))
        k.CloseHandle(h)
        return code.value == 259
    except Exception:
        return False
CFG_PATH = os.path.join(ROOT, "config.json")
LOG_PATH = os.path.join(ROOT, "transcripts.jsonl")
SR = 16000

QUEUE_MAX = 8          # recordings held while one is still transcribing

DEFAULTS = {
    "hold_key": "ctrl_l",
    "mode": "hold",
    "model": "large-v3-turbo",
    "compute_type": "float16",
    "device": "auto",       # auto = cuda when a CUDA device is present, else cpu int8
    "language": "auto",
    "paste": True,
    "restore_clipboard": True,
    "beeps": True,
    "log": False,
    "max_seconds": 180,
    "dumb": False,
    "mood": "auto",      # auto = tag each dictation with one earned emoji; off = never
    "talk": False,       # voice mode: speak the transcript aloud instead of pasting it
    "paste_key": "f9", "lang_key": "f10", "talk_key": "f7", "lang_lock": "auto", "listen": {"key": "f8", "silence_ms": 700, "min_s": 0.45, "max_s": 25, "paste": False},
    "min_seconds": 0.25,
    "dictionary": {
        "names": [],
        "pairs": [],
    },
    # per-app profiles: whichever list matches the focused window's title first wins.
    # This is the one thing Wispr's model gets structurally right (a "voice" bound to apps);
    # ours keys on the window title we actually read, not a fixed list of product names.
    "profiles": [
        {"name": "Code", "apps": ["Visual Studio Code", "Cursor", "PowerShell", "Command Prompt",
                                  "Windows Terminal", "PyCharm"], "level": "none", "tone": "default"},
        {"name": "Chat", "apps": ["Slack", "Teams", "WhatsApp", "Telegram", "Discord"],
         "level": "light", "tone": "casual"},
        {"name": "Mail", "apps": ["Outlook", "Gmail", "Mail", "Notion", "Google Docs"],
         "level": "light", "tone": "formal"},
    ],
    # spoken trigger -> text. An exact match on the whole utterance expands.
    "snippets": {},
    "clean": {
        "enabled": False,
        "level": "light",
        "tone": "default",
        "endpoint": "http://127.0.0.1:18080/v1/chat/completions",
        "max_tokens": 160,
        "timeout": 25,
        "system": ("Rewrite the dictated text below. Remove filler sounds (ah, uh, um, hmm, er; "
                   "Arabic: اه، امم، همم، يعني) when they carry no meaning. Add punctuation and "
                   "capitalisation; Arabic uses ، and ؟. Keep the speaker's language and dialect "
                   "exactly as spoken and never translate; if the sentence mixes Arabic and English, "
                   "keep the mix. Never add information, never answer questions, never summarise. "
                   "Leave code, commands, file paths, names and URLs exactly as written. If the text "
                   "is already clean, return it unchanged. Output only the rewritten text."),
    },
}


def load_cfg() -> dict:
    cfg = dict(DEFAULTS)
    if os.path.exists(CFG_PATH):
        try:
            cfg.update(json.load(open(CFG_PATH, encoding="utf-8")))
        except Exception as e:
            print(f"[rawi] config unreadable ({e}); using defaults")
    else:
        json.dump(DEFAULTS, open(CFG_PATH, "w", encoding="utf-8"), indent=2)
    return cfg


def save_cfg(cfg: dict) -> None:
    try:
        json.dump(cfg, open(CFG_PATH, "w", encoding="utf-8"), indent=2, ensure_ascii=False)
    except Exception as e:
        print(f"[rawi] could not save config ({e})", flush=True)


def beep(freq: int, ms: int = 55, on: bool = True) -> None:
    """A short tone, best-effort, on any platform: a failed beep must never break dictation."""
    if not on:
        return
    try:
        if IS_WIN:
            import winsound
            winsound.Beep(freq, ms)
        elif IS_MAC:
            subprocess.Popen(["afplay", "/System/Library/Sounds/Tink.aiff"],
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        else:
            sys.stdout.write("\a")
            sys.stdout.flush()
    except Exception:
        pass


VOICE_PORT = int(os.environ.get("RAWI_VOICE_PORT", "18097"))
VOICE_CFG = os.path.join(ROOT, "voice.json")


def voice_cfg() -> dict:
    """Optional: a local TTS server (voice_out.py) that can speak a line instead of pasting it."""
    try:
        with open(VOICE_CFG, encoding="utf-8") as f:
            return json.load(f) or {}
    except Exception:
        return {}


def ensure_mouth():
    """Keep the local voice server warm: it loads the model once, then speaks in a fraction of a second."""
    if not bool(voice_cfg().get("autostart")):
        return False
    try:
        import urllib.request
        with urllib.request.urlopen(f"http://127.0.0.1:{VOICE_PORT}/health", timeout=1.5) as r:
            h = json.loads(r.read().decode())
        print(f"[rawi] voice ready={h.get('ready')}", flush=True)
        return True
    except Exception:
        pass
    try:
        exe = os.path.join(os.path.dirname(sys.executable), "pythonw.exe")
        if not os.path.exists(exe):
            exe = sys.executable
        p = subprocess.Popen([exe, os.path.join(ROOT, "launch_voice.py")],
                             creationflags=NO_WINDOW)
        print(f"[rawi] voice server cold-start -> pid {p.pid}", flush=True)
        return False
    except Exception as e:
        print(f"[rawi] voice server failed to start: {type(e).__name__}: {e}", flush=True)
        return False


def _voice_post(path: str, obj: dict, timeout: float = 1.5) -> bool:
    """Post to the local voice server. Never raises: speech must not break dictation."""
    try:
        import urllib.request
        data = json.dumps(obj).encode("utf-8")
        req = urllib.request.Request(f"http://127.0.0.1:{VOICE_PORT}" + path, data=data,
                                     headers={"content-type": "application/json"})
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return 200 <= r.status < 300
    except Exception as e:
        print(f"[rawi] voice server unreachable ({type(e).__name__})", flush=True)
        return False


def active_app() -> str:
    """Title (or app name) of what will receive the paste. Empty when it cannot be read."""
    try:
        if IS_MAC:
            r = subprocess.run(["osascript", "-e",
                                'tell application "System Events" to get name of first '
                                'application process whose frontmost is true'],
                               capture_output=True, text=True, timeout=3)
            return (r.stdout or "").strip()[:120]
        if IS_WIN:
            import pygetwindow as gw
            w = gw.getActiveWindow()
            return (w.title or "").strip()[:120] if w else ""
        r = subprocess.run(["xdotool", "getactivewindow", "getwindowname"],
                           capture_output=True, text=True, timeout=3)
        return (r.stdout or "").strip()[:120]
    except Exception:
        return ""


# ---------- fidelity: did the cleaner keep his meaning? ----------
_WORD_RE = re.compile(r"[\w\u0600-\u06FF']+")
_FILLERS = {"um", "uh", "erm", "hmm", "mmm", "like", "yeah", "يعني", "امم", "اه", "اها"}


def _toks(t: str) -> list[str]:
    return [w for w in _WORD_RE.findall(_norm(t)) if w not in _FILLERS]


def fidelity(raw: str, clean: str) -> dict:
    """Word-level divergence between what he said and what the cleaner wrote.

    Fillers and punctuation are dropped first, so a good cleanup of a messy sentence
    scores green rather than amber — the meter must not punish the cleaner for doing
    its job, only for doing someone else's.

    d     = 1 - ratio over content words
    ins   = content words the cleaner added that he never said (an answer instead of a
            transcript shows here long before it shows as divergence)
    lvl   = same / green / amber / red
    """
    try:
        a, b = _toks(raw), _toks(clean)
        if not a or not b:
            return {"d": 0.0, "ins": 0, "lvl": "same"}
        sm = difflib.SequenceMatcher(None, a, b, autojunk=False)
        d = 1.0 - sm.ratio()
        seen = set(a)
        ins = 0
        for tag, i1, i2, j1, j2 in sm.get_opcodes():
            if tag in ("insert", "replace"):
                ins += sum(1 for w in b[j1:j2] if w not in seen)
        if d < 0.02 and ins == 0:
            lvl = "same"
        elif d <= 0.20:
            lvl = "green"
        elif d <= 0.45:
            lvl = "amber"
        else:
            lvl = "red"
        return {"d": round(d, 3), "ins": ins, "lvl": lvl}
    except Exception:
        return {"d": None, "ins": None, "lvl": "unknown"}



# ---------- the ear: always-on listening ----------
class Ear:
    """A latch, not a hold. One key down and the microphone stays open; voice-activity
    decides where a sentence begins and ends, and each finished sentence is transcribed,
    cleaned and put on the clipboard. Nothing is pasted into a window he did not choose."""

    def __init__(self, d):
        self.d = d
        self.on = False
        self.q = queue.Queue()
        self.buf = []
        self.voiced = 0.0
        self.silent = 0.0
        self.total = 0.0
        self.talking = False
        self.floor = 0.0012
        self.lvl = 0.0
        self.peak = 0.0
        self.dropped = 0
        self.emits = 0
        self.short = 0
        self.done = 0
        self.empty = 0
        self.errors = 0
        self.hot = 0.0
        self.pre = []
        self.qwin = []
        self.PRE = 14
        threading.Thread(target=self._loop, daemon=True).start()

    def cfg(self):
        c = self.d.cfg.get("listen") or {}
        return {"silence": float(c.get("silence_ms", 700)) / 1000.0,
                "min": float(c.get("min_s", 0.45)),
                "max": float(c.get("max_s", 25.0))}

    def feed(self, block):
        """Called from the audio callback: light numpy only, never transcription.
        Two thresholds, not one: a sentence starts when you clearly speak and ends when
        you are clearly quiet, so room noise cannot hold the ear open forever."""
        try:
            c = self.cfg()
            a = block.reshape(-1)
            if not a.size:
                return
            rms = float(np.sqrt(np.mean(np.square(a))))
            self.lvl = rms
            self.peak = max(self.peak * 0.995, rms)
            dtb = a.size / float(SR)
            if not self.talking:
                self.pre.append(a.copy())
                del self.pre[:-self.PRE]
                if rms > max(self.floor * 2.4, 0.0016):
                    self.hot += dtb
                else:
                    self.hot = 0.0
                    # the floor learns only from what is NOT voice — a fan rises it,
                    # his own speech never does, so speaking can never close the gate on him
                    self.floor = ((0.80 * self.floor + 0.20 * rms) if rms > self.floor
                                  else max(0.97 * self.floor, 0.0004))
                if self.hot >= 0.22:                       # 0.22 s of real voice, not a click
                    self.talking = True
                    self.buf = list(self.pre)              # keep the half-word that woke us
                    self.voiced = self.silent = self.total = self.hot
                    self.hot = 0.0
            if self.talking:
                # a room that gets loud while he is already speaking must not hold the ear
                # open for 25 s: the quietest moment of the last two seconds is the floor.
                self.qwin.append(rms)
                del self.qwin[:-133]
                if len(self.qwin) >= 100:
                    self.floor = max(self.floor, 0.85 * min(self.qwin))
                self.buf.append(a.copy())
                self.total += dtb
                if rms > max(self.floor * 1.25, 0.0009):
                    self.voiced += dtb
                    self.silent = 0.0
                else:
                    self.silent += dtb
                if self.silent >= c["silence"] or self.total >= c["max"]:
                    seg = np.concatenate(self.buf) if self.buf else None
                    voiced = self.voiced
                    self.talking = False
                    self.buf = []
                    self.pre = []
                    self.qwin = []
                    self.silent = 0.0
                    self.hot = 0.0
                    if seg is not None and voiced >= c["min"]:
                        if self.q.qsize() >= 3:          # he is faster than the model
                            try:
                                self.q.get_nowait()
                                self.dropped += 1
                            except Exception:
                                pass
                        self.emits += 1
                        self.q.put(seg)
                    else:
                        self.short += 1
        except Exception as e:
            self.errors += 1
            if self.errors <= 3:
                print(f"[rawi] ear feed error {type(e).__name__}: {e}", flush=True)

    def _loop(self):
        while True:
            seg = self.q.get()
            try:
                if not self.on:
                    continue
                self.done += 1
                self.d._work(seg, len(seg) / float(SR), listen=True)
            except Exception as e:
                print(f"[rawi] ear error {type(e).__name__}: {e}", flush=True)


def log_transcript(entry: dict, on: bool = True) -> None:
    if not on:
        return
    try:
        with open(LOG_PATH, "a", encoding="utf-8") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")
    except Exception:
        pass


# ---------- dictionary ----------
_AR_DIAC = dict.fromkeys(map(ord, "\u064B\u064C\u064D\u064E\u064F\u0650\u0651\u0652\u0640"), None)


def _norm(t: str) -> str:
    t = (t or "").translate(_AR_DIAC)
    t = t.replace("\u0623", "\u0627").replace("\u0625", "\u0627").replace("\u0622", "\u0627")
    t = t.replace("\u0649", "\u064A").replace("\u0629", "\u0647")
    return t.lower().strip()


# Tone is a light prompt modifier, not a rewrite: it changes register, never meaning.
TONES = {
    "default": "",
    "formal": " Use a formal, professional register: complete sentences, no slang, no emoji.",
    "casual": " Use a relaxed, conversational register, the way one writes to a colleague.",
    "veryCasual": " Use a very casual chat register: contractions, short clauses, lower case is fine.",
    "excited": " Keep the energy high: the speaker is enthusiastic; exclamation marks are welcome.",
    "genz": " Use a contemporary, informal online register; do not invent slang that was not spoken.",
}


def apply_snippets(text: str, cfg) -> str:
    """Whole-utterance trigger -> expansion. Exact match on the normalised text only."""
    sn = cfg.get("snippets") or {}
    if not isinstance(sn, dict) or not sn or not text:
        return text
    key = _norm(text).strip(" .،؟?!")
    for trig, val in sn.items():
        if _norm(str(trig)).strip(" .،؟?!") == key and str(val).strip():
            return str(val)
    return text


def dict_terms(cfg):
    d = cfg.get("dictionary") or {}
    names = [str(x).strip() for x in (d.get("names") or []) if str(x).strip()]
    pairs = []
    for raw in (d.get("pairs") or []):
        if "=>" in str(raw):
            a, b = str(raw).split("=>", 1)
            if a.strip() and b.strip():
                pairs.append((a.strip(), b.strip()))
    return names, pairs


def apply_dictionary(text, cfg):
    """Explicit pairs first, then conservative fuzzy repair of known names."""
    if not text:
        return text
    names, pairs = dict_terms(cfg)
    out = text
    for wrong, right in pairs:
        if wrong and wrong.lower() in out.lower():
            i = out.lower().index(wrong.lower())
            out = out[:i] + right + out[i + len(wrong):]
    if not names:
        return out
    try:
        import difflib
    except Exception:
        return out
    tokens = out.split(" ")
    known = {_norm(n) for n in names}
    for i, tok in enumerate(tokens):
        core = tok.strip(".,!?;:()[]\"'\u060C\u061F")
        if len(core) < 4:
            continue
        if _norm(core) in known:
            continue
        best, score = None, 0.0
        for n in names:
            if abs(len(n) - len(core)) > 2:
                continue
            r = difflib.SequenceMatcher(None, _norm(core), _norm(n)).ratio()
            if r > score:
                best, score = n, r
        if best and score >= 0.86:
            tokens[i] = tok.replace(core, best, 1)
    # multi-word names (e.g. "Ada Lovelace") matched as sliding windows
    multi = [n for n in names if " " in n]
    if multi:
        words = " ".join(tokens).split(" ")
        for n in multi:
            k = len(n.split())
            for j in range(0, len(words) - k + 1):
                window = " ".join(words[j:j + k])
                if _norm(window) == _norm(n):
                    break
                if difflib.SequenceMatcher(None, _norm(window), _norm(n)).ratio() >= 0.86:
                    words[j:j + k] = n.split(" ")
                    break
        tokens = words
    return " ".join(tokens)


class Rawi:
    def __init__(self, cfg: dict):
        import sounddevice as sd
        self.sd = sd
        self.cfg = cfg
        self.model = None
        self.model_lock = threading.Lock()
        self.frames: list[np.ndarray] = []
        self.stream = None
        self._mic_lock = threading.Lock()
        self._mic_used = 0.0
        self.recording = False
        self.started_at = 0.0
        self.busy = threading.Semaphore(1)
        self.pending = collections.deque()      # recordings waiting for the model, in order
        self.wave = None
        # True while we are injecting our own Ctrl+V. pynput sees injected keys as real,
        # so without this every paste would start a recording (Claude, 2026-10-30).
        self.injecting = False
        # the last dictation, kept so it can be un-pasted: trust lives in the undo
        self.last = None
        self._talk_buf = []
        self._talk_timer = None
        # the ear: always-on listening, off until he latches it
        self.ear = None
        self.lang_hint = None
        self.last_lang = (None, 0.0)
        self.lang_probs = {}

    # ---------- model ----------
    # a neutral default: ordinary spoken Arabic, in whatever dialect the speaker uses.
    # override with `arabic_prompt` in config.json when your audio has a known shape.
    AR_PROMPT = ("نص عربي منطوق، بالفصحى أو بلهجة المتحدث، بعلامات ترقيم عربية. "
                 "أسماء قد تظهر: ")
    EN_PROMPT = "A clear English dictation with punctuation. Names that may appear: "
    MIX_PROMPT = ("Mixed Arabic and English dictation. Keep each part in its own script: "
                  "Arabic in Arabic letters (عربي), English in Latin letters. "
                  "Never romanise Arabic and never translate. Names that may appear: ")
    # words that only appear when Arabic has been written in Latin letters
    EN_STOP = set("the is are was were this that these those at of and for with you we they he she "
                  "it to in on my your his her our their a an i do does did not have has had will "
                  "would can could should just so but if then there here what when where who how".split())
    ROMAN_AR = ("wallah", "walla", "billah", "yani", "ya3ni", "ya'ni", "habibi", "habibti",
                "inshallah", "insha'allah", "alhamdulillah", "hamdullah", "yalla", "khallas",
                "khalas", "shukran", "mashallah", "ma'shallah", "akhi", "ukhti", "na3am",
                "wallahi", "ya rab", "yareb", "insha allah", "astaghfirullah", "tayeb", "zain")

    def pick_lang(self, audio):
        """Ask the encoder what it hears before deciding how to write it. A short line
        from a bilingual speaker is exactly where a blind auto-detect gets it wrong."""
        try:
            lang, prob, allp = self.model.detect_language(audio, language_detection_segments=2)
            self.lang_probs = dict(allp or [])
            return lang, float(prob or 0.0)
        except Exception as e:
            print(f"[rawi] detect_language failed ({type(e).__name__}: {e})", flush=True)
            return None, 0.0

    def _prompt_for(self, lang):
        names, _pairs = dict_terms(self.cfg)
        if lang == "ar":
            ar_names = [n for n in names if any("\u0600" <= ch <= "\u06FF" for ch in n)]
            base = self.cfg.get("arabic_prompt") or self.AR_PROMPT
            return base + ", ".join(ar_names or names or [])
        if lang == "en":
            return self.EN_PROMPT + ", ".join(names or [])
        if lang is None:
            return self.MIX_PROMPT + ", ".join(names or [])   # he speaks both in one breath
        return None

    def transcribe(self, audio: np.ndarray, language: str | None, listen: bool = False):
        """Whisper, tuned for a man who speaks Arabic and English in the same breath.
        Arabic must come back in Arabic script. The killer bug was an English prompt on
        Arabic audio: it steered the model into writing English for what it heard."""
        lang, prob = language, 1.0
        if lang is None:
            got, prob = self.pick_lang(audio)
            need = 0.85 if self.lang_hint else 0.60   # a man mid-conversation keeps his language
            if prob >= need and got:
                lang = got
            elif self.lang_hint:
                lang = self.lang_hint
            else:
                lang = None          # unsure and no history: let Whisper decide, with no prompt
        segs, info = self.model.transcribe(
            audio, language=lang, task="transcribe",
            initial_prompt=self._prompt_for(lang),
            beam_size=5 if lang == "ar" else 1,
            condition_on_previous_text=False,
            without_timestamps=True,
            vad_filter=not listen,
            vad_parameters={"min_silence_duration_ms": 350},
        )
        text = "".join(s.text for s in segs).strip()
        wrote = lang or getattr(info, "language", None)
        # An English opener ("okay, so—") can make the probe say "en", and the Arabic that
        # follows then comes back romanised in Latin letters. Look again before believing it.
        if wrote == "en":
            no_arabic = not any("\u0600" <= ch <= "\u06FF" for ch in text)
            roman = [w for w in self.ROMAN_AR if w in text.lower()]
            ar_runner = float(self.lang_probs.get("ar") or 0.0)
            en_stop = sum(1 for w in re.findall(r"[a-z']+", text.lower()) if w in self.EN_STOP)
            if no_arabic and (roman or (ar_runner >= 0.35 and en_stop <= 2)):
                segs2, info2 = self.model.transcribe(
                    audio, language="ar", task="transcribe",
                    initial_prompt=self._prompt_for("ar"), beam_size=5,
                    condition_on_previous_text=False, without_timestamps=True,
                    vad_filter=not listen,
                    vad_parameters={"min_silence_duration_ms": 350})
                t2 = "".join(s.text for s in segs2).strip()
                ar_letters = sum(1 for ch in t2 if "\u0600" <= ch <= "\u06FF")
                if ar_letters >= 2:
                    print(f"[rawi] second look: arabic kept ({ar_letters} letters, "
                          f"markers={roman[:2]}, runner={ar_runner:.2f})", flush=True)
                    text, wrote, segs = t2, "ar", segs2
                else:
                    print(f"[rawi] second look: stayed english (markers={roman[:2]}, "
                          f"runner={ar_runner:.2f}, en_stop={en_stop})", flush=True)
        text = apply_dictionary(text, self.cfg)
        if wrote in ("ar", "en") and (prob >= 0.60 or lang is not None):
            self.lang_hint = wrote
        self.last_lang = (lang, round(prob, 2))
        print(f"[rawi] wrote as {wrote} (probe {prob:.2f})", flush=True)
        return text, (wrote or "?"), float(getattr(info, "duration", 0.0) or 0.0)

    # ---------- the ear ----------
    def paste_last(self):
        """Put the last line wherever the caret is now — the manual half of the ear."""
        if not self.last or not self.last.get("clean"):
            beep(300, 70, self.cfg["beeps"])
            return False
        try:
            import pyperclip
            pyperclip.copy(self.last["clean"])
        except Exception:
            pass
        self.paste(self.last["clean"])
        if self.wave is not None:
            self.wave.flash("\u2713 pasted \u00b7 " + self.last["clean"][:40])
        return True

    def set_listen(self, on: bool):
        if self.ear is None:
            self.ear = Ear(self)
        on = bool(on)
        if on == self.ear.on:
            return
        if on:
            self.mic_open()
        self.ear.on = on
        if self.wave is not None:
            try:
                self.wave.show() if on else self.wave.hide()
            except Exception:
                pass
        if on:
            beep(880, 45, self.cfg["beeps"]); time.sleep(0.08); beep(1175, 55, self.cfg["beeps"])
            print("[rawi] ear open — speak; it stops on silence", flush=True)
        else:
            self.ear.talking = False
            self.ear.buf = []
            beep(1175, 45, self.cfg["beeps"]); time.sleep(0.08); beep(880, 55, self.cfg["beeps"])
            print("[rawi] ear closed", flush=True)

    # ---------- audio ----------
    def mic_open(self):
        """Open the microphone if it is closed (~75 ms on a MacBook)."""
        with self._mic_lock:
            self._mic_used = time.time()
            if self.stream is None:
                self.stream = self.sd.InputStream(samplerate=SR, channels=1, dtype="float32",
                                                  callback=self._cb, blocksize=0)
                self.stream.start()

    def _mic_closer(self):
        """Close an idle microphone. macOS shows its orange mic light the whole time a stream is
        open, and AirPods drop to call quality, so on a Mac rawi holds the mic only while it is used.
        "mic_close_after": seconds idle before it closes; 0 keeps it open (the default elsewhere)."""
        while True:
            time.sleep(1.0)
            self.mic_close_if_idle()

    def mic_close_if_idle(self) -> bool:
        after = float(self.cfg.get("mic_close_after", 5 if IS_MAC else 0) or 0)
        with self._mic_lock:
            if self.stream is None or after <= 0:
                return False
            if self.recording or (self.ear is not None and self.ear.on):
                self._mic_used = time.time()
                return False
            if time.time() - self._mic_used < after:
                return False
            st, self.stream = self.stream, None
        try:
            st.stop(); st.close()
        except Exception as e:
            print(f"[rawi] closing the mic failed: {e}", flush=True)
        return True

    def _cb(self, indata, frames, time_info, status):  # sounddevice callback
        if self.ear is not None and self.ear.on and not self.recording:
            self.ear.feed(indata)
            if self.wave is not None:
                try:
                    self.wave.push(min(1.0, float(np.sqrt(np.mean(np.square(indata)))) * 9.0))
                except Exception:
                    pass
        if self.recording:
            self.frames.append(indata.copy())
            if self.wave is not None:
                try:
                    rms = float(np.sqrt(np.mean(np.square(indata))))
                    self.wave.push(min(1.0, rms * 6.0))
                except Exception:
                    pass

    def start_rec(self):
        if self.recording:
            return
        self.mic_open()
        self.frames = []
        self.recording = True
        self.started_at = time.time()
        beep(880, 55, self.cfg["beeps"])
        if self.wave is not None:
            self.wave.show()
        print("[rawi] recording...", flush=True)

    def abort_rec(self):
        """Discard a recording that turned out to be a shortcut, without a beep."""
        self.recording = False
        self.frames = []
        if self.wave is not None:
            self.wave.hide()

    def stop_and_process(self):
        if not self.recording:
            return
        self.recording = False
        beep(520, 55, self.cfg["beeps"])
        if self.wave is not None:
            self.wave.hide()
        dur = time.time() - self.started_at
        frames = self.frames
        self.frames = []
        if not frames:
            print("[rawi] no audio, ignored", flush=True)
            return
        audio = np.concatenate(frames, axis=0).reshape(-1).astype(np.float32)
        if len(audio) / float(SR) < float(self.cfg["min_seconds"]):
            print("[rawi] too short, ignored", flush=True)   # the audio's length, not the wall clock
            return
        print(f"[rawi] captured {dur:.1f}s ({len(audio)/SR:.1f}s audio), transcribing...", flush=True)
        threading.Thread(target=self._work, args=(audio, dur), daemon=True).start()

    # ---------- work ----------
    def _log_record(self, cleaned, text, lg, app, took, ctook, cstatus, dur, fid, listen):
        try:
            log_transcript({"ts": datetime.datetime.now().isoformat(timespec="seconds"),
                            "seconds": round(dur, 2), "language": lg, "model": self.cfg["model"],
                            "transcribe_s": round(took, 2), "clean_s": round(ctook, 2),
                            "clean": cstatus, "text": cleaned, "raw": text, "app": app,
                            "fid": fid,
                            "mood": (rawi_mood.tag(cleaned) if str(self.cfg.get("mood", "auto")) != "off" else None),
                            "to": ("voice" if self.cfg.get("talk") else ""),
                            "listen": bool(listen)},
                           self.cfg["log"] and not self.cfg.get("dumb"))
        except Exception as e:
            print(f"[rawi] log failed {type(e).__name__}: {e}", flush=True)

    def _talk_add(self, text: str):
        """Hold the line, then send one thought instead of four fragments.

        He speaks, stops to think, speaks again. Without this, a stop-to-think becomes a turn of its own
        and the conversation stutters. The timer is reset by every new fragment, so only his real silence
        flushes.
        """
        if not getattr(self, "_talk_buf", None):
            self._talk_buf = []
        self._talk_buf.append(text)
        joined = " ".join(self._talk_buf)
        delay = max(0.4, float(self.cfg.get("talk_merge_ms", 1200)) / 1000.0)
        if len(joined) > 400:                    # never hold a paragraph hostage to a timer
            delay = 0.05
        t = getattr(self, "_talk_timer", None)
        if t is not None:
            try:
                t.cancel()
            except Exception:
                pass
        timer = threading.Timer(delay, self._flush_talk)
        timer.daemon = True
        self._talk_timer = timer
        timer.start()
        if self.wave is not None and len(self._talk_buf) > 1:
            self.wave.flash(f"holding {len(self._talk_buf)} lines", ok=True, seconds=1.2)
        print(f"[rawi] talk buffer {len(self._talk_buf)} ({len(joined)} chars, flush in {delay:.1f}s)", flush=True)

    def _flush_talk(self):
        buf = " ".join(getattr(self, "_talk_buf", []) or []).strip()
        self._talk_buf = []
        self._talk_timer = None
        if not buf:
            return
        ok = _voice_post("/say", {"text": buf})
        how = "voice"
        beep(880 if ok else 330, 50, self.cfg["beeps"])
        if not ok:      # never lose his words because a bridge is down
            try:
                import pyperclip
                pyperclip.copy(buf)
            except Exception:
                pass
        if self.wave is not None:
            self.wave.flash(("\u2192 voice \u00b7 " if ok else "voice offline \u00b7 copied \u00b7 ")
                            + buf[:34], ok=ok, seconds=2.6)
        print(f"[rawi] talk send via {how} ok={ok}: {buf[:90]}", flush=True)

    def _work(self, audio: np.ndarray, dur: float, listen: bool = False):
        if not self.busy.acquire(blocking=False):
            # Never lose words to a slow machine: hold the audio, do it next.
            if len(self.pending) < QUEUE_MAX:
                self.pending.append((audio, dur, listen))
                print(f"[rawi] still transcribing the last one; queued ({len(self.pending)})", flush=True)
                if self.wave is not None:
                    self.wave.flash("queued \u00b7 one at a time", ok=True, seconds=1.8)
            else:
                print(f"[rawi] queue full ({QUEUE_MAX}); dropped", flush=True)
                beep(300, 90, self.cfg["beeps"])
            return
        try:
            lock = str(self.cfg.get("lang_lock") or "auto").lower()
            if lock in ("ar", "en"):
                lang = lock                      # he decided, not the model
            else:
                lang = None if str(self.cfg["language"]).lower() == "auto" else self.cfg["language"]
            t0 = time.time()
            text, lg, _ = self.transcribe(audio, lang, listen=listen)
            took = time.time() - t0
            if not text:
                if listen:
                    if self.ear is not None:
                        self.ear.empty += 1
                    if self.wave is not None:
                        self.wave.flash("heard nothing", ok=False, seconds=1.6)
                return
            app = active_app()
            c0 = time.time()
            if self.cfg.get("dumb"):
                cleaned, cstatus = text, "dumb"
            else:
                cleaned, cstatus = self.clean(text, app=app)
            ctook = time.time() - c0
            snip = apply_snippets(cleaned, self.cfg)
            if snip != cleaned:
                cleaned, cstatus = snip, "snippet"
            print(f"[rawi] {lg} in {took:.2f}s (+clean {ctook:.2f}s {cstatus}): {cleaned}", flush=True)
            fid = fidelity(text, cleaned)
            self.last = {"raw": text, "clean": cleaned, "app": app,
                         "ts": time.time(), "pasted": bool(self.cfg["paste"]) and not listen}
            if listen and self.cfg.get("talk"):
                # Voice mode: his pauses are breathing, not sentences ending.
                self._talk_add(cleaned)
                self._log_record(cleaned, text, lg, app, took, ctook, cstatus, dur, fid, listen)
                return
            if listen:
                # his words, ready — never pasted into a window he did not choose
                try:
                    import pyperclip
                    pyperclip.copy(cleaned)
                except Exception:
                    pass
                beep(660, 45, self.cfg["beeps"])
                if self.wave is not None:
                    self.wave.flash("\u2713 copied \u00b7 " + cleaned[:44])
            elif self.cfg["paste"]:
                self.paste(cleaned)
            self._log_record(cleaned, text, lg, app, took, ctook, cstatus, dur, fid, listen)
        except Exception as e:
            print(f"[rawi] ERROR {type(e).__name__}: {e}", flush=True)
            beep(220, 120, self.cfg["beeps"])
        finally:
            self.busy.release()
            if self.pending:                     # drain, one at a time, in order
                nxt = self.pending.popleft()
                threading.Thread(target=self._work, args=nxt, daemon=True).start()

    # ---------- cleanup ----------
    def profile_for(self, app: str | None) -> dict:
        """The profile whose app list appears in this window title, or an empty one."""
        t = (app or "").lower()
        if not t:
            return {}
        for prof in self.cfg.get("profiles") or []:
            for key in prof.get("apps") or []:
                if key and key.lower() in t:
                    return prof
        return {}

    def clean(self, text: str, level: str | None = None, tone: str | None = None,
              app: str | None = None) -> tuple[str, str]:
        """Return (clean_text, status). Never raises: on any failure the raw text wins."""
        c = self.cfg.get("clean") or {}
        if not c.get("enabled"):
            return text, "off"
        prof = self.profile_for(app)
        level = (level or prof.get("level") or c.get("level") or "light").lower()
        tone = (tone or prof.get("tone") or c.get("tone") or "default").lower()
        if level == "none":
            return text, f"off:{prof.get('name', 'level')}"
        try:
            import urllib.request
            names, _ = dict_terms(self.cfg)
            sysmsg = c["system"]
            if level == "full":
                sysmsg += (" Also tighten the result: remove repeated words and false starts, "
                           "split run-on sentences, and keep it to the fewest words that carry "
                           "the meaning. Do not change the meaning.")
            sysmsg += TONES.get(tone, "")
            if names:
                sysmsg += (" Keep these names spelled exactly like this if you see them: "
                           + ", ".join(names) + ".")
            body = json.dumps({
                "messages": [{"role": "system", "content": sysmsg},
                             {"role": "user", "content": text}],
                "temperature": 0,
                "max_tokens": int(c.get("max_tokens", 160)),
                "chat_template_kwargs": {"enable_thinking": False},
            }).encode("utf-8")
            req = urllib.request.Request(c["endpoint"], data=body,
                                         headers={"Content-Type": "application/json"})
            with urllib.request.urlopen(req, timeout=float(c.get("timeout", 25))) as resp:
                j = json.loads(resp.read().decode("utf-8"))
            out = (j["choices"][0]["message"].get("content") or "").strip()
            if not out:
                return text, "empty"
            if len(out) > max(3 * len(text), len(text) + 200):
                return text, "runaway"
            return out, "ok"
        except Exception as e:
            return text, f"{type(e).__name__}"

    # ---------- output ----------
    def unpaste(self) -> dict:
        """Undo the last paste in place and put the raw words back.

        ctrl+z reverts the paste in every editor and text field worth using; then the
        raw goes in the same way the clean did, so the same caret rule applies.
        """
        L = self.last
        if not L or not L.get("pasted"):
            return {"ok": False, "error": "nothing to undo"}
        app = active_app()
        if L.get("app") and app and L["app"] != app:
            return {"ok": False, "error": f"focus moved to {app[:40]}"}
        import pyautogui
        self.injecting = True
        try:
            pyautogui.hotkey(PASTE_MOD, "z")
            time.sleep(0.20)
        finally:
            self.injecting = False
        self.paste(L["raw"])
        L["pasted"] = False
        return {"ok": True, "raw": L["raw"]}

    def paste(self, text: str):
        import pyperclip
        import pyautogui
        old = None
        if self.cfg["restore_clipboard"]:
            try:
                old = pyperclip.paste()
            except Exception:
                old = None
        pyperclip.copy(text)
        time.sleep(0.10)
        self.injecting = True
        try:
            pyautogui.hotkey(PASTE_MOD, "v")
        finally:
            time.sleep(0.12)
            self.injecting = False
        time.sleep(0.35)
        if old is not None:
            try:
                pyperclip.copy(old)
            except Exception:
                pass


def key_to_name(k) -> str:
    from pynput import keyboard
    if isinstance(k, keyboard.Key):
        # on macOS pynput gives Key.ctrl and Key.ctrl_l one keycode, so left Ctrl arrives as "ctrl"
        return MAC_LEFT.get(k.name, k.name) if IS_MAC else k.name
    try:
        return k.char
    except Exception:
        return str(k)


GATE_PY = ""   # optional: a script that prints {"holder": ...} when another app holds the GPU


def cuda_available() -> bool:
    """True only when a CUDA device is actually usable here (no driver, no dice)."""
    try:
        import ctranslate2
        return int(ctranslate2.get_cuda_device_count()) > 0
    except Exception:
        return False


def gpu_gate():
    """Who else is using this GPU, if anyone. None when free, unreadable, or unconfigured."""
    if not GATE_PY:
        return None
    try:
        out = subprocess.run([sys.executable, GATE_PY, "status"], capture_output=True, text=True,
                             timeout=20, creationflags=NO_WINDOW)
        for line in reversed(out.stdout.strip().splitlines()):
            line = line.strip()
            if line.startswith("{"):
                return (json.loads(line) or {}).get("holder")
    except Exception:
        return None
    return None


GATE_YIELD_PURPOSES = ("exclusive", "training", "render")


def gate_requires_yield(holder):
    """True when the current holder should not share the card with a transcriber."""
    purpose = str((holder or {}).get("purpose") or "").lower()
    return any(w in purpose for w in GATE_YIELD_PURPOSES)


KEYS_SEEN = []


def ipc_server(d, host="127.0.0.1", port=18090):
    """A local hand for the dock and the Hub: toggle / start / stop / status. Never raises."""
    import socket

    def handle(conn):
        try:
            conn.settimeout(3)
            buf = b""
            while not buf.endswith(b"\n"):
                chunk = conn.recv(4096)
                if not chunk:
                    break
                buf += chunk
                if len(buf) > 65536:
                    break
            req = json.loads(buf.decode("utf-8") or "{}")
        except Exception:
            req = {}
        cmd = str(req.get("cmd") or "status")
        try:
            if cmd == "toggle":
                if d.recording:
                    d.stop_and_process()
                else:
                    d.start_rec()
            elif cmd == "start":
                d.start_rec()
            elif cmd == "wake":
                # a wave is not a click: the eye's own verb, so Rawi can say who woke it
                already = bool(d.ear and d.ear.on)
                if not already:
                    d.set_listen(True)
                _voice_post("/say", {"text": "Yes?" if not already else ""})
                if d.wave is not None:
                    d.wave.flash("woken by a wave", ok=True, seconds=2.2)
                print(f"[rawi] woken by a wave (already={already})", flush=True)
                try:
                    conn.sendall((json.dumps({"ok": True, "wake": True, "listening": bool(d.ear and d.ear.on)}) + "\n").encode())
                except Exception:
                    pass
                return
            elif cmd == "talk":
                want = req.get("on")
                if want is None:
                    want = not bool(d.cfg.get("talk"))
                d.cfg["talk"] = bool(want)
                try:
                    save_cfg(d.cfg)
                except Exception:
                    pass
                br = _voice_post("/mode", {"on": bool(want)}) if want else True
                try:
                    conn.sendall((json.dumps({"ok": True, "talk": bool(want), "bridge": br}) + "\n").encode())
                except Exception:
                    pass
                return
            elif cmd == "listen":
                want = req.get("on")
                if want is None:
                    want = not bool(d.ear and d.ear.on)
                d.set_listen(bool(want))
            elif cmd == "stop":
                d.stop_and_process()
            elif cmd == "swap":
                res = d.unpaste()
                busy = not d.busy.acquire(blocking=False)
                if not busy:
                    d.busy.release()
                out = dict(res, recording=bool(d.recording), busy=busy)
                try:
                    conn.sendall((json.dumps(out, ensure_ascii=False) + "\n").encode("utf-8"))
                except Exception:
                    pass
                try:
                    conn.close()
                except Exception:
                    pass
                return
            elif cmd == "dumb":
                d.cfg["dumb"] = bool(req.get("on", not d.cfg.get("dumb")))
                save_cfg(d.cfg)
            elif cmd == "raw":
                text = str(req.get("text") or "")
                if text:
                    d.paste(text)
            busy = not d.busy.acquire(blocking=False)
            if not busy:
                d.busy.release()
            out = {"ok": True, "recording": bool(d.recording), "busy": busy,
                   "dumb": bool(d.cfg.get("dumb")),
                   "swap": bool(d.last and d.last.get("pasted")),
                   "listening": bool(d.ear and d.ear.on),
                   "dropped": int(d.ear.dropped) if d.ear else 0,
                   "lvl": round(float(d.ear.lvl) if d.ear else 0.0, 4),
                   "floor": round(float(d.ear.floor) if d.ear else 0.0, 4),
                   "heard": bool(d.ear.talking) if d.ear else False,
                   "peak": round(float(d.ear.peak) if d.ear else 0.0, 4),
                   "keys": list(KEYS_SEEN),
                   "ear": ({"emits": int(d.ear.emits), "done": int(d.ear.done),
                            "short": int(d.ear.short), "dropped": int(d.ear.dropped),
                            "errors": int(d.ear.errors)} if d.ear else {})}
        except Exception as e:
            out = {"ok": False, "error": f"{type(e).__name__}: {e}"}
        try:
            conn.sendall((json.dumps(out) + "\n").encode("utf-8"))
        except Exception:
            pass
        try:
            conn.close()
        except Exception:
            pass

    def loop():
        try:
            s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            s.bind((host, port))
            s.listen(8)
            print(f"[rawi] ipc on {host}:{port}", flush=True)
        except Exception as e:
            print(f"[rawi] ipc unavailable ({type(e).__name__}: {e})", flush=True)
            return
        while True:
            try:
                conn, _ = s.accept()
                threading.Thread(target=handle, args=(conn,), daemon=True).start()
            except Exception:
                time.sleep(0.2)

    threading.Thread(target=loop, daemon=True).start()


def start_dock():
    """Best effort: the small always-on-top capsule, in its own process.

    Its own process, not a thread: tkinter wants one interpreter per thread and the pill
    already owns one. Two roots in one process is a crash waiting for a busy morning.
    """
    # macOS gets a menu bar item instead: a click on a Tk window activates python there,
    # so the paste would land in the dock; a status-bar menu never takes focus
    script = "rawi_menubar.py" if IS_MAC else "rawi_dock.py"
    dockpid = os.path.join(ROOT, "dock.pid")
    try:
        old = int(open(dockpid).read().strip() or 0)
        if old and pid_alive(old):
            print(f"[rawi] dock already up (pid {old})", flush=True)
            return None
    except Exception:
        pass
    try:
        exe = os.path.join(os.path.dirname(sys.executable), "pythonw.exe")
        if not os.path.exists(exe):
            exe = sys.executable
        logf = open(os.path.join(ROOT, "docker.log"), "a", encoding="utf-8", errors="replace")
        p = subprocess.Popen([exe, os.path.join(ROOT, script)],
                             stdout=logf, stderr=logf, creationflags=NO_WINDOW)
        try:
            open(dockpid, "w").write(str(p.pid))
        except Exception:
            pass
        print(f"[rawi] {script[:-3]} pid {p.pid}", flush=True)
        return p
    except Exception as e:
        print(f"[rawi] dock unavailable ({type(e).__name__}: {e})", flush=True)
        return None



# ---------------- doctor ----------------
def _short(path: str) -> str:
    """Home-relative for display: a doctor line should not print somebody's account name."""
    home = os.path.expanduser("~")
    try:
        if home and path.startswith(home):
            return "~" + path[len(home):]
    except Exception:
        pass
    return path


def _cache_dir(model: str) -> str:
    """Where faster-whisper's downloader would put this model: any `*faster-whisper-<model>` repo."""
    hub = os.path.join(os.path.expanduser("~"), ".cache", "huggingface", "hub")
    suffix = f"faster-whisper-{model.replace('/', '--')}"
    try:
        for name in os.listdir(hub):
            if name.endswith(suffix) and os.path.isdir(os.path.join(hub, name, "snapshots")):
                return os.path.join(hub, name)
    except Exception:
        pass
    return os.path.join(hub, f"models--Systran--{suffix}")


def _port_busy(port: int) -> bool:
    import socket
    s = socket.socket()
    s.settimeout(0.4)
    try:
        return s.connect_ex(("127.0.0.1", port)) == 0
    finally:
        s.close()


def doctor(cfg: dict, deep: bool = False) -> int:
    """Tell a stranger why rawi is not working. Prints one line per check; exits 1 on a hard failure."""
    fails = warns = 0

    def ok(msg):  print(f"  ok    {msg}")
    def warn(msg):
        nonlocal warns; warns += 1; print(f"  warn  {msg}")
    def bad(msg):
        nonlocal fails; fails += 1; print(f"  FAIL  {msg}")

    print(f"rawi doctor \u00b7 {sys.platform} \u00b7 python {sys.version.split()[0]}")

    print("\n[1] interpreter")
    if sys.version_info < (3, 10):
        bad(f"python {sys.version.split()[0]} is too old; 3.10-3.12 required")
    elif sys.version_info >= (3, 13):
        warn(f"python {sys.version.split()[0]}: 3.13+ is untested (3.10-3.12 recommended)")
    else:
        ok(f"python {sys.version.split()[0]}")

    print("\n[2] packages")
    need = ["numpy", "faster_whisper", "sounddevice", "pynput", "pyperclip", "requests"]
    for m in need:
        try:
            __import__(m); ok(m)
        except Exception as e:
            bad(f"{m} missing ({type(e).__name__}) \u2014 pip install -r requirements.txt")
    # the paste path: without these, a dictation is transcribed and then silently lost
    if str(cfg.get("paste", True)).lower() != "false":
        for m in ("pyautogui", "pygetwindow") if IS_WIN else ("pyautogui",):   # pygetwindow names the window on Windows only
            try:
                __import__(m); ok(m)
            except Exception:
                bad(f"{m} missing \u2014 paste cannot work; pip install -r requirements.txt")
    for m in ("soundfile", "webview"):
        try:
            __import__(m); ok(m if m == "soundfile" else "webview (hub window)")
        except Exception:
            warn(f"{m} missing (optional: {'--test / wav input' if m == 'soundfile' else 'the hub window'})")

    print("\n[3] microphone")
    try:
        import sounddevice as sd
        dev = sd.query_devices(kind="input")
        ok(f"default input: {dev['name'][:48]} ({int(dev['default_samplerate'])} Hz)")
        try:
            st = sd.InputStream(samplerate=SR, channels=1, dtype="float32")
            st.start(); st.stop(); st.close()
            ok("opened the microphone for 0.1 s")
        except Exception as e:
            bad(f"could not open the mic ({type(e).__name__}: {e})")
    except Exception as e:
        bad(f"no input device visible ({type(e).__name__})")

    print("\n[4] GPU")
    try:
        import ctranslate2
        n = ctranslate2.get_cuda_device_count()
    except Exception:
        n = 0
    import rawi_mlx
    engine = str(cfg.get("engine") or "auto").lower()
    if n:
        ok(f"{n} CUDA device(s) visible to ctranslate2")
    elif IS_MAC and engine in ("auto", "mlx") and rawi_mlx.available():
        ok("Apple GPU via MLX (mlx-whisper) \u2014 no CUDA needed")
    elif IS_MAC:
        warn("no CUDA on a Mac \u2014 CPU is slow; on Apple silicon `pip install mlx-whisper` runs it on the GPU")
    else:
        warn("no CUDA device visible \u2014 will run on CPU (set compute_type to int8 and a small model)")

    print("\n[5] model")
    model, ctype = str(cfg.get("model")), str(cfg.get("compute_type"))
    if os.path.isdir(model) or (os.path.isfile(model)):
        ok(f"local model path: {model}")
    elif os.path.isdir(_cache_dir(model)):
        ok(f"{model} is downloaded ({_short(_cache_dir(model))})")
    else:
        warn(f"{model} is not downloaded yet; the first run will fetch it (needs network once)")
    if deep:
        try:
            from faster_whisper import WhisperModel
            dev2 = "cuda" if n else "cpu"
            ct2 = ctype if n else "int8"
            t0 = time.time()
            WhisperModel(model, device=dev2, compute_type=ct2)
            ok(f"loaded {model} on {dev2}/{ct2} in {time.time()-t0:.1f} s (--deep)")
        except Exception as e:
            bad(f"model failed to load ({type(e).__name__}: {e})")

    print("\n[6] config")
    cfgp = os.path.join(ROOT, "config.json")
    if not os.path.exists(cfgp):
        warn("no config.json yet (one will be created with defaults on first run)")
    else:
        try:
            json.load(open(cfgp, encoding="utf-8"))
            ok("config.json parses (next to rawi.py)")
        except Exception as e:
            bad(f"config.json is not valid JSON ({type(e).__name__}) \u2014 delete it and start over")
    hk = str(cfg.get("hold_key"))
    try:
        from pynput import keyboard
        if not (hasattr(keyboard.Key, hk) or len(hk) == 1):
            warn(f"hold_key {hk!r} is not a known key name; use e.g. ctrl_l, alt_r, f6")
    except Exception:
        pass

    print("\n[7] paste path")
    if IS_WIN:
        ok("windows: clipboard + ctrl+v")
    elif IS_MAC:
        ok("macos: clipboard + cmd+v (needs Accessibility permission for the terminal/python)")
    else:
        ok("linux: clipboard + ctrl+v (needs xdotool)")

    print("\n[8] ports")
    for port, label in ((18090, "daemon IPC"), (18097, "voice server")):
        if _port_busy(port):
            warn(f"{port} ({label}) already in use \u2014 another rawi may be running")
        else:
            ok(f"{port} ({label}) free")

    print("\nsummary:", "ready" if not fails else f"{fails} failure(s), {warns} warning(s)")
    return 0 if not fails else 1


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--test", metavar="AUDIO", help="transcribe a file and print; no paste")
    ap.add_argument("--model", help="override model name")
    ap.add_argument("--demo", action="store_true", help="show the voice wave for a few seconds and exit")
    ap.add_argument("--doctor", action="store_true", help="check the machine, the mic, the model and the config")
    ap.add_argument("--deep", action="store_true", help="with --doctor: also load the model (slow)")
    ap.add_argument("--seconds", type=float, default=8.0, help="demo duration")
    args = ap.parse_args()

    if args.doctor:
        return doctor(load_cfg(), deep=args.deep)

    # single instance: a second daemon means two hotkey listeners fighting over one mic
    try:
        old_pid = int(open(PIDFILE, encoding="utf-8").read().strip())
    except Exception:
        old_pid = 0
    if old_pid and old_pid != os.getpid() and pid_alive(old_pid):
        print(f"[rawi] already running as {old_pid}; this one exits", flush=True)
        return 0
    try:
        with open(PIDFILE, "w", encoding="utf-8") as _f:
            _f.write(str(os.getpid()))
    except Exception:
        pass

    cfg = load_cfg()
    if args.model:
        cfg["model"] = args.model

    if args.demo:
        if Wave is None:
            print("[rawi] wave overlay unavailable (tkinter?)", flush=True)
            return 1
        print("[rawi] demo: watch the bottom of the screen", flush=True)
        if IS_MAC:
            w = Wave(own_thread=False)
            threading.Thread(target=lambda: (w.demo(args.seconds), w.quit()), daemon=True).start()
            w.run()
        else:
            Wave().demo(args.seconds)
        return 0

    # Apple silicon: Whisper on the GPU through MLX when it is installed (rawi_mlx.py), about
    # 6x faster than CTranslate2 on the CPU. "engine": "ctranslate2" keeps the old path.
    model, t0 = None, time.time()
    engine = str(cfg.get("engine") or "auto").lower()
    if engine == "mlx" or (engine == "auto" and IS_MAC):
        import rawi_mlx
        if rawi_mlx.available():
            print(f"[rawi] loading {cfg['model']} (MLX on the Apple GPU) ...", flush=True)
            try:
                model = rawi_mlx.MlxWhisper(cfg["model"])
            except Exception as e:
                print(f"[rawi] MLX load failed ({type(e).__name__}: {e}) -> CTranslate2", flush=True)
        elif engine == "mlx":
            print("[rawi] engine mlx asked for, but mlx-whisper is not installed -> CTranslate2", flush=True)
    if model is None:
        device, ctype = "cuda", cfg["compute_type"]
        want = str(cfg.get("device") or "auto").lower()
        if want == "cpu":
            device, ctype = "cpu", "int8"
            print("[rawi] device: cpu (from config)", flush=True)
        elif want == "auto" and not cuda_available():
            device, ctype = "cpu", "int8"
            print("[rawi] no CUDA device here -> CPU int8", flush=True)
        else:
            holder = gpu_gate()
            if holder and gate_requires_yield(holder):
                device, ctype = "cpu", "int8"
                print(f"[rawi] GPU busy with {holder.get('owner')} ({holder.get('purpose')}) -> CPU int8", flush=True)
        print(f"[rawi] loading {cfg['model']} ({ctype} on {device}) ...", flush=True)
        t0 = time.time()
        from faster_whisper import WhisperModel
        try:
            model = WhisperModel(cfg["model"], device=device, compute_type=ctype)
        except Exception as e:
            print(f"[rawi] load failed ({type(e).__name__}: {e})", flush=True)
            if device == "cuda":
                try:
                    model = WhisperModel(cfg["model"], device="cuda", compute_type="int8_float16")
                except Exception as e2:
                    print(f"[rawi] cuda int8_float16 failed ({type(e2).__name__}: {e2}) -> CPU int8", flush=True)
                    model = WhisperModel(cfg["model"], device="cpu", compute_type="int8")
            else:
                raise
    print(f"[rawi] model ready in {time.time()-t0:.1f}s", flush=True)

    d = Rawi(cfg)
    d.model = model

    if args.test:
        import soundfile as sf
        audio, sr = sf.read(args.test, dtype="float32", always_2d=False)
        if audio.ndim > 1:
            audio = audio.mean(axis=1)
        if sr != SR:
            import math
            n = int(len(audio) * SR / sr)
            idx = np.linspace(0, len(audio) - 1, n)
            audio = np.interp(idx, np.arange(len(audio)), audio).astype(np.float32)
        lang = None if str(cfg["language"]).lower() == "auto" else cfg["language"]
        t0 = time.time()
        text, lg, dur = d.transcribe(audio, lang)
        print(f"[{lg}] {time.time()-t0:.2f}s for {len(audio)/SR:.1f}s audio")
        print(text)
        return 0

    # live mode
    target = cfg["hold_key"]
    if IS_MAC:
        target = MAC_LEFT.get(target, target)    # "ctrl" and "ctrl_l" are one key there
    mode = cfg["mode"]
    print(f"[rawi] key={target} mode={mode} lang={cfg['language']} paste={cfg['paste']}", flush=True)

    if Wave is not None:
        try:
            d.wave = Wave(own_thread=not IS_MAC)     # macOS: Tk gets the main thread at the end
            print("[rawi] wave overlay ready", flush=True)
        except Exception as e:
            print(f"[rawi] wave overlay failed ({type(e).__name__}: {e}); continuing without it", flush=True)

    d.ear = Ear(d)
    threading.Thread(target=ensure_mouth, daemon=True).start()
    ipc_server(d)
    if os.environ.get("RAWI_NO_DOCK") != "1":
        d.dock = start_dock()

    d.mic_open()
    threading.Thread(target=d._mic_closer, daemon=True).start()

    from pynput import keyboard
    import time as _time
    state = {"down": False, "latched": False, "tap_at": 0.0, "press_at": 0.0,
             "pending": False, "combo": False}
    TAP = 0.30      # a press shorter than this is a tap, not a hold
    DOUBLE = 0.55   # two taps inside this window latch the recorder on
    HOLD = 0.22     # left Ctrl alone this long, with no other key, is a recording
    timer = {"t": None}

    def _cancel_pending():
        state["pending"] = False
        if timer["t"] is not None:
            try:
                timer["t"].cancel()
            except Exception:
                pass
            timer["t"] = None

    def _fire():
        # Only a plain, still-held Ctrl starts the microphone. Ctrl+C, Ctrl+V and every
        # other shortcut set `combo` and never reach here.
        if state["pending"] and state["down"] and not state["combo"] and not d.recording:
            state["by_hold"] = True
            d.start_rec()

    lkey = str((cfg.get("listen") or {}).get("key") or "f8").lower()

    def on_press(k):
        name = key_to_name(k)
        try:
            KEYS_SEEN.append(str(name))
            del KEYS_SEEN[:-8]
        except Exception:
            pass
        if d.injecting:               # our own paste, not him
            return
        if name == str(cfg.get("talk_key") or "f7").lower():
            on = not bool(d.cfg.get("talk"))
            d.cfg["talk"] = on
            try:
                save_cfg(d.cfg)
            except Exception:
                pass
            ok = _voice_post("/mode", {"on": on}) if on else True
            if on and not (d.ear and d.ear.on):
                d.set_listen(True)          # voice mode means the ear is open
            beep(1046 if on else 523, 70, d.cfg["beeps"])
            if d.wave is not None:
                if on and not ok:
                    msg = "voice on \u00b7 no voice server"
                else:
                    msg = "voice \u00b7 on" if on else "voice \u00b7 off"
                d.wave.flash(msg, ok=ok or not on, seconds=2.6)
            print(f"[rawi] voice={on}", flush=True)
            return
        if name == str(cfg.get("lang_key") or "f10").lower():
            order = ["auto", "ar", "en"]
            cur = str(d.cfg.get("lang_lock") or "auto")
            nxt = order[(order.index(cur) + 1) % 3] if cur in order else "auto"
            d.cfg["lang_lock"] = nxt
            try:
                save_cfg(d.cfg)
            except Exception:
                pass
            if d.wave is not None:
                d.wave.flash({"auto": "language: auto", "ar": "language: Arabic \u00b7 \u0639\u0631\u0628\u064a", "en": "language: English"}[nxt])
            beep(700 if nxt != "auto" else 560, 60, d.cfg["beeps"])
            print(f"[rawi] lang_lock={nxt}", flush=True)
            return
        if name == str(cfg.get("paste_key") or "f9").lower():
            d.paste_last()
            return
        if name == lkey:              # the latch: one press opens the ear, another closes it
            d.set_listen(not bool(d.ear and d.ear.on))
            return
        if name != target:
            if state["pending"]:
                state["combo"] = True
                if d.recording and state.get("by_hold"):
                    d.abort_rec()     # Ctrl was held long enough, then a letter arrived: a shortcut
            return
        if state["down"]:
            return
        threading.Thread(target=d.mic_open, daemon=True).start()   # ready before HOLD ends
        state["down"] = True
        state["press_at"] = _time.time()
        state["combo"] = False

        if state["latched"]:          # a single tap ends a latched recording
            state["latched"] = False
            d.stop_and_process()
            return

        if mode == "toggle":
            if d.recording:
                d.stop_and_process()
            else:
                d.start_rec()
            return

        # hold mode: wait to see whether this is Ctrl alone or the start of a shortcut
        state["pending"] = True
        state["by_hold"] = False
        timer["t"] = threading.Timer(HOLD, _fire)
        timer["t"].daemon = True
        timer["t"].start()

    def on_release(k):
        name = key_to_name(k)
        if name != target or not state["down"]:
            return
        state["down"] = False
        held = _time.time() - state["press_at"]
        was_pending = state["pending"]
        _cancel_pending()

        if mode != "hold":
            return
        if state["combo"] and not d.recording:
            return                    # a shortcut: nothing happened, nothing to undo
        if d.recording and not was_pending:
            d.stop_and_process()      # a latched tap ends here
            return
        if held < TAP and not d.recording:
            now = _time.time()
            if now - state["tap_at"] < DOUBLE:
                state["tap_at"] = 0.0
                state["latched"] = True
                d.start_rec()
                beep(880, 45, cfg["beeps"]); _time.sleep(0.09); beep(1100, 45, cfg["beeps"])
            else:
                state["tap_at"] = now
            return
        if d.recording:
            d.stop_and_process()

    with keyboard.Listener(on_press=on_press, on_release=on_release) as listener:
        print("[rawi] listening. Ctrl+C here to quit.", flush=True)
        try:
            if IS_MAC and d.wave is not None:
                import signal
                signal.signal(signal.SIGINT, lambda *_: d.wave.quit())
                if not d.wave.run():          # Tk could not start: dictate without the pill
                    d.wave = None
                    signal.signal(signal.SIGINT, signal.default_int_handler)
                    listener.join()
            else:
                listener.join()
        except KeyboardInterrupt:
            pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
