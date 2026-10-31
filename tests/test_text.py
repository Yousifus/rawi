"""rawi_core.text: normalisation, fidelity, snippets, dictionary, prompts, language checks."""
import unittest

import _helpers  # noqa: F401  (puts the repo root on sys.path)
from rawi_core import text


class TestNormalise(unittest.TestCase):
    def test_lowercases_and_strips(self):
        self.assertEqual(text.norm("  Hello World  "), "hello world")

    def test_arabic_alef_forms_fold_together(self):
        self.assertEqual(text.norm("أحمد"), text.norm("احمد"))

    def test_arabic_taa_marbuta_folds_to_haa(self):
        self.assertEqual(text.norm("مدرسة"), text.norm("مدرسه"))

    def test_diacritics_removed(self):
        self.assertEqual(text.norm("كَتَبَ"), text.norm("كتب"))

    def test_empty_is_safe(self):
        self.assertEqual(text.norm(""), "")
        self.assertEqual(text.norm(None), "")

    def test_count_arabic(self):
        self.assertEqual(text.count_arabic("hi مرحبا"), 5)
        self.assertEqual(text.count_arabic(""), 0)


class TestFidelity(unittest.TestCase):
    def test_identical_scores_same(self):
        f = text.fidelity("ship it on friday", "ship it on friday")
        self.assertEqual(f, {"d": 0.0, "ins": 0, "lvl": "same"})

    def test_filler_removal_is_green_not_red(self):
        f = text.fidelity("um uh like we ship it on friday", "we ship it on friday")
        self.assertIn(f["lvl"], ("same", "green"))
        self.assertEqual(f["ins"], 0)

    def test_discourse_markers_outside_the_filler_list_still_count(self):
        """Known limit, written down rather than hidden: FILLERS covers hesitation sounds, not
        every discourse marker. A cleaner that drops 'basically' is charged for it, so the meter
        errs toward under-crediting the cleaner rather than over-crediting it."""
        f = text.fidelity("so basically we ship it on friday", "we ship it on friday")
        self.assertIn(f["lvl"], ("green", "amber"))
        self.assertEqual(f["ins"], 0)

    def test_added_content_word_is_counted(self):
        f = text.fidelity("we ship it friday", "we ship it friday hopefully")
        self.assertGreaterEqual(f["ins"], 1)

    def test_rewritten_sentence_is_not_green(self):
        f = text.fidelity("we ship it on friday", "the deployment window opens on friday morning")
        self.assertIn(f["lvl"], ("amber", "red"))

    def test_empty_inputs_are_safe(self):
        self.assertEqual(text.fidelity("", "")["lvl"], "same")

    def test_arabic_filler_is_free(self):
        f = text.fidelity("يعني نشحنه يوم الجمعه", "نشحنه يوم الجمعه")
        self.assertIn(f["lvl"], ("same", "green"))


class TestSnippets(unittest.TestCase):
    CFG = {"snippets": {"sig": "Ada Lovelace\nAnalyst\nLondon", "brb": "be right back"}}

    def test_exact_utterance_expands(self):
        self.assertEqual(text.apply_snippets("sig", self.CFG), "Ada Lovelace\nAnalyst\nLondon")

    def test_case_and_punctuation_do_not_matter(self):
        self.assertEqual(text.apply_snippets("  SIG. ", self.CFG), "Ada Lovelace\nAnalyst\nLondon")

    def test_arabic_question_mark_is_trimmed(self):
        cfg = {"snippets": {"عنواني": "12 Baker St"}}
        self.assertEqual(text.apply_snippets("عنواني؟", cfg), "12 Baker St")

    def test_partial_match_does_not_expand(self):
        self.assertEqual(text.apply_snippets("sig please", self.CFG), "sig please")

    def test_no_snippets_is_a_no_op(self):
        self.assertEqual(text.apply_snippets("anything", {}), "anything")

    def test_empty_expansion_is_ignored(self):
        self.assertEqual(text.apply_snippets("sig", {"snippets": {"sig": "  "}}), "sig")


class TestDictionary(unittest.TestCase):
    def test_pairs_replace_case_insensitively(self):
        cfg = {"dictionary": {"pairs": ["hbar=>HBAR"]}}
        self.assertEqual(text.apply_dictionary("we met hbar today", cfg), "we met HBAR today")

    def test_pairs_replace_every_occurrence(self):
        cfg = {"dictionary": {"pairs": ["hbar=>HBAR"]}}
        self.assertEqual(text.apply_dictionary("hbar and Hbar", cfg), "HBAR and HBAR")

    def test_pair_replacement_is_literal_not_a_regex(self):
        cfg = {"dictionary": {"pairs": ["c++=>C++", "a.b=>A\\1"]}}
        self.assertEqual(text.apply_dictionary("i like c++ and a.b", cfg), "i like C++ and A\\1")

    def test_pairs_reach_inside_arabic_prefixes(self):
        # substring on purpose: "ال" + word must still be repaired
        cfg = {"dictionary": {"pairs": ["راوى=>راوي"]}}
        self.assertEqual(text.apply_dictionary("الراوى هنا", cfg), "الراوي هنا")

    def test_names_repair_a_near_miss(self):
        cfg = {"dictionary": {"names": ["Khalijee"]}}
        self.assertEqual(text.apply_dictionary("I called Khalije yesterday", cfg),
                         "I called Khalijee yesterday")

    def test_names_keep_surrounding_punctuation(self):
        cfg = {"dictionary": {"names": ["Khalijee"]}}
        self.assertEqual(text.apply_dictionary("call Khalije.", cfg), "call Khalijee.")

    def test_multi_word_names_repair(self):
        cfg = {"dictionary": {"names": ["Ada Lovelace"]}}
        self.assertEqual(text.apply_dictionary("ask ada lovelase now", cfg), "ask Ada Lovelace now")

    def test_short_words_are_left_alone(self):
        # a token under four letters is never fuzzy-repaired, even toward a known name
        cfg = {"dictionary": {"names": ["Eve"]}}
        self.assertEqual(text.apply_dictionary("eve said hi", cfg), "eve said hi")
        self.assertEqual(text.apply_dictionary("ok Eve", cfg), "ok Eve")

    def test_terms_parse_both_shapes(self):
        names, pairs = text.dict_terms({"dictionary": {"names": ["A"], "pairs": ["x=>y", "bad"]}})
        self.assertEqual(names, ["A"])
        self.assertEqual(pairs, [("x", "y")])

    def test_empty_text_is_safe(self):
        self.assertEqual(text.apply_dictionary("", {"dictionary": {"names": ["A"]}}), "")


class TestPrompts(unittest.TestCase):
    def test_arabic_prompt_prefers_arabic_names(self):
        p = text.prompt_for("ar", ["Ada", "راوي"])
        self.assertTrue(p.startswith(text.AR_PROMPT))
        self.assertIn("راوي", p)
        self.assertNotIn("Ada", p)

    def test_arabic_prompt_falls_back_to_all_names(self):
        self.assertIn("Ada", text.prompt_for("ar", ["Ada"]))

    def test_arabic_prompt_override(self):
        self.assertTrue(text.prompt_for("ar", [], "custom: ").startswith("custom: "))

    def test_english_and_mixed(self):
        self.assertTrue(text.prompt_for("en", ["Ada"]).startswith(text.EN_PROMPT))
        self.assertTrue(text.prompt_for(None, []).startswith(text.MIX_PROMPT))

    def test_other_languages_get_no_prompt(self):
        # an English prompt on French audio would steer it toward English
        self.assertIsNone(text.prompt_for("fr", ["Ada"]))


class TestRomanisedArabic(unittest.TestCase):
    def test_marker_triggers_a_second_look(self):
        retry, markers, _ = text.romanised_arabic_check("yalla khalas let's go", 0.1)
        self.assertTrue(retry)
        self.assertIn("yalla", markers)

    def test_strong_arabic_runner_up_with_few_english_words(self):
        retry, _, en_stop = text.romanised_arabic_check("shlonak zain", 0.5)
        self.assertTrue(retry)
        self.assertLessEqual(en_stop, 2)

    def test_plain_english_stays_english(self):
        retry, _, _ = text.romanised_arabic_check("we ship it on friday and that is that", 0.5)
        self.assertFalse(retry)

    def test_text_with_arabic_letters_never_retries(self):
        retry, _, _ = text.romanised_arabic_check("yalla يلا", 0.9)
        self.assertFalse(retry)


class TestProfilesAndTones(unittest.TestCase):
    PROFILES = [{"name": "Code", "apps": ["Visual Studio Code"], "level": "none"},
                {"name": "Chat", "apps": ["Slack"], "level": "light"}]

    def test_profile_matches_a_window_title_substring(self):
        self.assertEqual(text.profile_for("rawi.py - Visual Studio Code", self.PROFILES)["name"], "Code")
        self.assertEqual(text.profile_for("slack", self.PROFILES)["name"], "Chat")

    def test_no_match_or_no_title(self):
        self.assertEqual(text.profile_for("Notes", self.PROFILES), {})
        self.assertEqual(text.profile_for("", self.PROFILES), {})
        self.assertEqual(text.profile_for("Slack", None), {})

    def test_tone_lookup_ignores_case(self):
        # the cleaner lower-cases the tone; "veryCasual" used to match nothing
        self.assertEqual(text.tone_hint("verycasual"), text.TONES["veryCasual"])
        self.assertEqual(text.tone_hint("FORMAL"), text.TONES["formal"])
        self.assertEqual(text.tone_hint(None), "")
        self.assertEqual(text.tone_hint("nonsense"), "")


if __name__ == "__main__":
    unittest.main(verbosity=2)
