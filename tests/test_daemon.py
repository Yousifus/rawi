"""The daemon (rawi.Rawi) and the hub's Api, with the model, clipboard and OS faked out.

No microphone, GPU, network or keystrokes: the model is a stub that returns canned text, the
system adapter records the chords it was asked to send, and pyperclip is a stand-in module.
"""
import io
import json
import os
import sys
import tempfile
import types
import unittest
from unittest import mock

import numpy as np

import _helpers
from rawi_core import config, gpu, paths

import rawi  # noqa: E402  (the daemon script, imported as a module)


class Seg:
    def __init__(self, text):
        self.text = text


class Info:
    def __init__(self, language, duration=1.0):
        self.language = language
        self.duration = duration


class FakeModel:
    """faster-whisper's WhisperModel, reduced to what rawi calls."""

    def __init__(self, probe=("en", 0.9, [("en", 0.6), ("ar", 0.4)]), outputs=None):
        self.probe = probe
        self.outputs = outputs or {"en": "yalla let's go", "ar": "يلا نمشي", None: "mixed words"}
        self.calls = []

    def detect_language(self, audio, language_detection_segments=1):
        return self.probe

    def transcribe(self, audio, language=None, **kw):
        self.calls.append((language, kw))
        return iter([Seg(self.outputs.get(language, ""))]), Info(language or "en")


class FakeClipboard(types.ModuleType):
    def __init__(self):
        super().__init__("pyperclip")
        self.value = "what was there before"
        self.history = []

    def copy(self, text):
        self.value = text
        self.history.append(text)

    def paste(self):
        return self.value


class DaemonBase(unittest.TestCase):
    def setUp(self):
        cfg = config.merged({"beeps": False, "log": False, "paste": False})
        self.d = rawi.Rawi(cfg)
        self.plat = _helpers.FakePlatform(app="Notes")
        self.d.plat = self.plat
        self.d.model = FakeModel()
        self.clip = FakeClipboard()
        patches = [mock.patch.dict(sys.modules, {"pyperclip": self.clip}),
                   mock.patch.object(rawi.time, "sleep", lambda s: None),
                   mock.patch.object(rawi.config, "save", return_value=True),
                   mock.patch.object(rawi.voice, "post", return_value=False),
                   mock.patch("sys.stdout", new_callable=io.StringIO)]     # the daemon narrates
        for p in patches:
            p.start()
            self.addCleanup(p.stop)

    def audio(self, seconds=1.0):
        return np.zeros(int(rawi.SR * seconds), dtype=np.float32)


class TestTranscribe(DaemonBase):
    def test_romanised_arabic_gets_a_second_look(self):
        out, lang, _ = self.d.transcribe(self.audio(), None)
        self.assertEqual((out, lang), ("يلا نمشي", "ar"))
        self.assertEqual([c[0] for c in self.d.model.calls], ["en", "ar"])

    def test_a_locked_language_is_never_overridden(self):
        out, lang, _ = self.d.transcribe(self.audio(), "en")
        self.assertEqual((out, lang), ("yalla let's go", "en"))
        self.assertEqual(len(self.d.model.calls), 1)

    def test_arabic_uses_beam_search_and_an_arabic_prompt(self):
        self.d.transcribe(self.audio(), "ar")
        _lang, kw = self.d.model.calls[0]
        self.assertEqual(kw["beam_size"], 5)
        self.assertTrue(kw["initial_prompt"].startswith("نص عربي"))

    def test_the_language_sticks_mid_conversation(self):
        self.d.model.probe = ("ar", 0.95, [("ar", 0.95)])
        self.d.transcribe(self.audio(), None)
        self.assertEqual(self.d.lang_hint, "ar")
        self.d.model.probe = ("en", 0.7, [("en", 0.7), ("ar", 0.3)])   # unsure: below 0.85
        _out, lang, _ = self.d.transcribe(self.audio(), None)
        self.assertEqual(lang, "ar")

    def test_unsure_with_no_history_lets_whisper_decide_with_the_mixed_prompt(self):
        self.d.model.probe = ("en", 0.4, [("en", 0.4), ("ar", 0.35)])
        self.d.transcribe(self.audio(), None)
        lang, kw = self.d.model.calls[0]
        self.assertIsNone(lang)
        self.assertTrue(kw["initial_prompt"].startswith("Mixed Arabic and English"))

    def test_the_dictionary_is_applied_to_the_result(self):
        self.d.cfg["dictionary"] = {"pairs": ["yalla=>Yalla!"], "names": []}
        out, _, _ = self.d.transcribe(self.audio(), "en")
        self.assertTrue(out.startswith("Yalla!"))


class TestWork(DaemonBase):
    def test_held_recording_is_pasted_and_the_clipboard_restored(self):
        self.d.cfg["paste"] = True
        self.d._work(self.audio(), 1.0)
        self.assertEqual(self.plat.chords, [("ctrl", "v")])
        self.assertEqual(self.clip.history[0], "يلا نمشي")
        self.assertEqual(self.clip.value, "what was there before")
        self.assertTrue(self.d.last["pasted"])

    def test_the_ear_copies_but_never_pastes(self):
        self.d.cfg["paste"] = True
        self.d._work(self.audio(), 1.0, listen=True)
        self.assertEqual(self.plat.chords, [])
        self.assertEqual(self.clip.value, "يلا نمشي")
        self.assertFalse(self.d.last["pasted"])

    def test_the_language_lock_reaches_the_model(self):
        self.d.cfg["lang_lock"] = "en"
        self.d._work(self.audio(), 1.0)
        self.assertEqual(self.d.model.calls[0][0], "en")
        self.assertEqual(self.d.last["clean"], "yalla let's go")

    def test_snippets_expand_after_transcription(self):
        self.d.cfg["snippets"] = {"mixed words": "EXPANDED"}
        self.d.model.probe = ("en", 0.1, [])
        self.d._work(self.audio(), 1.0)
        self.assertEqual(self.d.last["clean"], "EXPANDED")

    def test_a_busy_model_queues_instead_of_dropping(self):
        self.d.busy.acquire()
        try:
            self.d._work(self.audio(), 1.0)
            self.assertEqual(len(self.d.pending), 1)
        finally:
            self.d.busy.release()

    def test_history_is_written_only_when_logging_is_on(self):
        with mock.patch.object(rawi.history, "append") as append:
            self.d._work(self.audio(), 1.0)
            self.assertFalse(append.call_args.args[1])
            self.d.cfg["log"] = True
            self.d._work(self.audio(), 1.0)
            entry, on = append.call_args.args
            self.assertTrue(on)
            self.assertEqual(entry["text"], "يلا نمشي")
            self.assertEqual(entry["app"], "Notes")


class TestClean(DaemonBase):
    def reply(self, content):
        body = json.dumps({"choices": [{"message": {"content": content}}]}).encode()
        resp = mock.MagicMock()
        resp.read.return_value = body
        resp.__enter__.return_value = resp
        return resp

    def test_off_by_default(self):
        self.assertEqual(self.d.clean("um hi"), ("um hi", "off"))

    def test_a_partial_clean_section_still_works(self):
        # what the hub writes: {"clean": {"enabled": true}} with no system/endpoint
        self.d.cfg = config.merged({"clean": {"enabled": True}, "beeps": False})
        with mock.patch("urllib.request.urlopen", return_value=self.reply("Hi.")) as urlopen:
            self.assertEqual(self.d.clean("um hi"), ("Hi.", "ok"))
        sent = json.loads(urlopen.call_args.args[0].data)
        self.assertIn("filler", sent["messages"][0]["content"])

    def test_profiles_can_switch_the_cleaner_off_per_app(self):
        self.d.cfg["clean"]["enabled"] = True
        self.assertEqual(self.d.clean("x", app="main.py - Visual Studio Code"), ("x", "off:Code"))

    def test_runaway_and_failures_keep_the_raw_words(self):
        self.d.cfg["clean"]["enabled"] = True
        with mock.patch("urllib.request.urlopen", return_value=self.reply("long " * 200)):
            self.assertEqual(self.d.clean("hi"), ("hi", "runaway"))
        with mock.patch("urllib.request.urlopen", side_effect=OSError("down")):
            self.assertEqual(self.d.clean("hi"), ("hi", "OSError"))


class TestCommands(DaemonBase):
    def test_status_shape(self):
        st = self.d.handle_command({})
        for key in ("ok", "recording", "busy", "dumb", "swap", "listening", "dropped", "lvl",
                    "floor", "heard", "peak", "keys", "ear"):
            self.assertIn(key, st)

    def test_toggle_starts_and_stops(self):
        self.assertTrue(self.d.handle_command({"cmd": "toggle"})["recording"])
        self.assertFalse(self.d.handle_command({"cmd": "toggle"})["recording"])

    def test_dumb_mode_flips_and_saves(self):
        self.assertTrue(self.d.handle_command({"cmd": "dumb"})["dumb"])
        self.assertFalse(self.d.handle_command({"cmd": "dumb", "on": False})["dumb"])
        self.assertEqual(rawi.config.save.call_count, 2)

    def test_swap_with_nothing_pasted(self):
        r = self.d.handle_command({"cmd": "swap"})
        self.assertEqual((r["ok"], r["error"]), (False, "nothing to undo"))

    def test_swap_undoes_and_pastes_the_raw_words(self):
        self.d.last = {"raw": "um hi", "clean": "Hi.", "app": "Notes", "pasted": True}
        r = self.d.handle_command({"cmd": "swap"})
        self.assertTrue(r["ok"])
        self.assertEqual(self.plat.chords, [("ctrl", "z"), ("ctrl", "v")])

    def test_swap_refuses_when_focus_moved(self):
        self.d.last = {"raw": "um hi", "clean": "Hi.", "app": "Mail", "pasted": True}
        self.assertIn("focus moved", self.d.handle_command({"cmd": "swap"})["error"])

    def test_listen_opens_the_ear(self):
        r = self.d.handle_command({"cmd": "listen", "on": True})
        self.assertTrue(r["listening"])
        self.assertFalse(self.d.handle_command({"cmd": "listen"})["listening"])

    def test_talk_reports_the_bridge(self):
        r = self.d.handle_command({"cmd": "talk", "on": True})
        self.assertEqual(r, {"ok": True, "talk": True, "bridge": False})

    def test_function_keys(self):
        self.d.cycle_lang_lock()
        self.assertEqual(self.d.cfg["lang_lock"], "ar")
        self.d.cycle_lang_lock()
        self.d.cycle_lang_lock()
        self.assertEqual(self.d.cfg["lang_lock"], "auto")
        self.d.toggle_listen()
        self.assertTrue(self.d.ear.on)
        self.assertFalse(self.d.paste_last())          # nothing to paste yet


class TestDeviceChoice(unittest.TestCase):
    """The CPU path the README promises must actually be reachable."""

    def test_auto_without_cuda_is_cpu_int8(self):
        self.assertEqual(gpu.choose_device({"device": "auto"}, cuda=False)[:2], ("cpu", "int8"))

    def test_cpu_forces_int8_even_with_float16_configured(self):
        cfg = {"device": "cpu", "compute_type": "float16"}
        self.assertEqual(gpu.choose_device(cfg, cuda=True)[:2], ("cpu", "int8"))

    def test_cuda_uses_the_configured_compute_type(self):
        cfg = {"device": "auto", "compute_type": "float16"}
        self.assertEqual(gpu.choose_device(cfg, cuda=True, gate=lambda: None), ("cuda", "float16", ""))

    def test_a_busy_gpu_yields(self):
        holder = {"owner": "trainer", "purpose": "training run"}
        dev, ctype, why = gpu.choose_device({"device": "auto"}, cuda=True, gate=lambda: holder)
        self.assertEqual((dev, ctype), ("cpu", "int8"))
        self.assertIn("trainer", why)

    def test_cuda_probe_answers_a_bool(self):
        self.assertIsInstance(gpu.cuda_available(), bool)

    def test_queue_holds_more_than_one(self):
        self.assertGreaterEqual(rawi.QUEUE_MAX, 2)


class TestHubConfig(unittest.TestCase):
    """rawi_app.Api.save_config / get_config against a temporary config.json."""

    def setUp(self):
        import rawi_app
        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)
        self.path = os.path.join(self.dir.name, "config.json")
        with open(self.path, "w", encoding="utf-8") as f:
            json.dump({"hold_key": "ctrl_l", "model": "small", "clean": {"enabled": False}}, f)
        for p in (mock.patch.object(paths, "CONFIG", self.path),
                  mock.patch.object(rawi_app.Api, "restart_daemon", return_value=1)):
            p.start()
            self.addCleanup(p.stop)
        self.api = rawi_app.Api()

    def saved(self):
        with open(self.path, encoding="utf-8") as f:
            return json.load(f)

    def test_a_partial_save_does_not_switch_the_cleaner_on(self):
        self.api.save_config({"talk": True})
        c = self.saved()
        self.assertTrue(c["talk"])
        self.assertFalse(c["clean"]["enabled"])
        self.assertEqual(c["model"], "small")

    def test_an_explicit_save_does(self):
        self.api.save_config({"clean": True, "level": "full", "names": ["راوي"]})
        c = self.saved()
        self.assertTrue(c["clean"]["enabled"])
        self.assertEqual(c["clean"]["level"], "full")
        self.assertEqual(c["dictionary"]["names"], ["راوي"])

    def test_get_config_answers_what_the_page_reads(self):
        c = self.api.get_config()
        self.assertTrue(c["ok"])
        self.assertFalse(c["clean"])
        self.assertEqual((c["talk"], c["mood"], c["model"], c["log"]), (False, "auto", "small", False))
        self.assertEqual(c["keys"], {"paste": "f9", "lang": "f10", "talk": "f7", "listen": "f8"})

    def test_a_blank_hold_key_is_ignored(self):
        # an empty value would silently kill dictation
        self.api.save_config({"hold_key": "  ", "model": ""})
        self.assertEqual((self.saved()["hold_key"], self.saved()["model"]), ("ctrl_l", "small"))

    def test_history_can_be_switched_on(self):
        self.api.save_config({"log": True})
        self.assertTrue(self.saved()["log"])

    def test_the_reply_carries_a_message(self):
        r = self.api.save_config({"talk": True})
        self.assertTrue(r["ok"])
        self.assertIn("restarted", r["message"])

    def test_an_unreadable_file_is_not_overwritten(self):
        with open(self.path, "w", encoding="utf-8") as f:
            f.write("{ broken")
        self.assertEqual(self.api.save_config({"talk": True}),
                         {"ok": False, "message": "Could not read config.json."})
        with open(self.path, encoding="utf-8") as f:
            self.assertEqual(f.read(), "{ broken")


if __name__ == "__main__":
    unittest.main(verbosity=2)
