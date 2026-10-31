"""The numbers behind the hub's tabs and stats.html, computed from history rows.

Every function is pure: rows in (from history.load_rows), a JSON-ready dict out, and the clock
passed in as `now` / `today`, so the same history always gives the same answer and tests can
build rows by hand. The output shapes are exactly what app.html reads; change both together.

    summary(rows, today)          shared totals: per day, per hour, languages, latency, streak
    snapshot(rows, now)           Usage tab (the hub adds a live "status" block)
    voice_report(rows, now)       Voice tab: pace, top words, the cleaner's frequent fixes
    galaxy(rows, now, days)       the sky: every dictation as a star
    room(rows, today)             Room tab: today's sentences and one honest gauge
    style_samples(rows, limit)    Style tab: raw vs cleaned pairs
    search(rows, q, limit)        History tab
"""
from __future__ import annotations

import datetime as dt
import difflib
import re
import statistics
from collections import Counter, defaultdict

from .history import app_label, day_streak, fmt_mins, words

_TOKEN_RE = re.compile(r"[\w؀-ۿ']+")
_AR_RE = re.compile(r"[؀-ۿ]")
_EN_RE = re.compile(r"[A-Za-z]")

# words too common to say anything about a voice, in both languages
STOPWORDS = set("""a an the and or but if then that this these those i you he she it we they me my your his her
its our their is are was were be been being am do does did doing have has had having to of in on at for with
from by as so not no yes just very really about into over after before when while what which who whom how why
can could would should will shall may might must im ive id ill youre dont doesnt didnt cant wont isnt wasnt
and-or than too also there here now out up down all any some more most other such only own same s t d ll re ve m
في من على عن الى إلى مع ان أن إن هذا هذه ذلك تلك هو هي هم انا أنا كنت كان كانت ما لا لم لن قد كل بعض غير بين
عند حتى لو ثم او أو يا اي أي شي شيء يعني بس لكن و انه أنه انها أنها كذا زي هذا huh um uh erm hmm yeah ok okay like just really actually maybe got get know
im i'm it's thats that's youre you're we're they're i've ive dont don't didnt didn't isnt isn't wasnt wasn't
cant can't wont won't whats what's theres there's lets let's gonna wanna sorta kinda""".split())


def _fmt(d: dt.datetime) -> str:
    return d.strftime("%d %b %H:%M")


# ---------------------------------------------------------------- shared totals
def summary(rows: list[dict], today: dt.date) -> dict:
    """Totals shared by the hub's Usage tab and stats.html.

    by_day    {date: {"n", "words", "sec", "lat": [transcribe seconds]}}
    per_day   {date: words}
    hours     Counter(hour -> dictations)
    langs     Counter(language -> dictations)
    today     {"n", "words", "sec"} for `today`
    words / seconds / count / latencies / streak   over all rows
    """
    by_day: dict = defaultdict(lambda: {"n": 0, "words": 0, "sec": 0.0, "lat": []})
    per_day: dict = defaultdict(int)
    hours: Counter = Counter()
    langs: Counter = Counter()
    for r in rows:
        d = r["_dt"].date()
        b = by_day[d]
        b["n"] += 1
        w = words(r.get("text"))
        b["words"] += w
        b["sec"] += float(r.get("seconds") or 0)
        if r.get("transcribe_s") is not None:
            b["lat"].append(float(r["transcribe_s"]))
        per_day[d] += w
        hours[r["_dt"].hour] += 1
        langs[str(r.get("language") or "?").lower()] += 1
    return {
        "by_day": by_day, "per_day": per_day, "hours": hours, "langs": langs,
        "today": by_day.get(today, {"n": 0, "words": 0, "sec": 0.0}),
        "words": sum(v["words"] for v in by_day.values()),
        "seconds": sum(v["sec"] for v in by_day.values()),
        "count": sum(v["n"] for v in by_day.values()),
        "latencies": [x for v in by_day.values() for x in v["lat"]],
        "streak": day_streak(per_day, today),
    }


def _row_view(r: dict) -> dict:
    """The fields History and the Usage 'recent' list both show."""
    return {"when": _fmt(r["_dt"]), "lang": r.get("language", "?"),
            "text": (r.get("text") or "").strip(), "app": app_label(r.get("app")),
            "clean": r.get("clean") or "", "fid": r.get("fid") or None,
            "raw": (r.get("raw") or "").strip()}


# ---------------------------------------------------------------- Usage tab
def snapshot(rows: list[dict], now: dt.datetime) -> dict:
    """Today, all-time totals, the 42-day chart, hours, languages, top apps, recent lines."""
    today = now.date()
    s = summary(rows, today)
    t, tw, ts, lats = s["today"], s["words"], s["seconds"], s["latencies"]
    days = [(today - dt.timedelta(days=i)) for i in range(41, -1, -1)]
    recent = [_row_view(r) for r in sorted(rows, key=lambda r: r["_dt"], reverse=True)[:12]]
    return {
        "when": now.strftime("%A %d %B %Y, %H:%M"),
        "today": {"words": t["words"], "time": fmt_mins(t["sec"]), "n": t["n"]},
        "all": {"words": tw,
                "wpm": "%.0f" % (tw / (ts / 60)) if ts else "0",
                "saved": "%.0f" % max(0.0, tw / 40.0 - ts / 60),
                "lat": "%.2f" % statistics.median(lats) if lats else "0",
                "rtf": "%.1f" % (ts / sum(lats)) if lats and sum(lats) else "0"},
        "streak": s["streak"],
        "days": [{"d": x.strftime("%d %b"), "words": s["per_day"].get(x, 0)} for x in days],
        "hours": [s["hours"].get(h, 0) for h in range(24)],
        "langs": s["langs"].most_common(),
        "apps": [{"name": k, "n": v} for k, v in
                 Counter(app_label(r.get("app")) for r in rows if r.get("app")).most_common(6)],
        "recent": recent,
    }


# ---------------------------------------------------------------- Voice tab
def voice_report(rows: list[dict], now: dt.datetime) -> dict:
    """Every number here is a query over the speaker's own log, computed now, and traceable."""
    rows = [r for r in rows if (r.get("text") or "").strip()]
    if not rows:
        return {"ok": True, "empty": True, "built": now.strftime("%H:%M:%S")}
    day = dt.timedelta(days=1)
    today = [r for r in rows if r["_dt"].date() == now.date()]
    week = [r for r in rows if now - r["_dt"] < 7 * day]
    prev = [r for r in rows if 7 * day <= now - r["_dt"] < 14 * day]

    def wpm(rs):
        secs = sum(float(r.get("seconds") or 0) for r in rs)
        w = sum(words(r.get("text")) for r in rs)
        return (w / (secs / 60.0)) if secs > 5 else 0.0

    def span(rs):
        return {"n": len(rs), "words": sum(words(r.get("text")) for r in rs), "wpm": round(wpm(rs), 1)}

    # the speaker's own vocabulary, with the latest line each word appeared in
    counts: dict[str, int] = {}
    for r in rows:
        for t in _TOKEN_RE.findall((r.get("text") or "").lower()):
            if len(t) < 3 or t in STOPWORDS or t.isdigit():
                continue
            counts[t] = counts.get(t, 0) + 1
    top = []
    for w, c in sorted(counts.items(), key=lambda kv: -kv[1])[:10]:
        sample = next((r for r in reversed(rows) if w in (r.get("text") or "").lower()), None)
        top.append({"w": w, "n": c, "when": _fmt(sample["_dt"]) if sample else "",
                    "line": (sample.get("text") or "")[:120] if sample else ""})

    # what the cleaner keeps changing: heard -> wrote, most frequent first
    fixes: dict = {}
    for r in rows:
        raw, cl = (r.get("raw") or ""), (r.get("text") or "")
        if not raw or not cl or raw.strip() == cl.strip():
            continue
        a = _TOKEN_RE.findall(raw.lower())
        b = _TOKEN_RE.findall(cl.lower())
        sm = difflib.SequenceMatcher(None, a, b, autojunk=False)
        for op, i1, i2, j1, j2 in sm.get_opcodes():
            if op == "replace" and (i2 - i1) and (j2 - j1):
                src = " ".join(a[i1:i2])[:40]
                dst = " ".join(b[j1:j2])[:40]
                if src != dst:
                    e = fixes.setdefault((src, dst), {"heard": src, "wrote": dst, "n": 0, "first": r["_dt"]})
                    e["n"] += 1
                    if r["_dt"] < e["first"]:
                        e["first"] = r["_dt"]
    fix_top = [{"heard": e["heard"], "wrote": e["wrote"], "n": e["n"], "first": e["first"].strftime("%d %b")}
               for e in sorted(fixes.values(), key=lambda x: -x["n"])[:8]]

    hours = [0] * 24
    for r in rows:
        hours[r["_dt"].hour] += 1
    peak = max(range(24), key=lambda h: hours[h])

    def has(rx, r):
        return bool(rx.search(r.get("text") or ""))

    mixed = sum(1 for r in rows if has(_AR_RE, r) and has(_EN_RE, r))
    ar = sum(1 for r in rows if has(_AR_RE, r))
    en = sum(1 for r in rows if has(_EN_RE, r))

    paced = []
    for r in rows:
        sec = float(r.get("seconds") or 0)
        w = words(r.get("text"))
        if sec >= 1.0 and w >= 4:
            paced.append((w / (sec / 60.0), r))
    fast = max(paced, key=lambda x: x[0]) if paced else None
    slow = min(paced, key=lambda x: x[0]) if paced else None

    return {"ok": True, "built": now.strftime("%H:%M:%S"),
            "today": span(today), "week": span(week), "prev_week": span(prev),
            "wpm_week": round(wpm(week), 1), "wpm_prev": round(wpm(prev), 1),
            "top": top, "fixes": fix_top, "hours": hours, "peak": peak,
            "langs": {"ar": ar, "en": en, "mixed": mixed, "n": len(rows)},
            "fastest": {"wpm": round(fast[0]) if fast else 0,
                        "line": (fast[1].get("text") or "")[:110] if fast else ""},
            "slowest": {"wpm": round(slow[0]) if slow else 0,
                        "line": (slow[1].get("text") or "")[:110] if slow else ""},
            "first_day": rows[0]["_dt"].strftime("%d %b %Y"),
            "all": span(rows)}


# ---------------------------------------------------------------- the sky
def galaxy(rows: list[dict], now: dt.datetime, days: float = 0, limit: int = 6000) -> dict:
    """Every dictation as a star: hours ago (radius) and hour of day (angle).

    days=0 sizes the window to the history (3 to 150 days); otherwise it is fixed.
    """
    span = float(days or 0) or 150.0
    stars = []
    for r in rows[-limit:]:
        age_h = (now - r["_dt"]).total_seconds() / 3600.0
        if age_h < 0 or age_h > span * 24:
            continue
        t = (r.get("text") or "").strip()
        if not t:
            continue
        fid = r.get("fid") or {}
        m = r.get("mood") or {}
        stars.append({"h": round(age_h, 3),
                      "a": round((r["_dt"].hour * 60 + r["_dt"].minute) / 1440.0, 4),
                      "w": words(t),
                      "l": (r.get("language") or "")[:2],
                      "app": app_label(r.get("app") or ""),
                      "f": fid.get("lvl") or "none",
                      "d": _fmt(r["_dt"]),
                      "m": m.get("m") or "",
                      "e": m.get("e") or "",
                      "to": r.get("to") or "",
                      "t": t[:160]})
    if days:
        win = span
    else:
        oldest = max([x["h"] for x in stars], default=0.0)
        win = min(150.0, max(3.0, (oldest / 24.0) * 1.15))
    return {"ok": True, "stars": stars, "days": round(win, 1), "total": len(stars)}


# ---------------------------------------------------------------- Room tab
def room(rows: list[dict], today: dt.date) -> dict:
    """Today's speech, one gauge, the shelf. No streaks, no scores."""
    todays = [r for r in rows if r["_dt"].date() == today]
    said = [{"t": (r.get("text") or "").strip(), "lang": (r.get("language") or "")[:2]}
            for r in todays if (r.get("text") or "").strip()][-80:]
    wc = sum(words(x["t"]) for x in said)
    frows = [r for r in todays if r.get("fid")]
    touched = sum(1 for r in frows if (r.get("fid") or {}).get("lvl") in ("amber", "red"))
    invented = sum((r.get("fid") or {}).get("ins") or 0 for r in frows)
    if not todays:
        gauge = {"k": "the day", "v": "quiet so far",
                 "line": "Nothing said yet. The room is open when you are."}
    elif touched:
        gauge = {"k": "the cleaner's hand", "v": "%d of %d touched" % (touched, len(frows)),
                 "line": ("It moved words in %d of %d dictations today. Open History and tap the dot to see "
                          "what you actually said." % (touched, len(frows)))}
    else:
        gauge = {"k": "the cleaner's hand", "v": "kept every word",
                 "line": ("%d dictations, %d words invented. Nothing today to answer for."
                          % (len(frows), invented))}
    date_line = today.strftime("%A, %d %B").replace(" 0", " ")
    if said:
        date_line += "  ·  %d said  ·  %d words" % (len(said), wc)
    return {"ok": True, "date_line": date_line, "sentences": said, "gauge": gauge,
            "seals": 0, "glyphs": [],
            "foot": "No streak is kept here. The days you speak are not a game."}


# ---------------------------------------------------------------- Style and History tabs
def style_samples(rows: list[dict], limit: int = 3) -> list[dict]:
    """The same words, before and after the cleaner. The speaker's own examples, not mocks."""
    out = []
    for r in sorted(rows, key=lambda r: r["_dt"], reverse=True):
        raw = (r.get("raw") or "").strip()
        text = (r.get("text") or "").strip()
        if not raw or not text or raw == text:
            continue
        out.append({"when": _fmt(r["_dt"]), "lang": r.get("language", "?"), "raw": raw, "text": text})
        if len(out) >= limit:
            break
    return out


def search(rows: list[dict], q: str = "", limit: int = 200) -> list[dict]:
    """Newest first, filtered by a case-insensitive substring of the text."""
    q = (q or "").strip().lower()
    out = []
    for r in sorted(rows, key=lambda r: r["_dt"], reverse=True):
        view = _row_view(r)
        if q and q not in view["text"].lower():
            continue
        m = r.get("mood") or {}
        view.update({"e": m.get("e") or "", "m": m.get("m") or ""})
        out.append(view)
        if len(out) >= int(limit):
            break
    return out
