"""Rawi Hub — a real window for the dictation daemon (pywebview: WebView2 on Windows,
WKWebView on macOS) plus a tray icon where the OS allows one.

    python rawi_app.py          (Windows: rawi-app.bat starts it without a console)

The hub is a thin client. It renders app.html, answers the page's `pywebview.api.*` calls
through `Api`, reads history and config from disk, and talks to the daemon over loopback IPC.
Opening it (re)starts the daemon; closing the window never stops dictation.
"""
from __future__ import annotations

import datetime as dt
import json
import os
import sys
import threading
import time
import urllib.request

from rawi_core import analytics, config, history, ipc, paths
from rawi_core.gpu import gate_requires_yield, gpu_gate
from rawi_core.system import current as current_platform

PLAT = current_platform()
CLEANER_HEALTH = "http://127.0.0.1:18080/health"
WINDOW_TITLE = "Rawi"          # rawi_dock.py finds the hub window by this exact title


# ---------------------------------------------------------------- the daemon process
def daemon_pids() -> list[int]:
    """The daemon's pid from its pid file, if that process is alive."""
    try:
        with open(paths.DAEMON_PID, encoding="utf-8") as f:
            pid = int(f.read().strip())
    except Exception:
        return []
    return [pid] if PLAT.pid_alive(pid) else []


def cleaner_up() -> bool:
    """True when the optional cleaner server answers its health check."""
    try:
        with urllib.request.urlopen(CLEANER_HEALTH, timeout=1.2) as r:
            return r.status == 200
    except Exception:
        return False


def ensure_cleaner() -> str:
    """Start the cleaner if 18080 is dark, the GPU is free, and a launcher actually exists
    (cleaner-server.bat on Windows, cleaner-server.sh elsewhere, next to rawi.py)."""
    if cleaner_up():
        return "already up"
    holder = gpu_gate()
    if holder:
        return "skipped: gpu busy with %s" % holder.get("owner", "?")
    cmd = PLAT.cleaner_command(paths.ROOT)
    if cmd is None:
        return "no %s in this folder" % PLAT.cleaner_script   # nothing to start; say so
    try:
        PLAT.spawn(cmd, cwd=paths.ROOT, new_group=True)
        return "starting"
    except Exception as e:
        return "failed: %s" % e


def status_block() -> dict:
    """The live line under the Usage tab: daemon, cleaner, GPU, ear."""
    holder = gpu_gate()
    live = ipc.call("status", timeout=1.5)
    return {"daemon": bool(daemon_pids()), "cleaner": cleaner_up(),
            "gpu": (holder or {}).get("owner"), "busy": gate_requires_yield(holder),
            "listening": bool(live.get("listening")), "dropped": int(live.get("dropped") or 0)}


# ---------------------------------------------------------------- the page's API
class Api:
    """Everything app.html can call as `await pywebview.api.<name>(...)`.

    Analytics are computed by rawi_core.analytics from history rows; this class only adds the
    live bits (daemon status, config file, processes) and must keep its replies' shapes.
    """

    # ---- reading ----
    def snapshot(self):
        snap = analytics.snapshot(history.load_rows(tag_mood=True), dt.datetime.now())
        snap["status"] = status_block()
        return snap

    def voice(self):
        return analytics.voice_report(history.load_rows(tag_mood=True), dt.datetime.now())

    def galaxy(self, days=0, limit=6000):
        return analytics.galaxy(history.load_rows(tag_mood=True), dt.datetime.now(), days, limit)

    def room(self):
        return analytics.room(history.load_rows(tag_mood=True), dt.date.today())

    def style_samples(self, limit=3):
        return analytics.style_samples(history.load_rows(tag_mood=True), limit)

    def history(self, q="", limit=200):
        return analytics.search(history.load_rows(tag_mood=True), q, limit)

    # ---- the daemon ----
    def listen(self, on=None):
        """Open or close the ear. on=None flips it."""
        return ipc.call({"cmd": "listen"} if on is None else {"cmd": "listen", "on": bool(on)})

    def stop_daemon(self):
        """Stop the Whisper daemon for real.

        Closing the Hub window only hides it to the tray (by design: the hotkeys must keep
        working), so for a year 'Quit' left rawi.py alive with ~4.6 GB of Whisper committed.
        Quit must be the thing that gives the memory back."""
        stopped = []
        for pid in daemon_pids():
            PLAT.kill(pid)
            stopped.append(pid)
        try:
            os.remove(paths.DAEMON_PID)
        except Exception:
            pass
        return stopped

    def restart_daemon(self):
        """Stop the daemon if it runs, start a fresh one, and report how many are alive."""
        for pid in daemon_pids():
            PLAT.kill(pid)
        time.sleep(1.2)
        PLAT.spawn([PLAT.background_python(), paths.DAEMON_SCRIPT], log_path=paths.DAEMON_SPAWN_LOG,
                   cwd=paths.ROOT, detached=True, new_group=True)
        time.sleep(2.0)
        return len(daemon_pids())

    # ---- settings ----
    def get_config(self):
        """The Settings / Dictionary / Style tabs' view of config.json (defaults filled in)."""
        user = config.read_user(paths.CONFIG)
        c = config.merged(user)
        cl = c.get("clean") or {}
        d = c.get("dictionary") or {}
        ls = c.get("listen") or {}
        return {"ok": True,
                "hold_key": c.get("hold_key"),
                "keys": {"paste": c.get("paste_key"), "lang": c.get("lang_key"),
                         "talk": c.get("talk_key"), "listen": ls.get("key")},
                "log": bool(c.get("log")),
                "clean": bool(cl.get("enabled")),
                "level": cl.get("level", "light"),
                "tone": cl.get("tone", "default"),
                "language": c.get("language"),
                "model": c.get("model"),
                "talk": bool(c.get("talk")),
                "mood": c.get("mood", "auto"),
                "names": d.get("names") or [],
                "pairs": d.get("pairs") or [],
                "profiles": c.get("profiles") or [],
                "snippets": c.get("snippets") or {}}

    def save_config(self, patch):
        """Apply only the keys the page sent, write config.json, restart the daemon.

        Partial patches are normal (the Mode switch sends only {"talk": ...}), so a key that is
        absent from the patch must leave the file alone — before, every save without "clean"
        switched the cleaner on. Replies are {"ok", "message"}; the page shows the message.
        """
        try:                       # an unreadable file is left alone: it may hold the user's work
            with open(paths.CONFIG, encoding="utf-8") as f:
                c = json.load(f)
        except Exception:
            return {"ok": False, "message": "Could not read config.json."}
        for key in ("hold_key", "language", "model"):
            if str(patch.get(key) or "").strip():   # an empty value would silently kill dictation
                c[key] = patch[key]
        if "log" in patch:
            c["log"] = bool(patch["log"])
        cl = c.setdefault("clean", {})
        if "clean" in patch:
            cl["enabled"] = bool(patch["clean"])
        if patch.get("level") in ("none", "light", "full"):
            cl["level"] = patch["level"]
        if patch.get("tone"):
            cl["tone"] = str(patch["tone"])
        if isinstance(patch.get("profiles"), list):
            c["profiles"] = patch["profiles"]
        if "talk" in patch:
            c["talk"] = bool(patch.get("talk"))
        if patch.get("mood") in ("auto", "off"):
            c["mood"] = patch["mood"]
        if isinstance(patch.get("snippets"), dict):
            c["snippets"] = {str(k): str(v) for k, v in patch["snippets"].items() if str(k).strip()}
        if "names" in patch or "pairs" in patch:
            d = c.setdefault("dictionary", {})
            d["names"] = [str(x) for x in (patch.get("names") or [])]
            d["pairs"] = [str(x) for x in (patch.get("pairs") or [])]
        if not config.save(c, paths.CONFIG):
            return {"ok": False, "message": "Could not write config.json."}
        n = self.restart_daemon()
        return {"ok": True, "message": ("Saved. rawi restarted and is loading the model." if n
                                        else "Saved, but rawi did not start again. Run %s to see why."
                                        % PLAT.launcher_hint)}

    # ---- small utilities for the page ----
    def copy(self, text):
        try:
            import pyperclip
            pyperclip.copy(str(text))
            return "copied"
        except Exception as e:
            return "copy failed: %s" % e

    def log(self, msg):
        with open(paths.HUB_DEBUG_LOG, "a", encoding="utf-8") as f:
            f.write("%s %s\n" % (dt.datetime.now().strftime("%H:%M:%S"), msg))
        return "ok"

    def open_folder(self):
        return "opened" if PLAT.open_path(paths.ROOT) else "could not open the folder"


# ---------------------------------------------------------------- tray
_ICON = [None]   # the tray icon, once it runs, so closing the window can speak through it
# Decided before the window opens, so a close in the tray's first second is not a real quit:
# True when this OS supports a tray and pystray is installed. Set back to False if it fails.
_TRAY_EXPECTED = [False]


def tray_expected() -> bool:
    """Will a tray icon appear? (supported OS + pystray importable)"""
    import importlib.util
    try:
        return PLAT.tray_supported and importlib.util.find_spec("pystray") is not None
    except Exception:
        return False


def _tray(win, api):
    """A tray icon with Open / Restart daemon / Start cleaner / Quit. Needs pystray (Windows)."""
    import pystray
    from PIL import Image

    img = Image.open(paths.ICON_PNG)

    def on_open(icon, item):
        try:
            win.show()
        except Exception:
            pass

    def on_restart(icon, item):
        api.restart_daemon()

    def on_cleaner(icon, item):
        ensure_cleaner()

    def on_quit(icon, item):
        try:
            api.stop_daemon()
        except Exception:
            pass
        icon.stop()
        try:
            win.destroy()
        except Exception:
            os._exit(0)

    menu = pystray.Menu(
        pystray.MenuItem("Open Rawi", on_open, default=True),
        pystray.MenuItem("Restart daemon", on_restart),
        pystray.MenuItem("Start cleaner", on_cleaner),
        pystray.MenuItem("Quit", on_quit),
    )
    icon = pystray.Icon("rawi", img, "Rawi - listening", menu)
    _ICON[0] = icon
    threading.Thread(target=icon.run, daemon=True).start()
    return icon


def _start_tray_later(win, api):
    """Start the tray a second after the window, where expected. Fails open: without a tray,
    closing the window really closes it, and a window already hidden comes back."""
    if not _TRAY_EXPECTED[0]:
        return
    time.sleep(1.0)
    try:
        _tray(win, api)
    except Exception as e:
        _TRAY_EXPECTED[0] = False
        print(f"[hub] no tray icon ({type(e).__name__}: {e}); closing the window will quit the hub",
              flush=True)
        try:
            win.show()
        except Exception:
            pass


# ---------------------------------------------------------------- main
def main():
    import webview

    api = Api()
    ensure_cleaner()
    # tests boot the window without loading Whisper: a marker file, because WSL does not pass
    # ordinary env vars to Windows processes (only WSLENV does)
    if not os.path.exists(paths.NO_DAEMON_MARKER):
        api.restart_daemon()
    with open(paths.HUB_PAGE, encoding="utf-8") as f:
        html = f.read()
    win = webview.create_window(WINDOW_TITLE, html=html, width=1080, height=780,
                                min_size=(880, 600), background_color="#f4ecdd",
                                js_api=api)

    def on_closing():
        # With a tray icon, closing the window sends it to the tray and Quit from the tray
        # really exits. Without one (macOS, or pystray missing) the window really closes;
        # either way the daemon keeps listening, which the message says out loud.
        if not _TRAY_EXPECTED[0]:
            return True
        try:
            win.hide()
        except Exception:
            pass
        if _ICON[0] is not None:            # still None in the tray's first second
            try:
                _ICON[0].title = "Rawi - still listening (F8). Quit here to stop."
                _ICON[0].notify("Rawi is still listening - the hotkeys work. "
                                "Quit from this tray icon to stop it.", "Rawi")
            except Exception:
                pass
        return False

    win.events.closing += on_closing
    _TRAY_EXPECTED[0] = tray_expected()
    threading.Thread(target=_start_tray_later, args=(win, api), daemon=True).start()
    webview.start(debug=False)


if __name__ == "__main__":
    sys.exit(main())
