"""rawi_core.ear: the voice-activity gate, fed synthetic audio in 10 ms blocks."""
import unittest

import numpy as np

import _helpers  # noqa: F401
from rawi_core.ear import MAX_WAITING, SR, Ear

BLOCK = SR // 100                     # 10 ms, like a typical sounddevice callback
RNG = np.random.default_rng(7)


def quiet(seconds):
    return RNG.normal(0.0, 0.0003, int(SR * seconds)).astype(np.float32)


def voice(seconds, amp=0.08, hz=220.0):
    t = np.arange(int(SR * seconds)) / SR
    return (amp * np.sin(2 * np.pi * hz * t)).astype(np.float32)


class Base(unittest.TestCase):
    LISTEN = {"silence_ms": 700, "min_s": 0.45, "max_s": 25}

    def setUp(self):
        self.segments = []
        self.ear = Ear(get_cfg=lambda: self.LISTEN, on_segment=self.segments.append, start_worker=False)

    def feed(self, *pieces):
        audio = np.concatenate(pieces)
        for i in range(0, len(audio), BLOCK):
            self.ear.feed(audio[i:i + BLOCK].reshape(-1, 1))   # (frames, channels), as sounddevice


class TestSentences(Base):
    def test_one_spoken_sentence_becomes_one_segment(self):
        self.feed(quiet(1.0), voice(1.0), quiet(1.0))
        self.assertEqual(self.ear.emits, 1)
        seg = self.ear.q.get_nowait()
        self.assertGreaterEqual(len(seg) / SR, 1.0)    # all the speech, plus pre-roll and the tail
        self.assertFalse(self.ear.talking)

    def test_the_pre_roll_keeps_the_start_of_the_first_word(self):
        # the gate opens only after 0.22 s of voice; without the pre-roll that onset would be
        # cut off and the segment would hold ~0.78 s of the 1.0 s spoken
        self.feed(quiet(1.0), voice(1.0), quiet(1.0))
        seg = self.ear.q.get_nowait()
        voiced_seconds = np.count_nonzero(np.abs(seg) > 0.01) / SR
        self.assertGreater(voiced_seconds, 0.85)

    def test_a_click_does_not_open_the_gate(self):
        self.feed(quiet(1.0), voice(0.1), quiet(1.0))
        self.assertEqual((self.ear.emits, self.ear.short), (0, 0))

    def test_too_little_voice_is_counted_as_short(self):
        self.feed(quiet(1.0), voice(0.3), quiet(1.0))  # opens the gate, but < min_s of voice
        self.assertEqual((self.ear.emits, self.ear.short), (0, 1))

    def test_two_sentences_with_a_pause_become_two_segments(self):
        self.feed(quiet(1.0), voice(1.0), quiet(1.0), voice(1.0), quiet(1.0))
        self.assertEqual(self.ear.emits, 2)

    def test_the_floor_learns_the_room_but_not_the_voice(self):
        self.feed(quiet(2.0))
        floor_quiet = self.ear.floor
        self.assertLess(floor_quiet, 0.001)
        self.feed(voice(1.0), quiet(1.0))
        self.assertLess(self.ear.floor, 0.01)          # speech never became "the room"


class TestLimits(Base):
    LISTEN = {"silence_ms": 700, "min_s": 0.45, "max_s": 1.0}

    def test_max_length_cuts_a_long_sentence(self):
        self.feed(quiet(0.5), voice(1.5))
        self.assertEqual(self.ear.emits, 1)
        self.assertLessEqual(len(self.ear.q.get_nowait()) / SR, 1.0 + 0.2)


class TestBackpressure(Base):
    def test_a_slow_model_drops_the_oldest_waiting_sentence(self):
        for _ in range(MAX_WAITING + 2):
            self.feed(quiet(1.0), voice(1.0))
        self.feed(quiet(1.0))
        self.assertEqual(self.ear.emits, MAX_WAITING + 2)
        self.assertEqual(self.ear.dropped, 2)
        self.assertEqual(self.ear.q.qsize(), MAX_WAITING)


class TestRobustness(Base):
    def test_empty_and_odd_blocks_are_safe(self):
        self.ear.feed(np.zeros((0, 1), dtype=np.float32))
        self.ear.feed(np.zeros(5, dtype=np.float32))
        self.assertEqual(self.ear.errors, 0)

    def test_reset_forgets_a_half_sentence(self):
        self.feed(quiet(1.0), voice(0.5))
        self.assertTrue(self.ear.talking)
        self.ear.reset()
        self.assertFalse(self.ear.talking)
        self.assertEqual(self.ear.buf, [])


if __name__ == "__main__":
    unittest.main(verbosity=2)
