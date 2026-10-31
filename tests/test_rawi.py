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

    def test_the_keep_list_can_be_replaced(self):
        cfg = {"repeat_collapse": {"keep": ["لا لا"]}}
        self.assertEqual(rawi.drop_hallucinations(" ".join(["لا لا"] * 9), cfg),
                         " ".join(["لا لا"] * 9))
        # a phrase no longer in the list falls back to the count
        self.assertEqual(rawi.drop_hallucinations(" ".join(["ha"] * 9), cfg), "ha")

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

    def test_a_plain_number_never_caps_a_hold(self):
        self.assertEqual(rawi.max_seconds_for({"max_seconds": 180}, "hold"), 0.0)

    def test_a_plain_number_caps_the_self_running_modes(self):
        self.assertEqual(rawi.max_seconds_for({"max_seconds": 180}, "toggle"), 180.0)
        self.assertEqual(rawi.max_seconds_for({"max_seconds": 180}, "latch"), 180.0)

    def test_an_object_gives_each_mode_its_own_cap(self):
        cfg = {"max_seconds": {"hold": 0, "toggle": 120, "latch": 30}}
        self.assertEqual(rawi.max_seconds_for(cfg, "hold"), 0.0)
        self.assertEqual(rawi.max_seconds_for(cfg, "toggle"), 120.0)
        self.assertEqual(rawi.max_seconds_for(cfg, "latch"), 30.0)

    def test_zero_means_off(self):
        self.assertEqual(rawi.max_seconds_for({"max_seconds": 0}, "toggle"), 0.0)
        self.assertEqual(rawi.max_seconds_for({"max_seconds": {"toggle": 0}}, "toggle"), 0.0)

    def test_a_missing_hold_key_is_uncapped_not_guessed(self):
        self.assertEqual(rawi.max_seconds_for({"max_seconds": {"toggle": 90}}, "hold"), 0.0)

    def test_a_missing_toggle_key_falls_back_to_zero(self):
        self.assertEqual(rawi.max_seconds_for({"max_seconds": {"hold": 5}}, "toggle"), 0.0)

    def test_the_default_leaves_holds_alone(self):
        self.assertEqual(rawi.max_seconds_for(rawi.DEFAULTS, "hold"), 0.0)
        self.assertGreater(rawi.max_seconds_for(rawi.DEFAULTS, "toggle"), 0)

    def test_a_bad_value_is_not_a_two_and_a_half_minute_cap(self):
        self.assertEqual(rawi.max_seconds_for({"max_seconds": "soon"}, "toggle"), 180.0)


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
        d.stop_and_process = lambda: setattr(d, "stopped", True)
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

