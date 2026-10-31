"""rawi_core.history and rawi_core.analytics: the hub's numbers from a hand-built history."""
import datetime as dt
import json
import os
import tempfile
import unittest

import _helpers  # noqa: F401
from rawi_core import analytics, history, mood

NOW = dt.datetime(2026, 10, 30, 15, 0, 0)


def row(when, text, raw=None, lang="en", seconds=3.0, app="", fid=None, **extra):
    r = {"ts": when.isoformat(timespec="seconds"), "_dt": when, "text": text,
         "raw": raw if raw is not None else text, "language": lang, "seconds": seconds,
         "transcribe_s": 0.5, "app": app, "fid": fid}
    r.update(extra)
    return r


ROWS = [
    row(NOW - dt.timedelta(days=2, hours=1), "we ship it on friday", app="budget.xlsx - Excel"),
    row(NOW - dt.timedelta(days=1), "مرحبا هذا اختبار", lang="ar", app="Slack"),
    row(NOW - dt.timedelta(hours=2), "um so we ship it friday", raw="um so we ship it friday"),
    row(NOW - dt.timedelta(hours=1), "We ship it Friday.", raw="we shp it friday",
        fid={"d": 0.3, "ins": 0, "lvl": "amber"}, app="Slack"),
]


class TestHistoryFile(unittest.TestCase):
    def test_append_and_load_round_trip_skipping_bad_lines(self):
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "t.jsonl")
            history.append({"ts": "2026-10-30T10:00:00", "text": "thank you so much"}, path=path)
            history.append({"ts": "x", "text": "bad timestamp"}, path=path)
            history.append({"ts": "2026-10-30T11:00:00", "text": "skipped"}, on=False, path=path)
            with open(path, "a", encoding="utf-8") as f:
                f.write("{ torn line\n\n")
            rows = history.load_rows(path, tag_mood=True)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["_dt"], dt.datetime(2026, 10, 30, 10, 0))
        self.assertEqual(rows[0]["mood"]["m"], "tender")

    def test_missing_file_is_empty(self):
        self.assertEqual(history.load_rows("/no/such/file.jsonl"), [])

    def test_unicode_is_written_readably(self):
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "t.jsonl")
            history.append({"ts": "2026-10-30T10:00:00", "text": "راوي"}, path=path)
            with open(path, encoding="utf-8") as f:
                self.assertIn("راوي", f.read())


class TestHelpers(unittest.TestCase):
    def test_words_and_minutes(self):
        self.assertEqual(history.words("a  b\nc"), 3)
        self.assertEqual(history.words(None), 0)
        self.assertEqual(history.fmt_mins(90), "1.5 min")
        self.assertEqual(history.fmt_mins(5400), "1.5 h")

    def test_app_label(self):
        self.assertEqual(history.app_label("budget.xlsx - Excel"), "Excel")
        self.assertEqual(history.app_label("Inbox — Google Chrome"), "Google Chrome")
        self.assertEqual(history.app_label("Slack"), "Slack")
        self.assertEqual(history.app_label(None), "")

    def test_streak_survives_a_quiet_today(self):
        today = NOW.date()
        days = {today - dt.timedelta(days=i) for i in (1, 2, 3)}
        self.assertEqual(history.day_streak(days, today), 3)
        self.assertEqual(history.day_streak(days | {today}, today), 4)
        self.assertEqual(history.day_streak({today - dt.timedelta(days=5)}, today), 0)


class TestAnalytics(unittest.TestCase):
    def test_summary_totals(self):
        s = analytics.summary(ROWS, NOW.date())
        self.assertEqual(s["count"], 4)
        self.assertEqual(s["today"]["n"], 2)
        self.assertEqual(s["streak"], 3)
        self.assertEqual(s["langs"]["ar"], 1)

    def test_snapshot_shape(self):
        snap = analytics.snapshot(ROWS, NOW)
        self.assertEqual(snap["days"][-1]["iso"], NOW.date().isoformat())
        self.assertEqual(len(snap["hours"]), 24)
        self.assertEqual(snap["today"]["n"], 2)
        self.assertEqual(snap["recent"][0]["text"], "We ship it Friday.")
        self.assertEqual(snap["apps"][0], {"name": "Slack", "n": 2})
        json.dumps(snap)                                   # the page receives it as JSON

    def test_empty_history_is_fine_everywhere(self):
        self.assertEqual(analytics.snapshot([], NOW)["all"]["words"], 0)
        self.assertTrue(analytics.voice_report([], NOW)["empty"])
        self.assertEqual(analytics.galaxy([], NOW)["total"], 0)
        self.assertEqual(analytics.room([], NOW.date())["gauge"]["v"], "quiet so far")
        self.assertEqual(analytics.search([]), [])

    def test_room_reports_the_cleaners_hand(self):
        r = analytics.room(ROWS, NOW.date())
        self.assertEqual(len(r["sentences"]), 2)
        self.assertEqual(r["gauge"]["v"], "1 of 1 touched")

    def test_voice_report_finds_the_cleaners_fixes(self):
        v = analytics.voice_report(ROWS, NOW)
        self.assertIn({"heard": "shp", "wrote": "ship"}, [{k: f[k] for k in ("heard", "wrote")} for f in v["fixes"]])
        self.assertEqual(v["langs"], {"ar": 1, "en": 3, "mixed": 0, "n": 4})
        self.assertEqual(v["top"][0]["w"], "ship")

    def test_calendar_is_whole_weeks_starting_on_sunday(self):
        days = analytics.calendar_days(NOW.date())          # 30 Oct 2026 is a Friday
        self.assertEqual(days[0].strftime("%A"), "Sunday")
        self.assertEqual(days[-1], NOW.date())
        self.assertEqual(len(days), 7 * 25 + 6)              # 25 full weeks, then Sun..Fri
        self.assertEqual(len(analytics.calendar_days(dt.date(2026, 11, 1))), 7 * 25 + 1)

    def test_script_of(self):
        self.assertEqual(analytics.script_of("hello"), "en")
        self.assertEqual(analytics.script_of("مرحبا"), "ar")
        self.assertEqual(analytics.script_of("hello مرحبا"), "mixed")
        self.assertEqual(analytics.script_of("123"), "other")

    def test_arabic_punctuation_is_not_a_word(self):
        rows = [row(NOW, "كيف؟ كيف، كيف")]
        top = analytics.voice_report(rows, NOW)["top"]
        self.assertEqual([t["w"] for t in top], ["كيف"])
        self.assertEqual(top[0]["n"], 3)

    def test_insights(self):
        ins = analytics.voice_report(ROWS, NOW)["insights"]
        self.assertEqual(sum(ins["weekdays"]), 4)
        self.assertEqual(len(ins["lang_weeks"]), 8)
        self.assertEqual(ins["lang_weeks"][-1]["from"], "25 Oct")   # this week's Sunday
        self.assertEqual(ins["avg_len"]["ar"], 3.0)
        self.assertEqual(ins["active_days"], 3)
        self.assertEqual(ins["longest_run"], 3)
        self.assertEqual(ins["apps_lang"][0], {"app": "Slack", "ar": 1, "en": 1, "mixed": 0, "n": 2})
        self.assertEqual(ins["best_day"]["d"], "Fri 30 Oct")
        self.assertIn("ship", ins["vocab"]["new_examples"])
        json.dumps(ins)

    def test_galaxy_window(self):
        g = analytics.galaxy(ROWS, NOW)
        self.assertEqual(g["total"], 4)
        self.assertEqual(g["days"], 3.0)                   # never narrower than three days
        self.assertEqual(analytics.galaxy(ROWS, NOW, days=1)["total"], 3)    # 24 h old is inside
        self.assertEqual(analytics.galaxy(ROWS, NOW, days=0.5)["total"], 2)

    def test_search_and_style_samples(self):
        self.assertEqual(len(analytics.search(ROWS, "SHIP")), 3)
        self.assertEqual(len(analytics.search(ROWS, limit=1)), 1)
        samples = analytics.style_samples(ROWS)
        self.assertEqual(samples, [{"when": "30 Oct 14:00", "lang": "en",
                                    "raw": "we shp it friday", "text": "We ship it Friday."}])


class TestStatsPage(unittest.TestCase):
    def test_every_placeholder_is_filled_and_text_is_escaped(self):
        import stats
        with open(os.path.join(_helpers.ROOT, "template.html"), encoding="utf-8") as f:
            tpl = f.read()
        rows = ROWS + [row(NOW, "<b>not markup</b>")]
        page = stats.render(rows, NOW, tpl)
        self.assertNotRegex(page, r"__[A-Z]+__")
        self.assertIn("&lt;b&gt;not markup&lt;/b&gt;", page)


class TestMood(unittest.TestCase):
    def test_tags_are_explained_by_the_words(self):
        t = mood.tag("thank you, I miss you")
        self.assertEqual(t["m"], "longing")
        self.assertIn("miss you", t["why"])

    def test_arabic_and_nothing(self):
        self.assertEqual(mood.tag("الحمدلله")["m"], "grateful")
        self.assertIsNone(mood.tag("the weather"))
        self.assertIsNone(mood.tag(""))

    def test_latin_words_need_a_word_start(self):
        self.assertIsNone(mood.tag("airplane airplane"))   # "plan" inside a word is not a plan


if __name__ == "__main__":
    unittest.main(verbosity=2)
