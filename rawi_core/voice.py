"""Client for the optional local voice server (voice_out.py), used by voice mode (F7).

The server keeps a TTS model warm so a line is spoken in about a second. Everything here is
best-effort: speech must never break dictation, so every function returns instead of raising.

    settings()         voice.json as a dict ({} when absent)
    post(path, obj)    POST JSON to the server; True on a 2xx reply
    ensure_running()   start the server if voice.json says autostart and it is not up yet
"""
from __future__ import annotations

import json
import os
import urllib.request

from . import paths
from .system import current

HOST = "127.0.0.1"
PORT = int(os.environ.get("RAWI_VOICE_PORT", "18097"))


def settings() -> dict:
    """voice.json: optional voice-mode settings (voice, device, device_name, autostart)."""
    try:
        with open(paths.VOICE_CONFIG, encoding="utf-8") as f:
            return json.load(f) or {}
    except Exception:
        return {}


def post(path: str, obj: dict, timeout: float = 1.5) -> bool:
    """POST `obj` to the voice server at `path` ("/say", "/mode", "/stop")."""
    try:
        data = json.dumps(obj).encode("utf-8")
        req = urllib.request.Request(f"http://{HOST}:{PORT}{path}", data=data,
                                     headers={"content-type": "application/json"})
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return 200 <= r.status < 300
    except Exception as e:
        print(f"[rawi] voice server unreachable ({type(e).__name__})", flush=True)
        return False


def health(timeout: float = 1.5) -> dict | None:
    """The server's /health reply, or None when it is not answering."""
    try:
        with urllib.request.urlopen(f"http://{HOST}:{PORT}/health", timeout=timeout) as r:
            return json.loads(r.read().decode())
    except Exception:
        return None


def ensure_running() -> bool:
    """Keep the voice server warm: it loads its model once, then speaks in a fraction of a second.

    Returns True when it is already answering; otherwise starts it (if autostart) and returns False.
    """
    if not bool(settings().get("autostart")):
        return False
    h = health()
    if h is not None:
        print(f"[rawi] voice ready={h.get('ready')}", flush=True)
        return True
    plat = current()
    try:
        p = plat.spawn([plat.background_python(), paths.VOICE_LAUNCHER], cwd=paths.ROOT)
        print(f"[rawi] voice server cold-start -> pid {p.pid}", flush=True)
    except Exception as e:
        print(f"[rawi] voice server failed to start: {type(e).__name__}: {e}", flush=True)
    return False
