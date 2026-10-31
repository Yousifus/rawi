#!/usr/bin/env python3
"""voice_out.py — a resident local TTS server. Kokoro (or Piper) loaded once, spoken on demand.

Engine from voice.json: "engine": "kokoro" (default, English only) or "piper" (Arabic + English:
`pip install piper-tts`; "voice_ar" / "voice_en" pick voices, defaults Kareem and LibriTTS-R).

Why this exists: loading a TTS model per call costs tens of seconds. A conversation cannot survive
that. This keeps the pipeline warm and turns a line into sound in about a second.

Protocol (loopback only):
    POST /say    {"text": "...", "voice": "af_heart"}  ->  {"ok":true,"ms":synth_ms,"dur_s":..}
    POST /stop   {}                                     ->  {"ok":true}
    GET  /health                                        ->  {"ok":true,"ready":bool}
"""
import json
import os
import re
import sys
import threading
import time

import numpy as np

PORT = int(os.environ.get("RAWI_VOICE_PORT", "18097"))
SR = 24000
VOICE = os.environ.get("KOKORO_VOICE", "af_heart")
CFG = os.path.join(os.path.dirname(os.path.abspath(__file__)), "voice.json")
_device = None          # resolved once at startup: a name match, never a bare index

_pipe = None
_ready = threading.Event()
_last_use = [0.0]
_rate = {"device": "unloaded", "load_s": None}
_lock = threading.Lock()
_speak_lock = threading.Lock()


def clean(t: str) -> str:
    t = re.sub(r"```[\s\S]*?```", " ", t)
    t = re.sub(r"`([^`]+)`", r"\1", t)
    t = re.sub(r"!\[[^\]]*\]\([^)]*\)", " ", t)
    t = re.sub(r"\[([^\]]+)\]\([^)]*\)", r"\1", t)
    t = re.sub(r"^#{1,6}\s+", "", t, flags=re.M)
    t = re.sub(r"[*_]{1,3}([^*_]+)[*_]{1,3}", r"\1", t)
    t = re.sub(r"^\s*[-*+]\s+", "", t, flags=re.M)
    t = re.sub(r"\s+", " ", t).strip()
    return t


def pick_device():
    """Windows renumbers its endpoints between boots, so match the NAME, not the index.
    voice.json may pin `device_name`; otherwise the system default output is used."""
    try:
        name = ""
        try:
            with open(CFG, encoding="utf-8") as f:
                name = json.load(f).get("device_name") or name
        except Exception:
            pass
        import sounddevice as sd
        for i, d in enumerate(sd.query_devices()):
            if d["max_output_channels"] > 0 and name.lower() in d["name"].lower():
                print(f"[voice] device {i}: {d['name']}", flush=True)
                return i
        print(f"[voice] no device matching {name!r}; using system default", flush=True)
    except Exception as e:
        print(f"[voice] device pick failed {type(e).__name__}: {e}", flush=True)
    return None


# en_US-libritts_r-medium is CC BY 4.0. en_US-ryan-high is CC BY-NC-SA 4.0 (non-commercial),
# so it cannot be the default in an MIT program someone may use at work. LibriTTS-R keeps the
# American accent of that old default, at medium size so the first download stays small.
# en_GB-cori-high is public domain, but it is British. ar_JO-kareem-medium stays: Piper
# publishes no other Arabic voice.
PIPER_VOICES = {"ar": "ar_JO-kareem-medium", "en": "en_US-libritts_r-medium"}
# rhasspy/piper-voices at this commit. Pinned so a moving branch cannot swap the files.
PIPER_REV = "b4e358ffcb232591bf73a540e70f7de528e4174a"


def _voice_cfg() -> dict:
    try:
        with open(CFG, encoding="utf-8") as f:
            return json.load(f) or {}
    except Exception:
        return {}


def piper_output_rate(voices) -> int:
    """One playback rate for a joined line. Each voice keeps its own config.sample_rate.
    Play at the higher rate so a 16 kHz voice is not the clock for a 22.05 kHz one."""
    rates = []
    for voice in voices.values():
        if isinstance(voice, str):
            continue
        rate = getattr(getattr(voice, "config", None), "sample_rate", None)
        if rate:
            rates.append(int(rate))
    return max(rates) if rates else 22050


def _resample(audio, src_sr: int, dst_sr: int):
    """Linear resample with numpy only, so a mixed-rate join does not need scipy."""
    audio = np.asarray(audio, dtype=np.float32).reshape(-1)
    src_sr, dst_sr = int(src_sr), int(dst_sr)
    if audio.size == 0 or src_sr == dst_sr:
        return audio
    n_out = int(round(audio.size * float(dst_sr) / float(src_sr)))
    if n_out <= 0:
        return np.zeros(0, dtype=np.float32)
    if audio.size == 1:
        return np.full(n_out, float(audio[0]), dtype=np.float32)
    x = np.linspace(0.0, audio.size - 1, n_out)
    xp = np.arange(audio.size, dtype=np.float64)
    return np.interp(x, xp, audio).astype(np.float32)


def warm_piper(cfg: dict):
    """Piper: small local voices with Arabic (Kokoro has none). One voice per script, so a
    line that mixes Arabic and English is read by both, each in its own language."""
    global _pipe, SR
    from huggingface_hub import hf_hub_download
    from piper import PiperVoice
    t0 = time.time()
    _pipe = {}
    for lang, default in PIPER_VOICES.items():
        name = cfg.get(f"voice_{lang}") or default                 # e.g. en_US-libritts_r-medium
        if name.startswith("apple:"):                              # e.g. apple:Majed
            best = apple_voice(name[6:])
            if best:
                _pipe[lang] = best
                continue
            print(f"[voice] no Premium/Enhanced Apple voice {name[6:]!r} installed "
                  f"(System Settings > Accessibility > Spoken Content) -> {default}", flush=True)
            name = default
        loc, _, quality = name.split("-")
        path = f"{loc.split('_')[0]}/{loc}/{name.split('-')[1]}/{quality}/{name}"
        onnx = hf_hub_download("rhasspy/piper-voices", path + ".onnx", revision=PIPER_REV)
        hf_hub_download("rhasspy/piper-voices", path + ".onnx.json", revision=PIPER_REV)
        _pipe[lang] = PiperVoice.load(onnx)
    SR = piper_output_rate(_pipe)
    _rate["device"], _rate["load_s"] = "cpu (piper)", round(time.time() - t0, 1)
    _rate["voice"] = "/".join(v if isinstance(v, str) else "piper" for v in _pipe.values())
    _ready.set()
    print(f"[voice] ready in {time.time()-t0:.1f}s: " + ", ".join(
        f"{k}={v if isinstance(v, str) else 'piper:' + (cfg.get(f'voice_{k}') or PIPER_VOICES[k])}"
        for k, v in _pipe.items()), flush=True)


def apple_voice(base: str):
    """The best installed macOS voice called `base`: Premium, then Enhanced. Never the compact
    one: that is the robotic voice this option exists to replace."""
    import subprocess
    have = subprocess.run(["say", "-v", "?"], capture_output=True, text=True).stdout
    for tier in ("Premium", "Enhanced"):
        if f"{base} ({tier})" in have:
            return f"{base} ({tier})"
    return None


def synth_apple(voice: str, text: str):
    """macOS `say` straight to a float WAV at the engine's rate, then read back for playback."""
    import subprocess
    import tempfile
    import soundfile as sf
    with tempfile.NamedTemporaryFile(suffix=".wav") as f:
        subprocess.run(["say", "-v", voice, "-o", f.name, "--file-format=WAVE",
                        f"--data-format=LEF32@{SR}", text], check=True)
        audio, _ = sf.read(f.name, dtype="float32")
    return audio if audio.ndim == 1 else audio.mean(axis=1)


def _runs(text: str):
    """Split a line into same-script runs: [("ar", "يا هلا"), ("en", "Sam"), ...].
    Digits and punctuation stay with the run they sit in."""
    runs = []
    for word in text.split():
        # letters decide, not punctuation: "Sam،" (Arabic comma) is still English
        lang = ("ar" if re.search(r"[ء-غف-يٱ-ۓ]", word) else
                "en" if re.search(r"[A-Za-z]", word) else (runs[-1][0] if runs else "en"))
        if runs and runs[-1][0] == lang:
            runs[-1][1].append(word)
        else:
            runs.append((lang, [word]))
    return [(lang, " ".join(words)) for lang, words in runs]


def synth_piper(text: str):
    """Join same-script runs onto one output rate. Each Piper voice keeps its native rate."""
    global SR
    SR = piper_output_rate(_pipe)
    gap = np.zeros(int(SR * 0.12), dtype=np.float32)     # a breath between language switches
    chunks = []
    for lang, run in _runs(text):
        voice = _pipe[lang]
        if isinstance(voice, str):                                  # an Apple voice by name
            pieces = [synth_apple(voice, run)]                      # already rendered at SR
            src = SR
        else:
            src = int(getattr(getattr(voice, "config", None), "sample_rate", SR) or SR)
            pieces = [c.audio_int16_array.astype(np.float32) / 32768.0 for c in voice.synthesize(run)]
        if pieces:
            chunks += [_resample(piece, src, SR) for piece in pieces] + [gap]
    return np.concatenate(chunks) if chunks else None


def warm():
    global _pipe
    cfg = _voice_cfg()
    if str(cfg.get("engine") or "kokoro").lower() == "piper":
        try:
            warm_piper(cfg)
        except Exception as e:
            print(f"[voice] piper FAILED {type(e).__name__}: {e}", flush=True)
        return
    t0 = time.time()
    try:
        from kokoro import KPipeline
        import json as _json
        dev = "cpu"
        try:
            dev = _json.load(open(CFG, encoding="utf-8")).get("device") or "cpu"
        except Exception:
            pass
        BUSY = os.path.join(os.path.dirname(os.path.abspath(__file__)), "gpu_busy")
        if os.path.exists(BUSY):
            print("[voice] GPU busy with another job -> CPU", flush=True)
            dev = "cpu"
        kw = {} if dev == "cpu" else {"device": dev}
        _pipe = KPipeline(lang_code="a", **kw)
        if dev == "cpu":
            try:
                _pipe.model = _pipe.model.to("cpu")
            except Exception:
                pass
        try:
            dev = str(next(_pipe.model.parameters()).device)
        except Exception:
            dev = "?"
        _rate["device"] = dev
        _rate["load_s"] = round(time.time() - t0, 1)
        _ready.set()
        print(f"[voice] kokoro ready in {time.time()-t0:.1f}s voice={VOICE} device={dev}", flush=True)
    except Exception as e:
        print(f"[voice] kokoro FAILED {type(e).__name__}: {e}", flush=True)


CHUNK_CHARS = 320
MAX_CHARS = 20000


def _split(text: str, n: int = CHUNK_CHARS):
    """Sentence-grouped chunks: Kokoro reads a whole text, but one huge call spikes memory,
    and splitting on sentence ends keeps the prosody sane across the seam."""
    parts = [p for p in re.split(r"(?<=[.!?\u061F\u06D4\u2026])\s+", text) if p.strip()]
    out, cur = [], ""
    for p in parts:
        if len(cur) + len(p) + 1 <= n:
            cur = (cur + " " + p).strip()
        else:
            if cur:
                out.append(cur)
            cur = p
    if cur:
        out.append(cur)
    return out or [text]


def synth(text: str):
    if isinstance(_pipe, dict):                       # piper: one voice per language
        return synth_piper(text)
    chunks = []
    for piece in _split(text):
        chunks += [r.audio.numpy() for r in _pipe(piece, voice=VOICE)]
    if not chunks:
        return None
    return np.concatenate(chunks)


def play(audio, device=None):
    import sounddevice as sd
    try:
        sd.stop()
        sd.play(audio, SR, device=device)   # Kokoro is native 24 kHz; Piper chunks are resampled to SR
    except Exception as e:
        print(f"[voice] play failed {type(e).__name__}: {e}", flush=True)


def say(text: str, device=None) -> dict:
    text = clean(text)
    if not text:
        return {"ok": False, "why": "empty"}
    text = text[:MAX_CHARS]
    _last_use[0] = time.time()
    if not _ready.is_set():
        _ready.wait(60)
    if device is None:
        device = _device
    t0 = time.time()
    with _speak_lock:
        audio = synth(text)
        ms = int((time.time() - t0) * 1000)
        if audio is None:
            return {"ok": False, "why": "no audio", "ms": ms}
        threading.Thread(target=play, args=(audio, device), daemon=True).start()
    return {"ok": True, "ms": ms, "dur_s": round(len(audio) / SR, 2), "chars": len(text)}


from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


class H(BaseHTTPRequestHandler):
    def _json(self, code, obj):
        body = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("content-type", "application/json")
        self.send_header("content-length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.path.startswith("/health"):
            try:
                import psutil
                free_gb = round(psutil.virtual_memory().available / 1073741824, 1)
            except Exception:
                free_gb = None
            return self._json(200, {"ok": True, "ready": _ready.is_set(), "voice": _rate.get("voice") or VOICE,
                                    "device": _rate["device"], "load_s": _rate["load_s"],
                                    "idle_s": round(time.time() - _last_use[0], 1) if _last_use[0] else None,
                                    "free_gb": free_gb})
        if self.path.startswith("/devices"):
            try:
                import sounddevice as sd
                outs = [{"i": i, "name": d["name"], "out": d["max_output_channels"]}
                        for i, d in enumerate(sd.query_devices()) if d["max_output_channels"] > 0]
                return self._json(200, {"ok": True, "default": list(sd.default.device), "devices": outs})
            except Exception as e:
                return self._json(500, {"ok": False, "why": str(e)})
        self._json(404, {"ok": False})

    def do_POST(self):
        n = int(self.headers.get("content-length") or 0)
        try:
            payload = json.loads(self.rfile.read(n) or b"{}")
        except Exception:
            payload = {}
        if self.path.startswith("/say"):
            try:
                dev = payload.get("device")
                return self._json(200, say(str(payload.get("text") or ""), dev))
            except Exception as e:
                return self._json(500, {"ok": False, "why": f"{type(e).__name__}: {e}"})
        if self.path.startswith("/stop"):
            try:
                import sounddevice as sd
                sd.stop()
            except Exception:
                pass
            return self._json(200, {"ok": True})
        self._json(404, {"ok": False})

    def log_message(self, *a):
        pass


if __name__ == "__main__":
    _device = pick_device()
    threading.Thread(target=warm, daemon=True).start()
    srv = ThreadingHTTPServer(("127.0.0.1", PORT), H)
    print(f"[voice] listening on 127.0.0.1:{PORT}", flush=True)
    srv.serve_forever()
