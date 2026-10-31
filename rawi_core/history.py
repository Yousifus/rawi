"""transcripts.jsonl: one JSON object per dictation, written only when `log` is true.

    append(entry, on=True)       add one line (never raises)
    load_rows()                  every readable row, with a parsed `_dt` datetime added
    words(t) / fmt_mins(s)       the small counters the hub and stats.html both show
    app_label(title)             "budget.xlsx - Excel" -> "Excel"
    day_streak(days, today)      consecutive days with speech, ending today (or yesterday)

A row looks like (see rawi.Rawi._log_record):
    {"ts": "2026-10-30T14:02:11", "seconds": 3.4, "language": "ar", "model": "large-v3-turbo",
     "transcribe_s": 0.41, "clean_s": 0.0, "clean": "off", "text": "...", "raw": "...",
     "app": "Slack", "fid": {"d": 0.0, "ins": 0, "lvl": "same"}, "mood": {...} | null,
     "to": "" | "voice", "listen": false}
"""
from __future__ import annotations

import datetime as dt
import json
import os
from typing import Container

from . import paths
from . import mood as mood_mod


def append(entry: dict, on: bool = True, path: str = paths.TRANSCRIPTS) -> None:
    """Add one row. `on=False` is a no-op, so callers can pass the config flag straight in."""
    if not on:
        return
    try:
        with open(path, "a", encoding="utf-8") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")
    except Exception:
        pass


def load_rows(path: str = paths.TRANSCRIPTS, *, tag_mood: bool = False) -> list[dict]:
    """Every row that parses and has an ISO `ts`, in file order, each with `_dt` added.

    tag_mood=True fills in `mood` for older rows written before moods existed.
    """
    rows: list[dict] = []
    if not os.path.exists(path):
        return rows
    with open(path, encoding="utf-8", errors="replace") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                r = json.loads(line)
                r["_dt"] = dt.datetime.fromisoformat(r["ts"])
            except Exception:
                continue                    # a torn or foreign line never breaks the hub
            if tag_mood and not r.get("mood"):
                try:
                    r["mood"] = mood_mod.tag(r.get("text"))
                except Exception:
                    r["mood"] = None
            rows.append(r)
    return rows


def words(text: str | None) -> int:
    """Whitespace-separated word count."""
    return len((text or "").split())


def fmt_mins(sec: float) -> str:
    """Seconds as "4.2 min" (or "1.5 h" past an hour)."""
    m = sec / 60.0
    return "%.1f min" % m if m < 60 else "%.1f h" % (m / 60)


def app_label(title: str | None) -> str:
    """'budget.xlsx - Excel' -> 'Excel'; a bare title stays as it is (max 28 chars)."""
    t = (title or "").strip()
    if not t:
        return ""
    for sep in (" - ", " — ", " – "):
        if sep in t:
            t = t.rsplit(sep, 1)[1]
    t = t.replace(" — Google Chrome", "").replace(" - Google Chrome", "")
    return t.strip()[:28]


def day_streak(days: Container[dt.date], today: dt.date, limit: int = 400) -> int:
    """Consecutive days in `days` ending today. A quiet *today* does not break a streak that
    ran through yesterday (the day is not over yet)."""
    streak = 0
    d = today
    for _ in range(limit):
        if d in days:
            streak += 1
            d -= dt.timedelta(days=1)
        elif streak == 0 and (d - dt.timedelta(days=1)) in days:
            d -= dt.timedelta(days=1)
        else:
            break
    return streak
