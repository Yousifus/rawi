"""Rawi analytics: transcripts.jsonl -> stats.html, a static page you can open in any browser.

    python stats.py            write stats.html and open it
    python stats.py --no-open  write it only

The numbers come from rawi_core.analytics.summary, the same totals the hub's Usage tab shows.
"""
from __future__ import annotations

import argparse
import datetime as dt
import html
import pathlib
import statistics
import sys
import webbrowser

from rawi_core import analytics, history, paths
from rawi_core.history import fmt_mins

TYPING_WPM = 40.0       # the typing speed "time saved" is measured against
COL = {"bg": "#0d0b08", "panel": "#151109", "line": "#2a2113", "gold": "#e8b34a",
       "ember": "#a9711f", "dim": "#6b5a3a", "text": "#efe6d4", "mute": "#93866b"}


def bars_svg(values, labels, width=760, height=150, accent=COL["gold"]) -> str:
    """A minimal inline-SVG bar chart (no JS, no library)."""
    if not values:
        return ""
    n = len(values)
    pad = 8
    bw = (width - pad * 2) / max(n, 1)
    mx = max(values) or 1
    out = ['<svg viewBox="0 0 %d %d" class="chart" style="height:%dpx" preserveAspectRatio="none">'
           % (width, height, height)]
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


def render(rows: list[dict], now: dt.datetime, template: str) -> str:
    """Fill template.html's __PLACEHOLDERS__ from the history rows."""
    today = now.date()
    s = analytics.summary(rows, today)
    t = s["today"]
    total_words, total_sec, lat_all = s["words"], s["seconds"], s["latencies"]
    spm = (total_words / (total_sec / 60)) if total_sec else 0.0
    saved_min = max(0.0, total_words / TYPING_WPM - total_sec / 60)
    med_lat = statistics.median(lat_all) if lat_all else 0.0
    rtf = (total_sec / sum(lat_all)) if lat_all and sum(lat_all) else 0.0

    days = [today - dt.timedelta(days=i) for i in range(29, -1, -1)]
    chart = bars_svg([float(s["per_day"].get(x, 0)) for x in days],
                     [x.strftime("%d") if (i % 5 == 0) else "" for i, x in enumerate(days)])
    hchart = bars_svg([float(s["hours"].get(h, 0)) for h in range(24)],
                      ["%02d" % h if h % 3 == 0 else "" for h in range(24)],
                      width=760, height=110, accent=COL["ember"])

    recent_html = []
    for r in sorted(rows, key=lambda r: r["_dt"], reverse=True)[:18]:
        txt = (r.get("text") or "").strip()
        if len(txt) > 260:
            txt = txt[:260] + "..."
        recent_html.append('<div class="row"><div class="when">%s</div><div class="lang">%s</div>'
                           '<div class="txt" dir="auto">%s</div></div>'
                           % (r["_dt"].strftime("%d %b %H:%M"), html.escape(str(r.get("language", "?"))),
                              html.escape(txt)))

    lang_html = " ".join('<span class="pill">%s &middot; %d</span>' % (html.escape(k), v)
                         for k, v in s["langs"].most_common())
    if not lang_html:
        lang_html = '<span class="pill">no data yet</span>'

    rep = {
        "__BG__": COL["bg"], "__TEXT__": COL["text"], "__GOLD__": COL["gold"], "__EMBER__": COL["ember"],
        "__MUTE__": COL["mute"], "__PANEL__": COL["panel"], "__LINE__": COL["line"],
        "__WHEN__": now.strftime("%A %d %B %Y, %H:%M"),
        "__TW__": str(t["words"]), "__TS__": fmt_mins(t["sec"]), "__TN__": str(t["n"]),
        "__STREAK__": str(s["streak"]), "__AW__": str(total_words), "__SPM__": "%.0f" % spm,
        "__SAVED__": "%.0f" % saved_min, "__LAT__": "%.2f" % med_lat, "__RTF__": "%.1f" % rtf,
        "__CHART__": chart or '<div class="empty">Nothing yet. Hold the key and say something.</div>',
        "__HCHART__": hchart or '<div class="empty">-</div>',
        "__LANGS__": lang_html,
        "__RECENT__": "".join(recent_html) or '<div class="empty">No transcripts yet.</div>',
        "__TOTALN__": str(s["count"]), "__TOTALSEC__": fmt_mins(total_sec),
    }
    for k, v in rep.items():
        template = template.replace(k, v)
    return template


def main() -> int:
    ap = argparse.ArgumentParser(description="write stats.html from transcripts.jsonl")
    ap.add_argument("--no-open", action="store_true", help="write the page without opening it")
    args = ap.parse_args()

    rows = history.load_rows()
    with open(paths.STATS_TEMPLATE, encoding="utf-8") as f:
        page = render(rows, dt.datetime.now(), f.read())
    with open(paths.STATS_PAGE, "w", encoding="utf-8") as f:
        f.write(page)
    print("wrote %s (%d dictations)" % (paths.STATS_PAGE, len(rows)))
    if not args.no_open:
        webbrowser.open(pathlib.Path(paths.STATS_PAGE).as_uri())   # a valid file:// URL on every OS
    return 0


if __name__ == "__main__":
    sys.exit(main())
