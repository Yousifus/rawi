# Rawi Hub - a real window for the dictation daemon (pywebview + WebView2)
import datetime as dt
import json
import os
import re
import socket
import statistics
import subprocess
import sys
import time
import urllib.request
from collections import Counter, defaultdict

import webview

ROOT = os.path.dirname(os.path.abspath(__file__))
LOG = os.path.join(ROOT, "transcripts.jsonl")
CFG = os.path.join(ROOT, "config.json")
PAGE = os.path.join(ROOT, "app.html")
PYW = os.path.join(os.path.dirname(sys.executable), "pythonw.exe")
IS_WIN = sys.platform.startswith("win")
NO_WINDOW = 0x08000000 if IS_WIN else 0                     # CREATE_NO_WINDOW
# DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP on Windows; a new session elsewhere
DETACH = ({"creationflags": 0x00000008 | 0x00000200} if IS_WIN else {"start_new_session": True})
CLEANER = "http://127.0.0.1:18080/health"


import rawi_mood  # optional: one emoji per dictation, earned by words


def load_rows():
    rows = []
    if not os.path.exists(LOG):
        return rows
    with open(LOG, encoding="utf-8", errors="replace") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                r = json.loads(line)
                r["_dt"] = dt.datetime.fromisoformat(r["ts"])
                if not r.get("mood"):
                    r["mood"] = rawi_mood.tag(r.get("text"))
            except Exception:
                continue
            rows.append(r)
    return rows


def words(t):
    return len([w for w in (t or "").split() if w.strip()])


def fmt_mins(sec):
    m = sec / 60.0
    return "%.1f min" % m if m < 60 else "%.1f h" % (m / 60)


PIDFILE = os.path.join(ROOT, "daemon.pid")


def pid_alive(pid: int) -> bool:
    """True if the pid names a live process. OpenProcess on Windows, os.kill(pid, 0) elsewhere."""
    if not IS_WIN:
        try:
            os.kill(int(pid), 0)
            return True
        except PermissionError:
            return True
        except Exception:
            return False
    try:
        import ctypes
        k = ctypes.windll.kernel32
        h = k.OpenProcess(0x1000, False, int(pid))   # PROCESS_QUERY_LIMITED_INFORMATION
        if not h:
            return False
        code = ctypes.c_ulong(0)
        k.GetExitCodeProcess(h, ctypes.byref(code))
        k.CloseHandle(h)
        return code.value == 259                      # STILL_ACTIVE
    except Exception:
        return False


def kill_pid(pid: int) -> None:
    if IS_WIN:
        subprocess.run(["taskkill", "/PID", str(pid), "/F"],
                       capture_output=True, creationflags=NO_WINDOW)
        return
    try:
        import signal
        os.kill(int(pid), signal.SIGTERM)
    except Exception:
        pass


def daemon_pids():
    """The daemon's own pid, read from its pid file. A null CommandLine hides pythonw from CIM."""
    try:
        pid = int(open(PIDFILE, encoding="utf-8").read().strip())
    except Exception:
        return []
    return [pid] if pid_alive(pid) else []


GATE_PY = ""   # optional GPU-holder script; empty = disabled


def gpu_gate():
    """Who else is using this GPU, if anyone. None when the gate is free or unreadable."""
    if not GATE_PY:
        return None                       # unconfigured: never spawn a doomed subprocess per poll
    try:
        out = subprocess.run([PYW if os.path.exists(PYW) else sys.executable, GATE_PY, "status"],
                             capture_output=True, text=True, timeout=20,
                             creationflags=NO_WINDOW)
        for line in reversed(out.stdout.strip().splitlines()):
            line = line.strip()
            if line.startswith("{"):
                return (json.loads(line) or {}).get("holder")
    except Exception:
        return None
    return None


GATE_YIELD_PURPOSES = ("exclusive", "training", "render")


def gate_requires_yield(holder):
    purpose = str((holder or {}).get("purpose") or "").lower()
    return any(w in purpose for w in GATE_YIELD_PURPOSES)


def app_label(title: str) -> str:
    """'budget.xlsx - Excel' -> 'Excel'; a bare title stays as it is."""
    t = (title or "").strip()
    if not t:
        return ""
    for sep in (" - ", " — ", " – "):
        if sep in t:
            t = t.rsplit(sep, 1)[1]
    t = t.replace(" — Google Chrome", "").replace(" - Google Chrome", "")
    return t.strip()[:28]


def ipc_raw(obj, timeout=4):
    """One line of JSON to the daemon, one line back. Windows-loopback only."""
    try:
        s = socket.create_connection(("127.0.0.1", 18090), timeout)
        s.sendall((json.dumps(obj, ensure_ascii=False) + "\n").encode("utf-8"))
        out = s.recv(65536).decode("utf-8", "replace")
        s.close()
        return json.loads(out.strip() or "{}")
    except Exception as e:
        return {"ok": False, "err": str(e)}


def status_block():
    holder = gpu_gate()
    live = ipc_raw({"cmd": "status"}, timeout=1.5)
    return {"daemon": bool(daemon_pids()), "cleaner": cleaner_up(),
            "gpu": (holder or {}).get("owner"), "busy": gate_requires_yield(holder),
            "listening": bool(live.get("listening")), "dropped": int(live.get("dropped") or 0)}


def ensure_cleaner():
    """Start the cleaner if 18080 is dark, the GPU is free, and a launcher actually exists."""
    if cleaner_up():
        return "already up"
    holder = gpu_gate()
    if holder:
        return "skipped: gpu busy with %s" % holder.get("owner", "?")
    launcher = os.path.join(ROOT, "cleaner-server.bat")
    if not os.path.exists(launcher):
        return "no cleaner-server.bat in this folder"      # nothing to start; say so, do not fail silently
    try:
        subprocess.Popen(["cmd.exe", "/c", launcher],
                         cwd=ROOT, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                         stdin=subprocess.DEVNULL,
                         creationflags=0x08000000 | 0x00000200)  # CREATE_NO_WINDOW | NEW_GROUP
        return "starting"
    except Exception as e:
        return "failed: %s" % e


def cleaner_up():
    try:
        with urllib.request.urlopen(CLEANER, timeout=1.2) as r:
            return r.status == 200
    except Exception:
        return False


class Api:
    def snapshot(self):
        rows = load_rows()
        now = dt.datetime.now()
        today = now.date()
        by_day = defaultdict(lambda: {"n": 0, "words": 0, "sec": 0.0, "lat": []})
        per_day = defaultdict(int)
        hours = Counter()
        langs = Counter()
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

        t = by_day.get(today, {"n": 0, "words": 0, "sec": 0.0})
        tw = sum(v["words"] for v in by_day.values())
        ts = sum(v["sec"] for v in by_day.values())
        tn = sum(v["n"] for v in by_day.values())
        lats = [x for v in by_day.values() for x in v["lat"]]

        streak = 0
        d = today
        for _ in range(400):
            if d in per_day:
                streak += 1
                d -= dt.timedelta(days=1)
            elif streak == 0 and (d - dt.timedelta(days=1)) in per_day:
                d -= dt.timedelta(days=1)
            else:
                break

        # 26 whole weeks for the Usage calendar, starting on a Sunday so each row is one weekday
        start = today - dt.timedelta(days=7 * 25 + (today.weekday() + 1) % 7)
        days = [start + dt.timedelta(days=i) for i in range((today - start).days + 1)]
        recent = []
        for r in sorted(rows, key=lambda r: r["_dt"], reverse=True)[:12]:
            recent.append({"when": r["_dt"].strftime("%d %b %H:%M"),
                           "lang": r.get("language", "?"),
                           "text": (r.get("text") or "").strip(),
                           "app": app_label(r.get("app")),
                           "clean": r.get("clean") or "",
                           "fid": r.get("fid") or None,
                           "raw": (r.get("raw") or "").strip()})

        return {
            "when": now.strftime("%A %d %B %Y, %H:%M"),
            "today": {"words": t["words"], "time": fmt_mins(t["sec"]), "n": t["n"]},
            "all": {"words": tw,
                    "wpm": "%.0f" % (tw / (ts / 60)) if ts else "0",
                    "saved": "%.0f" % max(0.0, tw / 40.0 - ts / 60),
                    "lat": "%.2f" % statistics.median(lats) if lats else "0",
                    "rtf": "%.1f" % (ts / sum(lats)) if lats and sum(lats) else "0"},
            "streak": streak,
            "days": [{"d": x.strftime("%a %d %b"), "iso": x.isoformat(), "words": per_day.get(x, 0)}
                     for x in days],
            "hours": [hours.get(h, 0) for h in range(24)],
            "langs": langs.most_common(),
            "apps": [{"name": k, "n": v} for k, v in
                     Counter(app_label(r.get("app")) for r in rows if r.get("app")).most_common(6)],
            "recent": recent,
            "status": status_block(),
        }

    # ---------- your voice: live, with receipts ----------
    STOP = set("""a an the and or but if then that this these those i you he she it we they me my your his her
its our their is are was were be been being am do does did doing have has had having to of in on at for with
from by as so not no yes just very really about into over after before when while what which who whom how why
can could would should will shall may might must im ive id ill youre dont doesnt didnt cant wont isnt wasnt
and-or than too also there here now out up down all any some more most other such only own same s t d ll re ve m
في من على عن الى إلى مع ان أن إن هذا هذه ذلك تلك هو هي هم انا أنا كنت كان كانت ما لا لم لن قد كل بعض غير بين
عند حتى لو ثم او أو يا اي أي شي شيء يعني بس لكن و انه أنه انها أنها كذا زي هذا huh um uh erm hmm yeah ok okay like just really actually maybe got get know
im i'm it's thats that's youre you're we're they're i've ive dont don't didnt didn't isnt isn't wasnt wasn't
cant can't wont won't whats what's theres there's lets let's gonna wanna sorta kinda""".split())

    def voice(self):
        """Every number here is a query over his own log, computed now, and every one can be traced."""
        rows = load_rows()
        now = dt.datetime.now()
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
            w = sum(words(r.get("text")) for r in rs)
            return {"n": len(rs), "words": w, "wpm": round(wpm(rs), 1)}

        tok_re = re.compile(r"[\w\u064B-\u065F\u0670']+")    # letters and harakat; not \u060C \u061F \u061B which share U+06xx
        counts = {}
        for r in rows:
            for t in tok_re.findall((r.get("text") or "").lower()):
                if len(t) < 3 or t in self.STOP or t.isdigit():
                    continue
                counts[t] = counts.get(t, 0) + 1
        top = []
        for w, c in sorted(counts.items(), key=lambda kv: -kv[1])[:10]:
            sample = next((r for r in reversed(rows) if w in (r.get("text") or "").lower()), None)
            top.append({"w": w, "n": c, "when": sample["_dt"].strftime("%d %b %H:%M") if sample else "",
                        "line": (sample.get("text") or "")[:120] if sample else ""})

        fixes = {}
        import difflib as _dl
        for r in rows:
            raw, cl = (r.get("raw") or ""), (r.get("text") or "")
            if not raw or not cl or raw.strip() == cl.strip():
                continue
            a = tok_re.findall(raw.lower())
            b = tok_re.findall(cl.lower())
            sm = _dl.SequenceMatcher(None, a, b, autojunk=False)
            for op, i1, i2, j1, j2 in sm.get_opcodes():
                if op == "replace" and (i2 - i1) and (j2 - j1):
                    src = " ".join(a[i1:i2])[:40]
                    dst = " ".join(b[j1:j2])[:40]
                    if src != dst:
                        k = (src, dst)
                        e = fixes.setdefault(k, {"heard": src, "wrote": dst, "n": 0, "first": r["_dt"]})
                        e["n"] += 1
                        if r["_dt"] < e["first"]:
                            e["first"] = r["_dt"]
        fix_top = []
        for e in sorted(fixes.values(), key=lambda x: -x["n"])[:8]:
            fix_top.append({"heard": e["heard"], "wrote": e["wrote"], "n": e["n"],
                            "first": e["first"].strftime("%d %b")})

        hours = [0] * 24
        for r in rows:
            hours[r["_dt"].hour] += 1
        peak = max(range(24), key=lambda h: hours[h]) if rows else 0

        ar_re = re.compile(r"[\u0600-\u06FF]")
        en_re = re.compile(r"[A-Za-z]")
        mixed = sum(1 for r in rows if ar_re.search(r.get("text") or "") and en_re.search(r.get("text") or ""))
        ar = sum(1 for r in rows if ar_re.search(r.get("text") or ""))
        en = sum(1 for r in rows if en_re.search(r.get("text") or ""))

        def pace_rows(rs):
            out = []
            for r in rs:
                sec = float(r.get("seconds") or 0)
                w = words(r.get("text"))
                if sec >= 1.0 and w >= 4:
                    out.append((w / (sec / 60.0), r))
            return out

        paced = pace_rows(rows)
        fast = max(paced, key=lambda x: x[0]) if paced else None
        slow = min(paced, key=lambda x: x[0]) if paced else None

        # ---- insights: more counts over the same log, nothing estimated ----
        def script(t):
            a, e = bool(ar_re.search(t)), bool(en_re.search(t))
            return "mixed" if a and e else ("ar" if a else ("en" if e else "other"))

        weekdays = [0] * 7                                   # Sunday first
        for r in rows:
            weekdays[(r["_dt"].weekday() + 1) % 7] += 1

        week_start = (now.date() - dt.timedelta(days=(now.weekday() + 1) % 7))
        lang_weeks = []
        for i in range(7, -1, -1):                           # the last 8 weeks, oldest first
            ws = week_start - dt.timedelta(days=7 * i)
            b = {"from": ws.strftime("%d %b"), "ar": 0, "en": 0, "mixed": 0}
            for r in rows:
                if ws <= r["_dt"].date() < ws + dt.timedelta(days=7):
                    s = script(r.get("text") or "")
                    if s in b:
                        b[s] += 1
            lang_weeks.append(b)

        lens = {"ar": [], "en": [], "mixed": []}
        for r in rows:
            s = script(r.get("text") or "")
            if s in lens:
                lens[s].append(words(r.get("text")))
        avg_len = {k: (round(sum(v) / len(v), 1) if v else 0) for k, v in lens.items()}

        first_seen, total_tok = {}, 0
        for r in sorted(rows, key=lambda r: r["_dt"]):
            for t in tok_re.findall((r.get("text") or "").lower()):
                total_tok += 1
                if len(t) >= 3 and t not in self.STOP and not t.isdigit() and t not in first_seen:
                    first_seen[t] = r["_dt"]
        new_words = sorted((w for w, d in first_seen.items() if now - d < 7 * day),
                           key=lambda w: -counts.get(w, 0))
        vocab = {"unique": len(first_seen), "total": total_tok, "new_week": len(new_words),
                 "new_examples": new_words[:10]}

        apps = {}
        for r in rows:
            a = app_label(r.get("app"))
            if a:
                b = apps.setdefault(a, {"app": a, "ar": 0, "en": 0, "mixed": 0, "n": 0})
                s = script(r.get("text") or "")
                if s in b:
                    b[s] += 1
                b["n"] += 1
        apps_lang = sorted(apps.values(), key=lambda b: -b["n"])[:6]

        moods = {}
        for r in rows:
            m = (r.get("mood") or {})
            if m.get("m"):
                e = moods.setdefault(m["m"], {"m": m["m"], "e": m.get("e") or "", "n": 0})
                e["n"] += 1
        moods = sorted(moods.values(), key=lambda x: -x["n"])[:8]

        days_on = sorted({r["_dt"].date() for r in rows})
        longest = run = 0
        for i, d in enumerate(days_on):
            run = run + 1 if i and (d - days_on[i - 1]).days == 1 else 1
            longest = max(longest, run)
        per_day = Counter()
        for r in rows:
            per_day[r["_dt"].date()] += words(r.get("text"))
        best_day = max(per_day.items(), key=lambda kv: kv[1]) if per_day else None
        insights = {"weekdays": weekdays, "lang_weeks": lang_weeks, "avg_len": avg_len, "vocab": vocab,
                    "apps_lang": apps_lang, "moods": moods, "active_days": len(days_on),
                    "longest_run": longest,
                    "best_day": ({"d": best_day[0].strftime("%a %d %b"), "words": best_day[1]}
                                 if best_day else None)}

        return {"ok": True, "built": now.strftime("%H:%M:%S"),
                "today": span(today), "week": span(week), "prev_week": span(prev),
                "wpm_week": round(wpm(week), 1), "wpm_prev": round(wpm(prev), 1),
                "top": top, "fixes": fix_top, "hours": hours, "peak": peak,
                "langs": {"ar": ar, "en": en, "mixed": mixed, "n": len(rows)},
                "fastest": {"wpm": round(fast[0]) if fast else 0,
                            "line": (fast[1].get("text") or "")[:110] if fast else ""},
                "slowest": {"wpm": round(slow[0]) if slow else 0,
                            "line": (slow[1].get("text") or "")[:110] if slow else ""},
                "first_day": rows[0]["_dt"].strftime("%d %b %Y") if rows else "",
                "all": span(rows), "insights": insights}

    def listen(self, on=None):
        """Open or close the ear. on=None flips it."""
        return ipc_raw({"cmd": "listen"} if on is None else {"cmd": "listen", "on": bool(on)})

    def galaxy(self, days=0, limit=6000):
        span = float(days or 0) or 150.0
        """Every dictation as a star: hours ago (radius) and hour of day (angle)."""
        rows = load_rows()
        now = dt.datetime.now()
        stars = []
        for r in rows[-limit:]:
            age_h = (now - r["_dt"]).total_seconds() / 3600.0
            if age_h < 0 or age_h > span * 24:
                continue
            t = (r.get("text") or "").strip()
            if not t:
                continue
            fid = r.get("fid") or {}
            stars.append({"h": round(age_h, 3),
                          "a": round((r["_dt"].hour * 60 + r["_dt"].minute) / 1440.0, 4),
                          "w": words(t),
                          "l": (r.get("language") or "")[:2],
                          "app": app_label(r.get("app") or ""),
                          "f": fid.get("lvl") or "none",
                          "d": r.get("_dt").strftime("%d %b %H:%M"),
                          "m": (r.get("mood") or {}).get("m") or "",
                          "e": (r.get("mood") or {}).get("e") or "",
                          "to": r.get("to") or "",
                          "t": t[:160]})
        if days:
            win = span
        else:
            oldest = max([x["h"] for x in stars], default=0.0)
            win = min(150.0, max(3.0, (oldest / 24.0) * 1.15))
        return {"ok": True, "stars": stars, "days": round(win, 1), "total": len(stars)}

    def room(self):
        """The Room: today's speech, one gauge, the shelf. No streaks, no scores."""
        rows = load_rows()
        today = dt.date.today()
        todays = [r for r in rows if r["_dt"].date() == today]
        said = []
        for r in todays:
            t = (r.get("text") or "").strip()
            if t:
                said.append({"t": t, "lang": (r.get("language") or "")[:2]})
        said = said[-80:]
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

    def style_samples(self, limit=3):
        """The same words, before and after the cleaner. His own examples, not mocks."""
        out = []
        for r in sorted(load_rows(), key=lambda r: r["_dt"], reverse=True):
            raw = (r.get("raw") or "").strip()
            text = (r.get("text") or "").strip()
            if not raw or not text or raw == text:
                continue
            out.append({"when": r["_dt"].strftime("%d %b %H:%M"), "lang": r.get("language", "?"),
                        "raw": raw, "text": text})
            if len(out) >= limit:
                break
        return out

    def history(self, q="", limit=200):
        rows = load_rows()
        q = (q or "").strip().lower()
        out = []
        for r in sorted(rows, key=lambda r: r["_dt"], reverse=True):
            txt = (r.get("text") or "").strip()
            if q and q not in txt.lower():
                continue
            out.append({"when": r["_dt"].strftime("%d %b %H:%M"),
                        "lang": r.get("language", "?"), "text": txt,
                        "app": app_label(r.get("app")),
                        "clean": r.get("clean") or "",
                        "fid": r.get("fid") or None,
                        "e": (r.get("mood") or {}).get("e") or "",
                        "m": (r.get("mood") or {}).get("m") or "",
                        "raw": (r.get("raw") or "").strip()})
            if len(out) >= int(limit):
                break
        return out

    def get_config(self):
        try:
            c = json.load(open(CFG, encoding="utf-8"))
        except Exception:
            c = {}
        d = c.get("dictionary") or {}
        cl = c.get("clean") or {}
        ls = c.get("listen") or {}
        # defaults mirror rawi.py's DEFAULTS, so the hub shows what the daemon will actually do
        return {"ok": True,
                "hold_key": c.get("hold_key", "ctrl_l"),
                "keys": {"paste": c.get("paste_key", "f9"), "lang": c.get("lang_key", "f10"),
                         "talk": c.get("talk_key", "f7"), "listen": ls.get("key", "f8")},
                "log": bool(c.get("log", False)),
                "talk": bool(c.get("talk", False)),
                "mood": c.get("mood", "auto"),
                "clean": bool(cl.get("enabled", False)),
                "level": cl.get("level", "light"),
                "tone": cl.get("tone", "default"),
                "language": c.get("language", "auto"),
                "model": c.get("model", "large-v3-turbo"),
                "names": d.get("names") or [],
                "pairs": d.get("pairs") or [],
                "profiles": c.get("profiles") or [],
                "snippets": c.get("snippets") or {}}

    def save_config(self, patch):
        try:
            c = json.load(open(CFG, encoding="utf-8"))
        except Exception:
            return {"ok": False, "message": "Could not read config.json."}
        for k in ("hold_key", "language", "model"):
            if str(patch.get(k) or "").strip():   # an empty value would silently kill dictation
                c[k] = patch[k]
        if "log" in patch:
            c["log"] = bool(patch["log"])
        cl = c.setdefault("clean", {})
        if "clean" in patch:              # only when asked: a mode or mood change must not switch cleanup on
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
        json.dump(c, open(CFG, "w", encoding="utf-8"), indent=2, ensure_ascii=False)
        n = self.restart_daemon()
        return {"ok": True, "message": ("Saved. rawi restarted and is loading the model." if n
                                        else "Saved, but rawi did not start again. Run start.bat to see why.")}

    def stop_daemon(self):
        """Stop the Whisper daemon for real.

        Closing the Hub window only hides it to the tray (by design: the hotkeys must keep
        working), so for a year 'Quit' left rawi.py alive with ~4.6 GB of Whisper committed.
        Quit must be the thing that gives the memory back."""
        stopped = []
        for pid in daemon_pids():
            kill_pid(pid)
            stopped.append(pid)
        try:
            os.remove(PIDFILE)
        except Exception:
            pass
        return stopped

    def restart_daemon(self):
        for pid in daemon_pids():
            kill_pid(pid)
        time.sleep(1.2)
        exe = PYW if os.path.exists(PYW) else sys.executable
        logf = open(os.path.join(ROOT, "dict.log"), "a", encoding="utf-8", errors="replace")
        subprocess.Popen([exe, os.path.join(ROOT, "rawi.py")],
                         stdout=logf, stderr=logf, stdin=subprocess.DEVNULL, **DETACH)
        time.sleep(2.0)
        return len(daemon_pids())

    def copy(self, text):
        try:
            if IS_WIN:
                subprocess.run(["powershell.exe", "-NoProfile", "-Command",
                                "$t=$input | Out-String; Set-Clipboard -Value $t"],
                               input=text, capture_output=True, text=True,
                               creationflags=NO_WINDOW)
            else:
                import pyperclip
                pyperclip.copy(text)
            return "copied"
        except Exception as e:
            return "copy failed: %s" % e

    def log(self, msg):
        with open(os.path.join(ROOT, "app_debug.log"), "a", encoding="utf-8") as f:
            f.write("%s %s\n" % (dt.datetime.now().strftime("%H:%M:%S"), msg))
        return "ok"

    def open_folder(self):
        if IS_WIN:
            os.startfile(ROOT)
        else:
            subprocess.Popen(["open" if sys.platform == "darwin" else "xdg-open", ROOT])
        return "opened"


_ICON = [None]   # the tray icon, so closing the window can speak through it


def _tray(win, api):
    import threading
    import pystray
    from PIL import Image

    img = Image.open(os.path.join(ROOT, "icon.png"))

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


def main():
    api = Api()
    ensure_cleaner()
    # tests boot the window without loading Whisper: a marker file, because WSL does not pass
    # ordinary env vars to Windows processes (only WSLENV does)
    if not os.path.exists(os.path.join(ROOT, "_no_daemon")):
        api.restart_daemon()
    html = open(PAGE, encoding="utf-8").read()
    win = webview.create_window("Rawi", html=html, width=1080, height=780,
                                min_size=(880, 600), background_color="#f4ecdd",
                                js_api=api)

    def on_closing():
        # closing the window sends it to the tray; Quit from the tray really exits.
        # Say it, so a hidden window is never mistaken for a stopped one.
        if _ICON[0] is None:
            return True               # no tray (pystray missing, or macOS): a hidden window is lost
        try:
            win.hide()
        except Exception:
            pass
        try:
            if _ICON[0] is not None:
                _ICON[0].title = "Rawi - still listening (F8). Quit here to stop."
                _ICON[0].notify("Rawi is still listening - the hotkeys work. "
                                "Quit from this tray icon to stop it.", "Rawi")
        except Exception:
            pass
        return False

    win.events.closing += on_closing

    def start_tray():
        if sys.platform == "darwin":
            return                    # pystray on macOS needs the main thread, which webview owns
        time.sleep(1.0)
        _tray(win, api)

    import threading
    threading.Thread(target=start_tray, daemon=True).start()
    webview.start(debug=False)


if __name__ == "__main__":
    main()
