"""Every file rawi reads or writes, named once.

All of them sit next to `rawi.py` (the repository root), which is how rawi has always worked:
the folder you cloned is the app's home. Nothing here creates files; it only names them.
"""
from __future__ import annotations

import os

#: the folder that holds rawi.py, config.json and everything below
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# ---- settings ----
CONFIG = os.path.join(ROOT, "config.json")          # user settings, created on first run
VOICE_CONFIG = os.path.join(ROOT, "voice.json")     # optional voice-mode settings
DOCK_POSITION = os.path.join(ROOT, "dock.json")     # where the dock was last dropped

# ---- data ----
TRANSCRIPTS = os.path.join(ROOT, "transcripts.jsonl")   # history, only when `log` is true

# ---- single-instance guards ----
DAEMON_PID = os.path.join(ROOT, "daemon.pid")
DOCK_PID = os.path.join(ROOT, "dock.pid")

# ---- logs (all git-ignored) ----
DAEMON_LOG = os.path.join(ROOT, "rawi.log")         # the daemon's prints when it has no console
DAEMON_SPAWN_LOG = os.path.join(ROOT, "dict.log")   # the daemon's output when the hub starts it
DOCK_LOG = os.path.join(ROOT, "docker.log")         # the dock's output
VOICE_LOG = os.path.join(ROOT, "voiceout.log")      # the voice server's output
HUB_DEBUG_LOG = os.path.join(ROOT, "app_debug.log")  # messages from app.html via Api.log

# ---- scripts and assets ----
DAEMON_SCRIPT = os.path.join(ROOT, "rawi.py")
HUB_SCRIPT = os.path.join(ROOT, "rawi_app.py")
DOCK_SCRIPT = os.path.join(ROOT, "rawi_dock.py")
MENUBAR_SCRIPT = os.path.join(ROOT, "rawi_menubar.py")   # macOS, in place of the dock
VOICE_SCRIPT = os.path.join(ROOT, "voice_out.py")
VOICE_LAUNCHER = os.path.join(ROOT, "launch_voice.py")
HUB_PAGE = os.path.join(ROOT, "app.html")
STATS_TEMPLATE = os.path.join(ROOT, "template.html")
STATS_PAGE = os.path.join(ROOT, "stats.html")
ICON_PNG = os.path.join(ROOT, "icon.png")

#: test hook: when this file exists the hub opens without (re)starting the daemon
NO_DAEMON_MARKER = os.path.join(ROOT, "_no_daemon")
