# Rawi analytics - transcripts.jsonl -> stats.html
import argparse
import datetime as dt
import json
import os
import statistics
import sys
import webbrowser
from collections import Counter, defaultdict

ROOT = os.path.dirname(os.path.abspath(__file__))
LOG = os.path.join(ROOT, "transcripts.jsonl")
TPL = os.path.join(ROOT, "template.html")
OUT = os.path.join(ROOT, "stats.html")
TYPING_WPM = 40.0
COL = {"bg": "#0d0b08", "panel": "#151109", "line": "#2a2113", "gold": "#e8b34a",
       "ember": "#a9711f", "dim": "#6b5a3a", "text": "#efe6d4", "mute": "#93866b"}


def load():
    rows = []
    if not os.path.exists(LOG):
        return rows
    with open(LOG, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                rows.append(json.loads(line))
            except Exception:
                pass
    out = []
    for r in rows:
        try:
            r["_dt"] = dt.datetime.fromisoformat(r["ts"])
        except Exception:
            continue
        out.append(r)
    return out


def words(text):
    return len([w for w in (text or "").replace("\n", " ").split(" ") if w.strip()])


def fmt_mins(sec):
    m = sec / 60.0
    return "%.1f min" % m if m < 60 else "%.1f h" % (m / 60)


def bars_svg(values, labels, width=760, height=150, accent=COL["gold"]):
    if not values:
        return ""
    n = len(values)
    pad = 8
    bw = (width - pad * 2) / max(n, 1)
    mx = max(values) or 1
    out = ['<svg viewBox="0 0 %d %d" class="chart" style="height:%dpx" preserveAspectRatio="none">' % (width, height, height)]
    out.append('<line x1="%d" y1="%d" x2="%d" y2="%d" stroke="%s"/>'
               % (pad, height - 22, width - pad, height - 22, COL["line"]))
    for i, v in enumerate(values):
        h = (height - 34) * (v / mx) if mx else 0
        x = pad + i * bw
        y = height - 22 - h
        op = 0.25 + 0.75 * (v / mx if mx else 0)
        out.append('<rect x="%.1f" y="%.1f" width="%.1f" height="%.1f" fill="%s" opacity="%.2f" rx="1.5"/>'
                   % (x + bw * 0.18, y, bw * 0.64, max(h, 1), accent, op))
    for i, lab in enumerate(labels):
        if lab:
            out.append('<text x="%.1f" y="%d" fill="%s" font-size="9" text-anchor="middle">%s</text>'
                       % (pad + i * bw + bw / 2, height - 6, COL["mute"], lab))
    out.append("</svg>")
    return "".join(out)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--no-open", action="store_true")
    args = ap.parse_args()

    rows = load()
    now = dt.datetime.now()
    today = now.date()

    by_day = defaultdict(lambda: {"n": 0, "words": 0, "sec": 0.0, "lat": []})
    by_hour = Counter()
    langs = Counter()
    per_day_words = defaultdict(int)

    for r in rows:
        d = r["_dt"].date()
        b = by_day[d]
        b["n"] += 1
        w = words(r.get("text"))
        b["words"] += w
        b["sec"] += float(r.get("seconds") or 0)
        if r.get("transcribe_s") is not None:
            b["lat"].append(float(r["transcribe_s"]))
        by_hour[r["_dt"].hour] += 1
        langs[str(r.get("language") or "?").lower()] += 1
        per_day_words[d] += w

    t = by_day.get(today, {"n": 0, "words": 0, "sec": 0.0})
    total_words = sum(v["words"] for v in by_day.values())
    total_sec = sum(v["sec"] for v in by_day.values())
    total_n = sum(v["n"] for v in by_day.values())
    spm = (total_words / (total_sec / 60)) if total_sec else 0.0
    saved_min = max(0.0, total_words / TYPING_WPM - total_sec / 60)
    lat_all = [x for v in by_day.values() for x in v["lat"]]
    med_lat = statistics.median(lat_all) if lat_all else 0.0
    rtf = (total_sec / sum(lat_all)) if lat_all and sum(lat_all) else 0.0

    streak = 0
    d = today
    guard = 0
    while guard < 400:
        guard += 1
        if d in per_day_words:
            streak += 1
            d -= dt.timedelta(days=1)
            continue
        if streak == 0 and (d - dt.timedelta(days=1)) in per_day_words:
            d -= dt.timedelta(days=1)
            continue
        break

    days = [today - dt.timedelta(days=i) for i in range(29, -1, -1)]
    vals = [float(per_day_words.get(x, 0)) for x in days]
    labels = [x.strftime("%d") if (i % 5 == 0) else "" for i, x in enumerate(days)]
    chart = bars_svg(vals, labels)

    hvals = [float(by_hour.get(h, 0)) for h in range(24)]
    hlabels = ["%02d" % h if h % 3 == 0 else "" for h in range(24)]
    hchart = bars_svg(hvals, hlabels, width=760, height=110, accent=COL["ember"])

    recent_html = []
    for r in sorted(rows, key=lambda r: r["_dt"], reverse=True)[:18]:
        txt = (r.get("text") or "").strip()
        if len(txt) > 260:
            txt = txt[:260] + "..."
        recent_html.append('<div class="row"><div class="when">%s</div><div class="lang">%s</div>'
                           '<div class="txt" dir="auto">%s</div></div>'
                           % (r["_dt"].strftime("%d %b %H:%M"), r.get("language", "?"), txt))

    lang_html = " ".join('<span class="pill">%s &middot; %d</span>' % (k, v) for k, v in langs.most_common())
    if not lang_html:
        lang_html = '<span class="pill">no data yet</span>'

    tpl = open(TPL, encoding="utf-8").read()
    rep = {
        "__BG__": COL["bg"], "__TEXT__": COL["text"], "__GOLD__": COL["gold"], "__EMBER__": COL["ember"],
        "__MUTE__": COL["mute"], "__PANEL__": COL["panel"], "__LINE__": COL["line"],
        "__WHEN__": now.strftime("%A %d %B %Y, %H:%M"),
        "__TW__": str(t["words"]), "__TS__": fmt_mins(t["sec"]), "__TN__": str(t["n"]),
        "__STREAK__": str(streak), "__AW__": str(total_words), "__SPM__": "%.0f" % spm,
        "__SAVED__": "%.0f" % saved_min, "__LAT__": "%.2f" % med_lat, "__RTF__": "%.1f" % rtf,
        "__CHART__": chart or '<div class="empty">Nothing yet. Hold the key and say something.</div>',
        "__HCHART__": hchart or '<div class="empty">-</div>',
        "__LANGS__": lang_html,
        "__RECENT__": "".join(recent_html) or '<div class="empty">No transcripts yet.</div>',
        "__TOTALN__": str(total_n), "__TOTALSEC__": fmt_mins(total_sec),
    }
    for k, v in rep.items():
        tpl = tpl.replace(k, v)

    with open(OUT, "w", encoding="utf-8") as f:
        f.write(tpl)
    print("wrote %s (%d dictations)" % (OUT, len(rows)))
    if not args.no_open:
        webbrowser.open("file:///" + OUT.replace("\\", "/"))
    return 0


if __name__ == "__main__":
    sys.exit(main())
