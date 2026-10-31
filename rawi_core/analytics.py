"""The numbers behind the hub's tabs and stats.html, computed from history rows.

Every function is pure: rows in (from history.load_rows), a JSON-ready dict out, and the clock
passed in as `now` / `today`, so the same history always gives the same answer and tests can
build rows by hand. The output shapes are exactly what app.html reads; change both together.

    summary(rows, today)          shared totals: per day, per hour, languages, latency, streak
    snapshot(rows, now)           Usage tab: totals and a 26-week calendar (the hub adds "status")
    voice_report(rows, now)       Voice tab: pace, top words, the cleaner's fixes, `insights`
    insights(rows, now, ...)      Patterns: weekdays, languages by week, vocabulary, moods, runs
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

# a word: letters (\w covers Arabic) plus Arabic diacritics. Not the whole U+06xx block, which
# also holds the Arabic comma, semicolon and question mark (U+060C, U+061B, U+061F).
_TOKEN_RE = re.compile(r"[\w\u064B-\u065F\u0670']+")
_AR_RE = re.compile(r"[\u0600-\u06FF]")
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


def _sunday_on_or_before(day: dt.date) -> dt.date:
    """Weeks start on Sunday in the hub (date.weekday() counts Monday as 0)."""
    return day - dt.timedelta(days=(day.weekday() + 1) % 7)


def calendar_days(today: dt.date, weeks: int = 26) -> list[dt.date]:
    """Every day from the Sunday `weeks - 1` weeks before this week's, through today:
    whole weeks, so the Usage calendar draws one weekday per row."""
    start = _sunday_on_or_before(today) - dt.timedelta(days=7 * (weeks - 1))
    return [start + dt.timedelta(days=i) for i in range((today - start).days + 1)]


def script_of(text: str) -> str:
    """"ar", "en", "mixed" (both scripts) or "other" (neither)."""
    a, e = bool(_AR_RE.search(text or "")), bool(_EN_RE.search(text or ""))
    return "mixed" if a and e else ("ar" if a else ("en" if e else "other"))


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
    days = calendar_days(today)
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
        "days": [{"d": x.strftime("%a %d %b"), "iso": x.isoformat(), "words": s["per_day"].get(x, 0)}
                 for x in days],
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
            "all": span(rows), "insights": insights(rows, now, counts)}


def insights(rows: list[dict], now: dt.datetime, counts: dict[str, int] | None = None) -> dict:
    """More counts over the same log, nothing estimated (the Voice tab's Patterns).

    weekdays     dictations per weekday, Sunday first
    lang_weeks   the last 8 weeks, oldest first: {"from", "ar", "en", "mixed"} dictations
    avg_len      average words per dictation, by script
    vocab        {"unique", "total", "new_week", "new_examples"}: words first said this week,
                 most used first (`counts` ranks them; computed here when not given)
    apps_lang    the 6 busiest apps, with dictations per script
    moods        the 8 most frequent mood tags
    active_days, longest_run, best_day
    """
    rows = [r for r in rows if (r.get("text") or "").strip()]
    if counts is None:
        counts = {}
        for r in rows:
            for t in _TOKEN_RE.findall((r.get("text") or "").lower()):
                counts[t] = counts.get(t, 0) + 1

    weekdays = [0] * 7
    for r in rows:
        weekdays[(r["_dt"].weekday() + 1) % 7] += 1

    week_start = _sunday_on_or_before(now.date())
    lang_weeks = []
    for i in range(7, -1, -1):
        ws = week_start - dt.timedelta(days=7 * i)
        b = {"from": ws.strftime("%d %b"), "ar": 0, "en": 0, "mixed": 0}
        for r in rows:
            if ws <= r["_dt"].date() < ws + dt.timedelta(days=7):
                kind = script_of(r.get("text"))
                if kind in b:
                    b[kind] += 1
        lang_weeks.append(b)

    lens: dict[str, list[int]] = {"ar": [], "en": [], "mixed": []}
    for r in rows:
        kind = script_of(r.get("text"))
        if kind in lens:
            lens[kind].append(words(r.get("text")))
    avg_len = {k: (round(sum(v) / len(v), 1) if v else 0) for k, v in lens.items()}

    first_seen: dict[str, dt.datetime] = {}
    total_tok = 0
    for r in sorted(rows, key=lambda r: r["_dt"]):
        for t in _TOKEN_RE.findall((r.get("text") or "").lower()):
            total_tok += 1
            if len(t) >= 3 and t not in STOPWORDS and not t.isdigit() and t not in first_seen:
                first_seen[t] = r["_dt"]
    new_words = sorted((w for w, d in first_seen.items() if now - d < dt.timedelta(days=7)),
                       key=lambda w: -counts.get(w, 0))
    vocab = {"unique": len(first_seen), "total": total_tok, "new_week": len(new_words),
             "new_examples": new_words[:10]}

    apps: dict[str, dict] = {}
    for r in rows:
        a = app_label(r.get("app"))
        if a:
            b = apps.setdefault(a, {"app": a, "ar": 0, "en": 0, "mixed": 0, "n": 0})
            kind = script_of(r.get("text"))
            if kind in b:
                b[kind] += 1
            b["n"] += 1
    apps_lang = sorted(apps.values(), key=lambda b: -b["n"])[:6]

    moods: dict[str, dict] = {}
    for r in rows:
        m = r.get("mood") or {}
        if m.get("m"):
            e = moods.setdefault(m["m"], {"m": m["m"], "e": m.get("e") or "", "n": 0})
            e["n"] += 1
    top_moods = sorted(moods.values(), key=lambda x: -x["n"])[:8]

    days_on = sorted({r["_dt"].date() for r in rows})
    longest = run = 0
    for i, d in enumerate(days_on):
        run = run + 1 if i and (d - days_on[i - 1]).days == 1 else 1
        longest = max(longest, run)
    per_day: Counter = Counter()
    for r in rows:
        per_day[r["_dt"].date()] += words(r.get("text"))
    best = max(per_day.items(), key=lambda kv: kv[1]) if per_day else None

    return {"weekdays": weekdays, "lang_weeks": lang_weeks, "avg_len": avg_len, "vocab": vocab,
            "apps_lang": apps_lang, "moods": top_moods, "active_days": len(days_on),
            "longest_run": longest,
            "best_day": ({"d": best[0].strftime("%a %d %b"), "words": best[1]} if best else None)}


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
