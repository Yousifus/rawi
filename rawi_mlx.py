"""Whisper on the Apple GPU (MLX), behind the two faster-whisper calls rawi makes.

On an Apple-silicon Mac there is no CUDA, and CTranslate2 runs Whisper on the CPU: about 16 s a
line for large-v3-turbo on an M1 Pro. MLX runs the same model on the Mac's GPU in about 2.5 s.
Optional: `pip install mlx-whisper`, then rawi picks it on its own
(config "engine": "auto"), or force it with "engine": "mlx" / "ctranslate2".

Not a full port: MLX has no beam search, so beam_size is ignored and decoding is greedy. On 28 Saudi
dialect sentences (M1 Pro) that cost a few words: 12.7% wrong (4-bit) and 10.9% (fp16) against 9.7%
for CTranslate2 with beam 5, and greedy decoding sometimes turns dialect into formal Arabic.
The silence trim reuses faster-whisper's Silero VAD, which rawi already installs.
"""
from __future__ import annotations

import platform
import sys
from types import SimpleNamespace

import numpy as np

SR = 16000
REPOS = {
    # 4-bit by default: 0.46 GB instead of 1.6 GB, at a small cost in words (12.7% vs 10.9% wrong).
    # On a 16 GB Mac under memory pressure the full model gets swapped out between lines and
    # every line waits for it to page back in. "large-v3-turbo-fp16" keeps the full weights.
    "large-v3-turbo": "mlx-community/whisper-large-v3-turbo-q4",
    "turbo": "mlx-community/whisper-large-v3-turbo-q4",
    "large-v3-turbo-fp16": "mlx-community/whisper-large-v3-turbo",
    "large-v3": "mlx-community/whisper-large-v3-mlx",
    "medium": "mlx-community/whisper-medium-mlx",
    "small": "mlx-community/whisper-small-mlx",
    "base": "mlx-community/whisper-base-mlx",
    "tiny": "mlx-community/whisper-tiny-mlx",
}


def available() -> bool:
    """Apple silicon with mlx-whisper installed."""
    if sys.platform != "darwin" or platform.machine() != "arm64":
        return False
    try:
        import mlx_whisper  # noqa: F401
        return True
    except Exception:
        return False


def speech_only(audio: np.ndarray, vad_parameters: dict | None) -> np.ndarray:
    """Cut the silences out, as faster-whisper's vad_filter does, so Whisper never hears them."""
    from faster_whisper.vad import VadOptions, collect_chunks, get_speech_timestamps
    chunks = get_speech_timestamps(audio, VadOptions(**(vad_parameters or {})))
    if not chunks:
        return np.array([], dtype=np.float32)
    return np.concatenate(collect_chunks(audio, chunks)[0])


class MlxWhisper:
    """Duck-types the parts of faster_whisper.WhisperModel that rawi calls."""

    def __init__(self, name: str):
        import mlx.core as mx
        from mlx_whisper.transcribe import ModelHolder
        self.repo = REPOS.get(name, name)
        self.dtype = mx.float16
        self.model = ModelHolder.get_model(self.repo, self.dtype)   # downloads once, then cached
        # The first real decode builds the GPU kernels (~12 s); pay it here, not on the first line.
        # Silence is not enough: it decodes to nothing and skips the work, so use a hum.
        hum = (0.1 * np.sin(np.linspace(0, 2 * np.pi * 220 * 2, 2 * SR))).astype(np.float32)
        self.detect_language(hum)
        speech_only(hum, None)                       # loads Silero VAD (~1.7 s the first time)
        for lang in ("en", "ar"):
            self.transcribe(hum, language=lang, initial_prompt="warm up")

    def detect_language(self, audio: np.ndarray, language_detection_segments: int = 1):
        from mlx_whisper.audio import N_FRAMES, N_SAMPLES, log_mel_spectrogram, pad_or_trim
        mel = log_mel_spectrogram(audio, n_mels=self.model.dims.n_mels, padding=N_SAMPLES)
        _, probs = self.model.detect_language(pad_or_trim(mel, N_FRAMES, axis=-2).astype(self.dtype))
        allp = sorted(probs.items(), key=lambda kv: kv[1], reverse=True)
        return allp[0][0], float(allp[0][1]), allp

    def transcribe(self, audio: np.ndarray, language=None, task="transcribe", initial_prompt=None,
                   beam_size=None, condition_on_previous_text=False, without_timestamps=True,
                   vad_filter=False, vad_parameters=None):
        import mlx_whisper
        duration = len(audio) / SR
        if vad_filter:
            audio = speech_only(audio, vad_parameters)
        if len(audio) < SR // 10:                    # nothing left to hear
            return [], SimpleNamespace(language=language, duration=duration)
        r = mlx_whisper.transcribe(audio, path_or_hf_repo=self.repo, language=language, task=task,
                                   initial_prompt=initial_prompt,
                                   condition_on_previous_text=condition_on_previous_text,
                                   without_timestamps=without_timestamps, verbose=None,
                                   # The default fallback re-decodes a low-confidence line up to 5
                                   # more times, which dialect triggers often. Retry once, and only
                                   # for a repetition loop ("وممممم..."), caught by compression ratio.
                                   temperature=(0.0, 0.4), logprob_threshold=None)
        segs = [SimpleNamespace(text=s["text"]) for s in r.get("segments") or []]
        return segs, SimpleNamespace(language=r.get("language"), duration=duration)
