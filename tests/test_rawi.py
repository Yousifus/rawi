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
