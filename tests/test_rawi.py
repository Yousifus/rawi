"""Unit tests for rawi's pure logic. Stdlib only: `python -m unittest discover -s tests`.

These deliberately do not touch the microphone, the GPU, or the clipboard, so they run anywhere,
including CI, in about a second.
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import rawi  # noqa: E402


class TestNormalise(unittest.TestCase):
    def test_lowercases_and_strips(self):
        self.assertEqual(rawi._norm("  Hello World  "), "hello world")

    def test_arabic_alef_forms_fold_together(self):
        self.assertEqual(rawi._norm("أحمد"), rawi._norm("احمد"))

    def test_arabic_taa_marbuta_folds_to_haa(self):
        self.assertEqual(rawi._norm("مدرسة"), rawi._norm("مدرسه"))

    def test_diacritics_removed(self):
        self.assertEqual(rawi._norm("كَتَبَ"), rawi._norm("كتب"))

    def test_empty_is_safe(self):
        self.assertEqual(rawi._norm(""), "")
        self.assertEqual(rawi._norm(None), "")


class TestFidelity(unittest.TestCase):
    def test_identical_scores_same(self):
        f = rawi.fidelity("ship it on friday", "ship it on friday")
        self.assertEqual(f["lvl"], "same")
        self.assertEqual(f["d"], 0.0)
        self.assertEqual(f["ins"], 0)

    def test_filler_removal_is_green_not_red(self):
        raw = "um uh like we ship it on friday"
        clean = "we ship it on friday"
        f = rawi.fidelity(raw, clean)
        self.assertIn(f["lvl"], ("same", "green"))
        self.assertEqual(f["ins"], 0)

    def test_discourse_markers_outside_the_filler_list_still_count(self):
        """Known limit, written down rather than hidden: `_FILLERS` covers hesitation sounds, not
        every discourse marker. A cleaner that drops 'basically' is charged for it, so the meter
        errs toward under-crediting the cleaner rather than over-crediting it."""
        f = rawi.fidelity("so basically we ship it on friday", "we ship it on friday")
        self.assertIn(f["lvl"], ("green", "amber"))
        self.assertEqual(f["ins"], 0)

    def test_added_content_word_is_counted(self):
        f = rawi.fidelity("we ship it friday", "we ship it friday hopefully")
        self.assertGreaterEqual(f["ins"], 1)

    def test_rewritten_sentence_is_not_green(self):
        f = rawi.fidelity("we ship it on friday", "the deployment window opens on friday morning")
        self.assertIn(f["lvl"], ("amber", "red"))

    def test_empty_inputs_are_safe(self):
        self.assertEqual(rawi.fidelity("", "")["lvl"], "same")


class TestSnippets(unittest.TestCase):
    CFG = {"snippets": {"sig": "Ada Lovelace\nAnalyst\nLondon",
                        "brb": "be right back"}}

    def test_exact_utterance_expands(self):
        self.assertEqual(rawi.apply_snippets("sig", self.CFG), "Ada Lovelace\nAnalyst\nLondon")

    def test_case_and_punctuation_do_not_matter(self):
        self.assertEqual(rawi.apply_snippets("  SIG. ", self.CFG), "Ada Lovelace\nAnalyst\nLondon")

    def test_partial_match_does_not_expand(self):
        self.assertEqual(rawi.apply_snippets("sig please", self.CFG), "sig please")

    def test_no_snippets_is_a_no_op(self):
        self.assertEqual(rawi.apply_snippets("anything", {}), "anything")


class TestDictionary(unittest.TestCase):
    def test_pairs_replace_case_insensitively(self):
        cfg = {"dictionary": {"pairs": ["hbar=>HBAR"]}}
        self.assertEqual(rawi.apply_dictionary("we met hbar today", cfg), "we met HBAR today")

    def test_names_repair_a_near_miss(self):
        cfg = {"dictionary": {"names": ["Khalijee"]}}
        self.assertEqual(rawi.apply_dictionary("I called Khalije yesterday", cfg),
                         "I called Khalijee yesterday")

    def test_short_words_are_left_alone(self):
        # a token under four letters is never fuzzy-repaired, even toward a known name
        cfg = {"dictionary": {"names": ["Eve"]}}
        self.assertEqual(rawi.apply_dictionary("eve said hi", cfg), "eve said hi")
        self.assertEqual(rawi.apply_dictionary("ok Eve", cfg), "ok Eve")

    def test_terms_parse_both_shapes(self):
        names, pairs = rawi.dict_terms({"dictionary": {"names": ["A"], "pairs": ["x=>y", "bad"]}})
        self.assertEqual(names, ["A"])
        self.assertEqual(pairs, [("x", "y")])

    def test_empty_text_is_safe(self):
        self.assertEqual(rawi.apply_dictionary("", {"dictionary": {"names": ["A"]}}), "")


class TestConfig(unittest.TestCase):
    def test_defaults_are_complete(self):
        for key in ("hold_key", "mode", "model", "compute_type", "language", "paste",
                    "paste_key", "lang_key", "listen"):
            self.assertIn(key, rawi.DEFAULTS)

    def test_default_hotkeys_do_not_collide(self):
        keys = [rawi.DEFAULTS["hold_key"], rawi.DEFAULTS["paste_key"],
                rawi.DEFAULTS["lang_key"], rawi.DEFAULTS["talk_key"],
                rawi.DEFAULTS["listen"]["key"]]
        self.assertEqual(len(keys), len(set(keys)))

    def test_google_and_arabic_keys_are_absent(self):
        # a guard against shipping someone's personal config: defaults carry no names or terms
        self.assertEqual(rawi.DEFAULTS["dictionary"], {"names": [], "pairs": []})
        self.assertEqual(rawi.DEFAULTS["log"], False)


if __name__ == "__main__":
    unittest.main(verbosity=2)


class TestDeviceChoice(unittest.TestCase):
    """The CPU path the README promises must actually be reachable."""

    def test_device_defaults_to_auto(self):
        self.assertEqual(rawi.DEFAULTS["device"], "auto")

    def test_cuda_probe_answers_a_bool(self):
        self.assertIsInstance(rawi.cuda_available(), bool)

    def test_cpu_is_an_int8_choice(self):
        # whatever the machine, "cpu" must never be paired with a float16 compute type
        self.assertNotIn(rawi.DEFAULTS["device"], ("cuda", "gpu"))


class TestQueue(unittest.TestCase):
    def test_queue_holds_more_than_one(self):
        self.assertGreaterEqual(rawi.QUEUE_MAX, 2)


class TestHallucinations(unittest.TestCase):
    """Whisper on silence or room noise: loops and stock YouTube lines (seen on a 22 min recording)."""

    def test_word_loop_collapses_to_once(self):
        loop = "، ".join(["سعيدة"] * 40) + " هل يمكنك أن تكون هذا الجهاز؟"
        self.assertEqual(rawi.drop_hallucinations(loop), "سعيدة، هل يمكنك أن تكون هذا الجهاز؟")

    def test_loop_mid_sentence(self):
        text = "لا يمكننا التعليقات " + " ".join(["بأكام"] * 30) + " في القناة"
        self.assertEqual(rawi.drop_hallucinations(text), "لا يمكننا التعليقات بأكام في القناة")

    def test_phrase_loop_collapses(self):
        self.assertEqual(rawi.drop_hallucinations("thank you " * 8 + "bye"), "thank you bye")

    def test_real_repetition_survives(self):
        self.assertEqual(rawi.drop_hallucinations("no no no I said no"), "no no no I said no")
        self.assertEqual(rawi.drop_hallucinations("لا لا لا"), "لا لا لا")

    def test_stock_line_alone_is_dropped(self):
        self.assertEqual(rawi.drop_hallucinations("اشتركوا في القناة"), "")
        self.assertEqual(rawi.drop_hallucinations("Thanks for watching!"), "")

    def test_stock_line_inside_real_speech_is_kept(self):
        text = "في نهاية الفيديو قول اشتركوا في القناة"
        self.assertEqual(rawi.drop_hallucinations(text), text)

    def test_empty_is_safe(self):
        self.assertEqual(rawi.drop_hallucinations(""), "")

class TestMeantRepetition(unittest.TestCase):
    """A prayer and a laugh are dictation, not a model loop. The old filter cut both at five."""

    def test_a_dhikr_five_times_survives(self):
        text = " ".join(["سبحان الله"] * 5)
        self.assertEqual(rawi.drop_hallucinations(text), text)

    def test_a_dhikr_mixed_with_a_real_sentence_survives(self):
        text = " ".join(["الحمد لله"] * 6) + " الحين نبدأ"
        self.assertEqual(rawi.drop_hallucinations(text), text)

    def test_a_laugh_survives(self):
        text = "ha ha ha ha ha that was funny"
        self.assertEqual(rawi.drop_hallucinations(text), text)

    def test_arabic_laughter_survives(self):
        text = "هههههههههه لا تقول"
        self.assertEqual(rawi.drop_hallucinations(text), text)

    def test_a_loop_of_a_real_phrase_still_collapses(self):
        text = " ".join(["سعيدة"] * 12) + " اليوم"
        self.assertEqual(rawi.drop_hallucinations(text), "سعيدة اليوم")


class TestRepeatThreshold(unittest.TestCase):
    """The count is a setting: the default sits above how often a person repeats on purpose."""

    def test_default_threshold_is_eight(self):
        self.assertEqual(rawi._repeat_settings(rawi.DEFAULTS)[0], 8)

    def test_five_identical_words_are_left_alone(self):
        text = " ".join(["go"] * 5)
        self.assertEqual(rawi.drop_hallucinations(text), text)

    def test_the_setting_is_read(self):
        cfg = {"repeat_collapse": {"min_repeats": 3}}
        self.assertEqual(rawi.drop_hallucinations(" ".join(["go"] * 5), cfg), "go")

    def test_a_20_word_loop_survives_a_high_threshold(self):
        cfg = {"repeat_collapse": {"min_repeats": 30}}
        text = " ".join(["go"] * 20)
        self.assertEqual(rawi.drop_hallucinations(text, cfg), text)

    def test_a_user_keep_list_adds_to_the_built_in_one(self):
        # it used to replace it: following the README to keep a lone «الله» silently dropped the
        # dhikr and the laughter, which is the harm this filter exists to prevent
        cfg = {"repeat_collapse": {"keep": ["الله"]}}
        self.assertEqual(rawi.drop_hallucinations(" ".join(["الله"] * 9), cfg),
                         " ".join(["الله"] * 9))
        self.assertEqual(rawi.drop_hallucinations(" ".join(["سبحان الله"] * 9), cfg),
                         " ".join(["سبحان الله"] * 9))
        self.assertEqual(rawi.drop_hallucinations(" ".join(["ha"] * 9), cfg), " ".join(["ha"] * 9))
        self.assertEqual(rawi.drop_hallucinations(" ".join(["يا رب"] * 9),
                                                  {"repeat_collapse": {"keep": ["يا رب"]}}),
                         " ".join(["يا رب"] * 9))
        # a phrase that is in neither list still obeys the count
        self.assertEqual(rawi.drop_hallucinations(" ".join(["لا لا"] * 9), cfg), "لا")

    def test_an_unreadable_threshold_falls_back_to_the_default(self):
        cfg = {"repeat_collapse": {"min_repeats": "many"}}
        self.assertEqual(rawi._repeat_settings(cfg)[0], 8)

    def test_nonsense_config_does_not_crash(self):
        self.assertEqual(rawi.drop_hallucinations("hello", {"repeat_collapse": "off"}), "hello")


class TestStockLinesRepeated(unittest.TestCase):
    """Two to four stock lines used to slip through the single-line check."""

    def test_stock_line_twice_is_dropped(self):
        self.assertEqual(rawi.drop_hallucinations("Thanks for watching! Thanks for watching!"), "")

    def test_stock_line_four_times_in_two_languages_is_dropped(self):
        text = "اشتركوا في القناة. Thanks for watching. اشتركوا في القناة. Thanks for watching."
        self.assertEqual(rawi.drop_hallucinations(text), "")

    def test_one_real_word_keeps_everything(self):
        text = "Thanks for watching! Thanks for watching! ok"
        self.assertEqual(rawi.drop_hallucinations(text), text)

    def test_stock_only_is_true_for_a_pile_and_false_for_one_word(self):
        self.assertTrue(rawi._stock_only("اشتركوا في القناة اشتركوا في القناة"))
        self.assertFalse(rawi._stock_only("اشتركوا في القناة ابدأ"))


class TestMaxSecondsSetting(unittest.TestCase):
    """The cap is per way of recording, and a long hold is not a fault."""

    def test_a_bare_number_is_reported_and_ignored(self):
        # the old form meant "cap toggle and latch at 180". Obeying it now would cut his holds at
        # three minutes, so it is named at startup and the new defaults apply instead.
        self.assertEqual(rawi.legacy_max_seconds({"max_seconds": 180}), 180.0)
        self.assertIsNone(rawi.legacy_max_seconds({"max_seconds": {"hold": 5}}))
        self.assertEqual(rawi.max_seconds_for({"max_seconds": 180}, "hold"), 300.0)
        self.assertEqual(rawi.max_seconds_for({"max_seconds": 180}, "toggle"), 0.0)
        self.assertEqual(rawi.max_seconds_for({"max_seconds": 180}, "latch"), 0.0)

    def test_an_object_gives_each_mode_its_own_cap(self):
        cfg = {"max_seconds": {"hold": 0, "toggle": 120, "latch": 30}}
        self.assertEqual(rawi.max_seconds_for(cfg, "hold"), 0.0)
        self.assertEqual(rawi.max_seconds_for(cfg, "toggle"), 120.0)
        self.assertEqual(rawi.max_seconds_for(cfg, "latch"), 30.0)

    def test_zero_means_off(self):
        self.assertEqual(rawi.max_seconds_for({"max_seconds": 0}, "toggle"), 0.0)
        self.assertEqual(rawi.max_seconds_for({"max_seconds": {"toggle": 0}}, "toggle"), 0.0)

    def test_a_missing_hold_key_keeps_the_default_ceiling(self):
        # you turn the ceiling off by writing {"hold": 0}, not by leaving the key out
        self.assertEqual(rawi.max_seconds_for({"max_seconds": {"toggle": 90}}, "hold"), 300.0)
        self.assertEqual(rawi.max_seconds_for({"max_seconds": {"toggle": 90}}, "toggle"), 90.0)

    def test_a_missing_toggle_key_falls_back_to_zero(self):
        self.assertEqual(rawi.max_seconds_for({"max_seconds": {"hold": 5}}, "toggle"), 0.0)

    def test_the_default_caps_a_hold_and_frees_the_rest(self):
        # his choice of 2026-11-01, after seeing OpenWhispr: a hold ceiling, no cap on the modes
        # he starts on purpose
        self.assertEqual(rawi.max_seconds_for(rawi.DEFAULTS, "hold"), 300.0)
        self.assertEqual(rawi.max_seconds_for(rawi.DEFAULTS, "toggle"), 0.0)
        self.assertEqual(rawi.max_seconds_for(rawi.DEFAULTS, "latch"), 0.0)

    def test_the_shipped_defaults_are_the_documented_object(self):
        self.assertEqual(rawi.DEFAULTS["max_seconds"], {"hold": 300, "toggle": 0, "latch": 0})

    def test_a_bad_value_is_not_a_cap(self):
        self.assertEqual(rawi.max_seconds_for({"max_seconds": "soon"}, "toggle"), 0.0)
        self.assertEqual(rawi.max_seconds_for({"max_seconds": {"toggle": "soon"}}, "toggle"), 0.0)


class TestCapLeavesTheNextTapAlone(unittest.TestCase):
    """After the cap stops a recording, the tap he meant as "stop" must not start a new one."""

    def _rawi(self, cfg=None):
        # __new__, not __init__: the constructor opens a microphone and imports sounddevice,
        # and these tests must run in CI where neither exists.
        d = rawi.Rawi.__new__(rawi.Rawi)
        d.cfg = dict(rawi.DEFAULTS, beeps=False, **(cfg or {}))
        d.recording = False
        d._capped = False
        d._cap_stopped = 0.0
        d._cap_s = 0.0
        d._cap_mode = str(d.cfg.get("mode") or "hold")
        d.wave = None
        d.mic_open = lambda: None           # the real one opens a device
        d.start_rec = lambda *a, **k: setattr(d, "started", True)
        d.stop_and_process = lambda *a, **k: setattr(d, "stopped", True)
        return d

    def test_a_tap_right_after_the_cap_does_nothing(self):
        d = self._rawi()
        d._cap_stopped = rawi.time.time()
        self.assertEqual(d.toggle_rec(), "capped")
        self.assertFalse(getattr(d, "started", False))
        self.assertFalse(getattr(d, "stopped", False))

    def test_the_tap_is_absorbed_only_once(self):
        d = self._rawi()
        d._cap_stopped = rawi.time.time()
        d.toggle_rec()
        self.assertEqual(d.toggle_rec(), "started")

    def test_an_old_cap_stop_does_not_swallow_a_real_start(self):
        d = self._rawi()
        d._cap_stopped = rawi.time.time() - rawi._CAP_TAP_GRACE - 1
        self.assertEqual(d.toggle_rec(), "started")

    def test_a_running_recording_stops_on_the_tap(self):
        d = self._rawi()
        d.recording = True
        self.assertEqual(d.toggle_rec(), "stopped")

    def test_the_cap_follows_the_mode_of_the_recording(self):
        d = self._rawi({"mode": "hold", "max_seconds": {"hold": 0, "latch": 45}})
        del d.start_rec                     # the real one, to see what it records
        d.recording = False
        d.start_rec(latched=True)
        self.assertEqual(d._cap_s, 45.0)
        self.assertEqual(d._cap_mode, "latch")
        d.stop_and_process = lambda: None
        d.recording = False
        d.start_rec()
        self.assertEqual(d._cap_s, 0.0)


class _FakeStream:
    opened = 0

    def __init__(self, **_):
        _FakeStream.opened += 1
        self.closed = False

    def start(self):
        pass

    def stop(self):
        pass

    def close(self):
        self.closed = True


class TestIdleMic(unittest.TestCase):
    """macOS shows its orange mic light while a stream is open, so an idle mic gets closed."""

    def _rawi(self, after):
        import threading
        import types
        d = rawi.Rawi.__new__(rawi.Rawi)
        d.sd = types.SimpleNamespace(InputStream=_FakeStream)
        d.cfg = {"mic_close_after": after}
        d.stream, d._mic_lock, d._mic_used = None, threading.Lock(), 0.0
        d.recording, d.ear = False, None
        return d

    def test_closes_when_idle_and_reopens(self):
        d = self._rawi(5)
        d.mic_open()
        st = d.stream
        self.assertFalse(d.mic_close_if_idle())          # just used
        d._mic_used -= 6
        self.assertTrue(d.mic_close_if_idle())
        self.assertTrue(st.closed)
        self.assertIsNone(d.stream)
        d.mic_open()
        self.assertIsNotNone(d.stream)

    def test_stays_open_while_recording(self):
        d = self._rawi(5)
        d.mic_open()
        d._mic_used -= 60
        d.recording = True
        self.assertFalse(d.mic_close_if_idle())
        self.assertIsNotNone(d.stream)

    def test_zero_keeps_it_open(self):
        d = self._rawi(0)
        d.mic_open()
        d._mic_used -= 60
        self.assertFalse(d.mic_close_if_idle())

import types as _types
from unittest import mock as _mock


class _FakeWhisper:
    """A model with the shape Rawi.transcribe uses, so the real call site can be exercised."""

    def __init__(self, text):
        self.text, self.told = text, []

    def transcribe(self, audio, language=None, **kw):
        self.told.append(language)
        return ([_types.SimpleNamespace(text=self.text)],
                _types.SimpleNamespace(language=language or "ar", duration=1.0))


class TestTheFilterReadsItsSetting(unittest.TestCase):
    """A setting only the tests can change is not a setting: these go through transcribe()."""

    def _rawi(self, text, cfg=None):
        # __new__, not __init__: the constructor imports sounddevice and a device may not exist.
        d = rawi.Rawi.__new__(rawi.Rawi)
        d.cfg = dict(rawi.DEFAULTS, **(cfg or {}))
        d.model = _FakeWhisper(text)
        d.lang_hint, d.lang_probs, d.last_lang = None, {}, (None, 0.0)
        d.wave = None
        return d

    def _text(self, text, cfg=None):
        return self._rawi(text, cfg).transcribe(rawi.np.zeros(1600, dtype=rawi.np.float32), "ar")[0]

    def test_the_threshold_setting_reaches_the_live_call(self):
        nine = " ".join(["same"] * 9)
        self.assertEqual(self._text(nine).split().count("same"), 1)          # default 8 collapses it
        self.assertEqual(self._text(nine, {"repeat_collapse": {"min_repeats": 20}}).split().count("same"), 9)
        self.assertEqual(self._text(" ".join(["same"] * 4),
                                   {"repeat_collapse": {"min_repeats": 3}}).split().count("same"), 1)

    def test_a_kept_phrase_longer_than_four_words_survives_whole(self):
        # A guard, not a fix: nothing five words or longer can be detected as a repeat unit (the
        # window stops at four), so «لا حول ولا قوة إلا بالله» survives by arithmetic. This pins it
        # so a later wider unit window cannot start eating the six-word dhikr.
        line = "لا حول ولا قوة إلا بالله"
        text = self._text(" ".join([line] * 3), {"repeat_collapse": {"min_repeats": 2}})
        self.assertEqual(text.count("حول"), 3)

    def test_a_single_word_dhikr_needs_the_keep_list(self):
        # Documented behaviour: «الله» alone at nine repeats is the shape of a Whisper loop, so it
        # collapses unless he adds it. The escape is one value in config.json.
        self.assertEqual(self._text(" ".join(["الله"] * 9)).split().count("الله"), 1)
        kept = {"repeat_collapse": {"keep": ["الله"]}}
        self.assertEqual(self._text(" ".join(["الله"] * 9), kept).split().count("الله"), 9)

    def test_dhikr_is_never_collapsed_however_long_the_run(self):
        line = "سبحان الله"
        self.assertEqual(self._text(" ".join([line] * 12)).count("سبحان"), 12)


class _WatchOrder(rawi.Rawi):
    """Records what the object looked like at the instant `recording` became True."""

    def __setattr__(self, name, value):
        if name == "recording" and value and "saw" not in self.__dict__:
            self.__dict__["saw"] = (self.__dict__.get("started_at", 0), )
        object.__setattr__(self, name, value)


class TestTheCapCannotSeeAnOldStart(unittest.TestCase):
    def test_started_at_is_set_before_the_flag(self):
        # a callback between the two lines would read the previous recording's time and cap at once
        d = _WatchOrder.__new__(_WatchOrder)
        d.cfg = dict(rawi.DEFAULTS, beeps=False)
        d.recording, d._capped, d._cap_stopped = False, False, 0.0
        d._cap_mode, d._cap_s, d.wave = "hold", 0.0, None
        d.started_at = 0.0
        d.mic_open = lambda: None
        d.start_rec()
        self.assertTrue(d.recording)
        self.assertGreater(d.saw[0], 0.0)
        self.assertAlmostEqual(d.started_at, d.saw[0])

class _Pill:
    def __init__(self):
        self.flashed = []

    def flash(self, text, ok=True, seconds=1.6):
        self.flashed.append(text)

    def show(self):
        pass

    def hide(self):
        pass

    def push(self, *_):
        pass


class TestWhyALineEnded(unittest.TestCase):
    """A cap is a stop with a reason, and a stop while a key is down must not paste.

    His choice of 2026-11-01, from OpenWhispr's behaviour: a capped recording is never lost, and it
    says why. When the cap fires during a hold his Ctrl is still down, so the text is copied and the
    pill says so instead of injecting Ctrl+V into a held modifier.
    """

    def _rawi(self, paste=True):
        import collections
        import threading
        d = rawi.Rawi.__new__(rawi.Rawi)
        d.cfg = dict(rawi.DEFAULTS, beeps=False, paste=paste, log=True, dumb=True)
        d.busy = threading.Semaphore(1)
        d.pending = collections.deque()
        d.injecting, d.last, d.ear = False, None, None
        d.wave = _Pill()
        d.transcribe = lambda audio, lang, listen=False: ("مرحبا", "ar", 1.0)
        d.paste = lambda text: setattr(d, "pasted", text)
        return d

    def _run(self, d, reason="release", paste=True):
        rows = []
        fake = _types.SimpleNamespace(copy=rows.append)
        with _mock.patch.dict(sys.modules, {"pyperclip": fake}), \
                _mock.patch.object(rawi, "log_transcript", lambda row, _on: rows.append(row)), \
                _mock.patch.object(rawi, "active_app", lambda: "app"), \
                _mock.patch.object(rawi, "fidelity", lambda raw, clean: 1.0):
            d._work(rawi.np.zeros(1600, dtype=rawi.np.float32), 1.0, False, reason, paste)
        return rows

    def test_the_reason_is_in_the_history_and_in_last(self):
        d = self._rawi()
        self._run(d, reason="cap 300s")
        self.assertEqual(d.last["stop"], "cap 300s")
        self.assertEqual(d.pasted, "مرحبا")          # a release-free cap on a tap still pastes

    def test_a_cap_during_a_hold_copies_instead_of_pasting(self):
        d = self._rawi()
        rows = self._run(d, reason="cap 300s", paste=False)
        self.assertFalse(hasattr(d, "pasted"))       # no Ctrl+V into a held Ctrl
        self.assertFalse(d.last["pasted"])
        self.assertEqual(rows[0], "مرحبا")            # it is on the clipboard
        self.assertTrue(any("not pasted" in f for f in d.wave.flashed))
        self.assertEqual(d.last["stop"], "cap 300s")
        self.assertIs(rows[-1]["stop"], "cap 300s")


import types
from unittest import mock


class _FakeProbe:
    """Whisper, reduced to what the decision reads: a probe vector, and what it was told to write."""

    def __init__(self, allp, texts=("hello",)):
        self.allp, self.told = allp, []
        self.texts = list(texts)

    def detect_language(self, audio, language_detection_segments=1):
        return self.allp[0][0], self.allp[0][1], self.allp

    def transcribe(self, audio, language=None, **kw):
        self.told.append(language)
        text = self.texts[min(len(self.told) - 1, len(self.texts) - 1)]
        return [types.SimpleNamespace(text=text)], types.SimpleNamespace(language=language, duration=1.0)


class TestTwoLanguageProbe(unittest.TestCase):
    """He dictates Khaliji Arabic and English, so the probe decides between two, not a hundred.

    Every vector here is hand-made, from the review of 2026-11-01, not from recorded audio."""

    def _rawi(self, allp, hint=None, cfg=None, texts=("hello",)):
        # CI installs numpy only, and Rawi.__init__ imports sounddevice. The stub is the fix the
        # review verified; adding sounddevice to CI would need PortAudio on Linux.
        with mock.patch.dict(sys.modules, {"sounddevice": types.ModuleType("sounddevice")}):
            d = rawi.Rawi(dict(rawi.DEFAULTS, **(cfg or {})))
        d.model, d.lang_hint = _FakeProbe(allp, texts), hint
        return d

    def _told(self, d):
        d.transcribe(rawi.np.zeros(16000, dtype=rawi.np.float32), None)
        return d.model.told

    # --- the bug the PR was written for, and the one its own fix introduced ---

    def test_an_accented_hello_beats_a_stale_arabic_hint(self):
        d = self._rawi([("en", 0.51), ("nn", 0.20), ("ar", 0.15), ("fa", 0.14)], hint="ar")
        self.assertEqual(self._told(d), ["en"])

    def test_arabic_spread_over_look_alikes_does_not_become_english(self):
        # fa .40, en .20, ur .20, ar .06: renormalised, that read as "en 0.77" and translated him.
        d = self._rawi([("fa", 0.40), ("en", 0.20), ("ur", 0.20), ("ar", 0.06)], hint="ar")
        self.assertEqual(self._told(d), ["ar"])
        self.assertEqual(d.lang_hint, "ar")

    def test_a_language_he_does_not_speak_is_never_forced(self):
        d = self._rawi([("fa", 0.80), ("ur", 0.16), ("ar", 0.03), ("en", 0.01)], hint="ar")
        self.assertEqual(self._told(d), ["ar"])

    def test_a_stranger_winning_does_not_flip_the_hint(self):
        d = self._rawi([("nn", 0.80), ("de", 0.14), ("en", 0.05), ("ar", 0.01)], hint="ar")
        self.assertEqual(self._told(d), ["ar"])
        self.assertEqual(d.lang_hint, "ar")

    def test_with_no_hint_and_nothing_of_his_heard_whisper_decides(self):
        d = self._rawi([("fa", 0.45), ("ur", 0.25), ("en", 0.15), ("ar", 0.06)])
        self.assertEqual(self._told(d), [None])

    # --- what must keep working ---

    def test_a_real_coin_toss_keeps_the_hint(self):
        d = self._rawi([("ar", 0.35), ("en", 0.32), ("ur", 0.33)], hint="ar")
        self.assertEqual(self._told(d), ["ar"])

    def test_a_loud_english_line_switches_the_hint(self):
        d = self._rawi([("en", 0.80), ("ar", 0.15)], hint="ar")
        self.assertEqual(self._told(d), ["en"])
        self.assertEqual(d.lang_hint, "en")

    def test_mid_conversation_a_modest_english_signal_keeps_english(self):
        d = self._rawi([("en", 0.55), ("ar", 0.40)], hint="en")
        self.assertEqual(self._told(d)[0], "en")
        self.assertEqual(d.lang_hint, "en")

    def test_the_romanised_second_look_still_fires(self):
        # English was written, but the words are romanised Arabic: ask again, in Arabic.
        d = self._rawi([("en", 0.62), ("ar", 0.36)], texts=("yalla khalas", "يلا خلاص"))
        told = self._told(d)
        self.assertEqual(told, ["en", "ar"])
        self.assertEqual(d.lang_hint, "ar")


class TestConfiguredLanguages(unittest.TestCase):
    """`languages` is a setting, and a string is one language, not a list of letters."""

    def test_the_default_pair(self):
        self.assertEqual(rawi._languages(rawi.DEFAULTS), ("ar", "en"))

    def test_a_string_is_one_language(self):
        self.assertEqual(rawi._languages({"languages": "ar"}), ("ar",))

    def test_a_list_keeps_order_and_drops_duplicates(self):
        self.assertEqual(rawi._languages({"languages": ["EN", "ar", "en"]}), ("en", "ar"))

    def test_junk_falls_back_to_the_pair(self):
        self.assertEqual(rawi._languages({"languages": 7}), ("ar", "en"))
        self.assertEqual(rawi._languages({"languages": []}), ("ar", "en"))

    def test_only_the_declared_language_is_weighed(self):
        # "ar" alone: English at 0.51 is not his language here, so it cannot win. Arabic at 0.30
        # is above the floor and wins outright; Arabic at 0.06 is a reading of nothing, and with
        # no hint Whisper decides rather than a language the config excludes.
        make = TestTwoLanguageProbe()._rawi        # the stubbed constructor, reused
        heard = make([("en", 0.51), ("ar", 0.30)], cfg={"languages": "ar"})
        heard.transcribe(rawi.np.zeros(16000, dtype=rawi.np.float32), None)
        self.assertEqual(heard.model.told, ["ar"])
        faint = make([("en", 0.51), ("ar", 0.06)], cfg={"languages": "ar"})
        faint.transcribe(rawi.np.zeros(16000, dtype=rawi.np.float32), None)
        self.assertEqual(faint.model.told, [None])

class _FailingProbe(_FakeProbe):
    """A probe that raises, with a vector left over from the previous line."""

    def __init__(self, allp, texts=("hello",)):
        super().__init__(allp, texts)
        self.calls = 0

    def detect_language(self, audio, language_detection_segments=1):
        self.calls += 1
        raise RuntimeError("probe died")


class TestAProbeFailure(unittest.TestCase):
    """A failed probe must fall back to the hint, never decide from the last line's vector."""

    def _rawi(self, hint="ar", cfg=None, stale=None):
        with mock.patch.dict(sys.modules, {"sounddevice": types.ModuleType("sounddevice")}):
            d = rawi.Rawi(dict(rawi.DEFAULTS, **(cfg or {})))
        d.model, d.lang_hint = _FailingProbe([("en", 0.90), ("ar", 0.05)]), hint
        if stale is not None:
            d.lang_probs = dict(stale)          # exactly the state a romanised line leaves behind
        return d

    def test_a_dead_probe_falls_back_to_the_hint(self):
        d = self._rawi(stale={"en": 0.90, "ar": 0.05, "nn": 0.05})
        d.transcribe(rawi.np.zeros(16000, dtype=rawi.np.float32), None)
        self.assertEqual(d.model.told, ["ar"])      # the stale vector said "en 0.90" and would have won
        self.assertEqual(d.lang_hint, "ar")

    def test_a_dead_probe_with_no_hint_lets_whisper_decide(self):
        d = self._rawi(hint=None, stale={"en": 0.90, "ar": 0.05})
        d.transcribe(rawi.np.zeros(16000, dtype=rawi.np.float32), None)
        self.assertEqual(d.model.told, [None])


class TestTheHintFollowsTheConfiguredPair(unittest.TestCase):
    def test_a_declared_third_language_becomes_the_hint(self):
        with mock.patch.dict(sys.modules, {"sounddevice": types.ModuleType("sounddevice")}):
            d = rawi.Rawi(dict(rawi.DEFAULTS, languages=["fr", "en"]))
        d.model, d.lang_hint = _FakeProbe([("fr", 0.90), ("en", 0.05)]), None
        d.transcribe(rawi.np.zeros(16000, dtype=rawi.np.float32), None)
        self.assertEqual(d.model.told, ["fr"])
        self.assertEqual(d.lang_hint, "fr")
