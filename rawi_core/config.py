"""config.json: the defaults, and how a user's file is layered on top of them.

Three functions cover every caller:

    load()        defaults + the user's file, merged; creates the file on first run (the daemon)
    read_user()   only what the user's file says, no defaults (the hub edits exactly that)
    save()        write a config back, keeping Arabic readable (ensure_ascii=False)

Nested sections (`listen`, `dictionary`, `clean`) are merged key by key. Before this, a file
holding only `"clean": {"enabled": true}` replaced the whole default section, lost `system` and
`endpoint`, and the cleaner failed with a silent KeyError on every dictation.
"""
from __future__ import annotations

import copy
import json
import os

from . import paths

DEFAULTS: dict = {
    "hold_key": "ctrl_l",
    "mode": "hold",            # hold | toggle | latch (latch behaves like toggle)
    "model": "large-v3-turbo",
    "compute_type": "float16",
    "device": "auto",          # auto = cuda when a CUDA device is present, else cpu int8
    "language": "auto",
    "paste": True,
    "restore_clipboard": True,
    "beeps": True,
    "log": False,
    "max_seconds": 180,
    "dumb": False,
    "mood": "auto",            # auto = tag each dictation with one earned emoji; off = never
    "talk": False,             # voice mode: speak the transcript aloud instead of pasting it
    "paste_key": "f9",
    "lang_key": "f10",
    "talk_key": "f7",
    "lang_lock": "auto",
    "listen": {"key": "f8", "silence_ms": 700, "min_s": 0.45, "max_s": 25, "paste": False},
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


def merged(user: dict | None) -> dict:
    """Defaults with the user's values on top. Dict-valued sections merge one level deep.

    >>> merged({"clean": {"enabled": True}})["clean"]["endpoint"]
    'http://127.0.0.1:18080/v1/chat/completions'
    """
    cfg = copy.deepcopy(DEFAULTS)            # never hand out the module's own nested dicts
    for key, value in (user or {}).items():
        if isinstance(cfg.get(key), dict) and isinstance(value, dict):
            cfg[key] = {**cfg[key], **value}
        else:
            cfg[key] = value
    return cfg


def read_user(path: str = paths.CONFIG) -> dict:
    """Only what the user's file says (no defaults). {} when missing or unreadable."""
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def load(path: str = paths.CONFIG, create: bool = True) -> dict:
    """The effective config: defaults + the user's file.

    create=True writes the defaults to `path` when it does not exist yet (first run).
    An unreadable file is reported and ignored, never overwritten: it may hold the user's work.
    """
    if not os.path.exists(path):
        if create:
            save(DEFAULTS, path)
        return merged({})
    try:
        with open(path, encoding="utf-8") as f:
            return merged(json.load(f))
    except Exception as e:
        print(f"[rawi] config unreadable ({e}); using defaults", flush=True)
        return merged({})


def save(cfg: dict, path: str = paths.CONFIG) -> bool:
    """Write `cfg` as pretty JSON. Returns False (and says why) instead of raising."""
    try:
        with open(path, "w", encoding="utf-8") as f:
            json.dump(cfg, f, indent=2, ensure_ascii=False)
        return True
    except Exception as e:
        print(f"[rawi] could not save config ({e})", flush=True)
        return False
