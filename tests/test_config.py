"""rawi_core.config: defaults, merging, first-run creation, saving."""
import json
import os
import tempfile
import unittest

import _helpers  # noqa: F401
from rawi_core import config


class TestDefaults(unittest.TestCase):
    def test_defaults_are_complete(self):
        for key in ("hold_key", "mode", "model", "compute_type", "device", "language", "paste",
                    "paste_key", "lang_key", "talk_key", "listen", "clean", "dictionary"):
            self.assertIn(key, config.DEFAULTS)

    def test_default_hotkeys_do_not_collide(self):
        d = config.DEFAULTS
        keys = [d["hold_key"], d["paste_key"], d["lang_key"], d["talk_key"], d["listen"]["key"]]
        self.assertEqual(len(keys), len(set(keys)))

    def test_no_personal_data_ships_in_defaults(self):
        # a guard against shipping someone's personal config: no names, no terms, no history
        self.assertEqual(config.DEFAULTS["dictionary"], {"names": [], "pairs": []})
        self.assertFalse(config.DEFAULTS["log"])
        self.assertFalse(config.DEFAULTS["clean"]["enabled"])

    def test_device_defaults_to_auto(self):
        self.assertEqual(config.DEFAULTS["device"], "auto")

    def test_example_config_only_uses_known_keys(self):
        with open(os.path.join(_helpers.ROOT, "config.example.json"), encoding="utf-8") as f:
            example = json.load(f)
        self.assertEqual(set(example) - set(config.DEFAULTS), set())


class TestMerge(unittest.TestCase):
    def test_partial_section_keeps_the_defaults_it_does_not_mention(self):
        # the bug this fixes: {"clean": {"enabled": true}} used to lose system/endpoint
        cfg = config.merged({"clean": {"enabled": True}})
        self.assertTrue(cfg["clean"]["enabled"])
        self.assertEqual(cfg["clean"]["endpoint"], config.DEFAULTS["clean"]["endpoint"])
        self.assertTrue(cfg["clean"]["system"])

    def test_user_values_win(self):
        cfg = config.merged({"model": "small", "listen": {"key": "f6"}})
        self.assertEqual(cfg["model"], "small")
        self.assertEqual(cfg["listen"]["key"], "f6")
        self.assertEqual(cfg["listen"]["silence_ms"], 700)

    def test_lists_replace_rather_than_merge(self):
        cfg = config.merged({"profiles": []})
        self.assertEqual(cfg["profiles"], [])

    def test_merging_never_mutates_the_defaults(self):
        cfg = config.merged({})
        cfg["listen"]["key"] = "x"
        cfg["dictionary"]["names"].append("someone")
        self.assertEqual(config.DEFAULTS["listen"]["key"], "f8")
        self.assertEqual(config.DEFAULTS["dictionary"]["names"], [])

    def test_unknown_keys_survive(self):
        self.assertEqual(config.merged({"arabic_prompt": "x"})["arabic_prompt"], "x")


class TestFiles(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.path = os.path.join(self.dir.name, "config.json")

    def tearDown(self):
        self.dir.cleanup()

    def test_load_creates_the_file_on_first_run(self):
        cfg = config.load(self.path)
        self.assertTrue(os.path.exists(self.path))
        self.assertEqual(cfg["model"], config.DEFAULTS["model"])

    def test_load_without_create_leaves_the_disk_alone(self):
        config.load(self.path, create=False)
        self.assertFalse(os.path.exists(self.path))

    def test_unreadable_file_gives_defaults_and_is_not_overwritten(self):
        with open(self.path, "w", encoding="utf-8") as f:
            f.write("{ not json")
        cfg = config.load(self.path)
        self.assertEqual(cfg["hold_key"], "ctrl_l")
        with open(self.path, encoding="utf-8") as f:
            self.assertEqual(f.read(), "{ not json")

    def test_save_round_trips_and_keeps_arabic_readable(self):
        self.assertTrue(config.save({"dictionary": {"names": ["راوي"]}}, self.path))
        with open(self.path, encoding="utf-8") as f:
            raw = f.read()
        self.assertIn("راوي", raw)
        self.assertEqual(config.read_user(self.path)["dictionary"]["names"], ["راوي"])

    def test_read_user_is_empty_for_missing_or_odd_files(self):
        self.assertEqual(config.read_user(self.path), {})
        with open(self.path, "w", encoding="utf-8") as f:
            f.write("[1, 2]")
        self.assertEqual(config.read_user(self.path), {})

    def test_save_to_an_impossible_path_returns_false(self):
        self.assertFalse(config.save({}, os.path.join(self.dir.name, "missing", "config.json")))


if __name__ == "__main__":
    unittest.main(verbosity=2)
