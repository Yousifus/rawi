"""Hub settings must survive a partial save.

Two defects found on abdulrahman's Mac test (2026-10-31) were fixed on main by the hub
clarity pass (a blank hold key, and cleanup switching itself on). This file holds the
remaining one -- saving half the dictionary erased the other half -- and one small
neighbour: the value that passed the "not empty" check must be the value written.
"""
import json
import os
import sys
import tempfile
import types
import unittest
from unittest import mock

sys.modules.setdefault("webview", types.SimpleNamespace(start=None, create_window=None))

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

import rawi_app  # noqa: E402


BASE = {
    "hold_key": "ctrl_l",
    "language": "auto",
    "model": "large-v3-turbo",
    "clean": {"enabled": False, "level": "full", "tone": "formal"},
    "dictionary": {"names": ["Yousef"], "pairs": ["teh => the"]},
}


class HubSettings(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.NamedTemporaryFile("w", suffix=".json", delete=False, encoding="utf-8")
        json.dump(BASE, self.tmp, ensure_ascii=False)
        self.tmp.close()
        self.cfg = self.tmp.name
        self.addCleanup(os.unlink, self.cfg)
        patcher = mock.patch.object(rawi_app, "CFG", self.cfg)
        patcher.start()
        self.addCleanup(patcher.stop)
        restarter = mock.patch.object(rawi_app.Api, "restart_daemon", return_value=1)
        restarter.start()
        self.addCleanup(restarter.stop)

    def save(self, patch):
        rawi_app.Api().save_config(patch)
        with open(self.cfg, encoding="utf-8") as f:
            return json.load(f)

    def test_saving_names_does_not_erase_pairs(self):
        after = self.save({"names": ["Yousef", "Abdulrahman"]})
        self.assertEqual(after["dictionary"]["names"], ["Yousef", "Abdulrahman"])
        self.assertEqual(after["dictionary"]["pairs"], ["teh => the"])

    def test_saving_pairs_does_not_erase_names(self):
        after = self.save({"pairs": ["teh => the", "recieve => receive"]})
        self.assertEqual(after["dictionary"]["names"], ["Yousef"])
        self.assertEqual(len(after["dictionary"]["pairs"]), 2)

    def test_a_dictionary_save_touches_nothing_else(self):
        after = self.save({"names": ["Yousef", "Abdulrahman"]})
        self.assertEqual(after["hold_key"], "ctrl_l")
        self.assertEqual(after["model"], "large-v3-turbo")
        self.assertFalse(after["clean"]["enabled"])
        self.assertEqual(after["clean"]["level"], "full")

    def test_the_value_that_passes_the_check_is_the_value_written(self):
        after = self.save({"hold_key": "  ctrl_r  "})
        self.assertEqual(after["hold_key"], "ctrl_r")

    def test_blank_or_missing_never_clobbers(self):
        after = self.save({"hold_key": None, "model": "", "language": "   "})
        self.assertEqual(after["hold_key"], "ctrl_l")
        self.assertEqual(after["model"], "large-v3-turbo")
        self.assertEqual(after["language"], "auto")


class MaxSecondsSave(unittest.TestCase):
    """The caps are three numbers but one setting: a partial save must not unlock or overwrite.

    These are hardening for callers other than the hub's own page, which rejects bad input itself.
    """

    def setUp(self):
        self.tmp = tempfile.NamedTemporaryFile("w", suffix=".json", delete=False, encoding="utf-8")
        json.dump(BASE, self.tmp, ensure_ascii=False)
        self.tmp.close()
        self.cfg = self.tmp.name
        self.addCleanup(os.unlink, self.cfg)
        patcher = mock.patch.object(rawi_app, "CFG", self.cfg)
        patcher.start()
        self.addCleanup(patcher.stop)
        restarter = mock.patch.object(rawi_app.Api, "restart_daemon", return_value=1)
        restarter.start()
        self.addCleanup(restarter.stop)

    def save(self, patch, current=None):
        if current is not None:
            with open(self.cfg, "w", encoding="utf-8") as f:
                json.dump(dict(BASE, max_seconds=current), f, ensure_ascii=False)
        rawi_app.Api().save_config(patch)
        with open(self.cfg, encoding="utf-8") as f:
            return json.load(f)["max_seconds"]

    def test_a_partial_patch_keeps_the_other_caps(self):
        got = self.save({"max_seconds": {"toggle": 240}},
                        current={"hold": 0, "toggle": 180, "latch": 45})
        self.assertEqual(got, {"hold": 0.0, "toggle": 240.0, "latch": 45.0})

    def test_a_plain_number_lifts_into_three_boxes_without_capping_hold(self):
        got = self.save({"max_seconds": {"hold": 0, "toggle": 600, "latch": 600}}, current=600)
        self.assertEqual(got, {"hold": 0.0, "toggle": 600.0, "latch": 600.0})

    def test_true_is_not_one_second(self):
        got = self.save({"max_seconds": {"hold": True, "toggle": 240, "latch": 180}},
                        current={"hold": 90, "toggle": 180, "latch": 180})
        self.assertEqual(got["hold"], 90.0)
        self.assertEqual(got["toggle"], 240.0)

    def test_infinity_is_not_a_cap(self):
        got = self.save({"max_seconds": {"hold": 0, "toggle": "1e999", "latch": 180}}, current=180)
        self.assertEqual(got, {"hold": 0.0, "toggle": 180.0, "latch": 180.0})
        self.assertNotIn("Infinity", open(self.cfg, encoding="utf-8").read().replace("\u221e", ""))

    def test_a_bad_value_neither_unlocks_nor_raises(self):
        got = self.save({"max_seconds": {"hold": "x", "toggle": "y", "latch": "z"}},
                        current={"hold": "bad", "toggle": 300, "latch": "bad"})
        self.assertEqual(got["toggle"], 300.0)
        self.assertEqual(got["hold"], 0.0)
        self.assertEqual(got["latch"], 180.0)

    def test_a_scalar_patch_keeps_the_stored_number_when_unreadable(self):
        got = self.save({"max_seconds": "abc"}, current=600)
        self.assertEqual(got, 600.0)


if __name__ == "__main__":
    unittest.main()
