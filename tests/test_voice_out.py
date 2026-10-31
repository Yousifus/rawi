"""Piper joins two voices onto one sample rate. Numpy only: no Piper download, no audio device.

`python -m unittest discover -s tests`
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import numpy as np

import voice_out  # noqa: E402


class _Chunk:
    def __init__(self, samples):
        self.audio_int16_array = samples


class _Voice:
    """One second of constant samples at a fixed native rate."""

    def __init__(self, sample_rate, seconds=1.0):
        self.config = type("Cfg", (), {"sample_rate": sample_rate})()
        self.calls = 0
        self._samples = np.full(int(round(sample_rate * seconds)), 8000, dtype=np.int16)

    def synthesize(self, _text):
        self.calls += 1
        yield _Chunk(self._samples)


class TestPiperRates(unittest.TestCase):
    def setUp(self):
        self._pipe = voice_out._pipe
        self._sr = voice_out.SR

    def tearDown(self):
        voice_out._pipe = self._pipe
        voice_out.SR = self._sr

    def test_two_rates_resample_to_the_higher_one(self):
        # 16 kHz beside 22.05 kHz used to be concatenated and played at one clock,
        # so one of the two runs came out the wrong length and pitch.
        ar = _Voice(16000)
        en = _Voice(22050)
        voice_out._pipe = {"ar": ar, "en": en}
        audio = voice_out.synth_piper("abc ااا")
        sr = voice_out.SR
        gap = int(sr * 0.12)
        self.assertEqual(ar.config.sample_rate, 16000)
        self.assertEqual(en.config.sample_rate, 22050)
        self.assertEqual(ar.calls, 1)
        self.assertEqual(en.calls, 1)
        self.assertEqual(sr, 22050)
        self.assertEqual(audio.dtype, np.float32)
        # one second from each voice, plus the 0.12 s breath after each run, at 22050 Hz
        self.assertEqual(len(audio), sr + gap + sr + gap)
