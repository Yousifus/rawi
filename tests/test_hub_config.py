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


if __name__ == "__main__":
    unittest.main()
