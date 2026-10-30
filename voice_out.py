#!/usr/bin/env python3
"""voice_out.py — a resident local TTS server. Kokoro loaded once, spoken on demand.

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


def warm():
    global _pipe
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
        sd.play(audio, SR, device=device)
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
            return self._json(200, {"ok": True, "ready": _ready.is_set(), "voice": VOICE,
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
