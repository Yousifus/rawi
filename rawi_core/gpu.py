"""Where Whisper runs: CUDA probing, the optional GPU gate, device choice and model loading.

Device choice is measured, not assumed: `"device": "auto"` means "CUDA when ctranslate2 can
actually see a device", a config value of "cpu" forces int8, and a failed CUDA load falls back
to CPU instead of exiting.

    cuda_available()                    -> bool
    choose_device(cfg, cuda, gate)      -> (device, compute_type, reason)   pure, unit-tested
    load_model(name, device, ctype)     -> WhisperModel, with the CUDA -> CPU fallback chain
    gpu_gate() / gate_requires_yield()  optional: share one GPU politely with other jobs
"""
from __future__ import annotations

import json
import sys
from typing import Callable

from .system import current

# Optional: path to a script that prints {"holder": {"owner": ..., "purpose": ...}} when another
# program has claimed the GPU. Empty = disabled (the default): nothing is ever spawned.
GATE_PY = ""
GATE_YIELD_PURPOSES = ("exclusive", "training", "render")


def cuda_available() -> bool:
    """True only when a CUDA device is actually usable here (no driver, no dice)."""
    try:
        import ctranslate2
        return int(ctranslate2.get_cuda_device_count()) > 0
    except Exception:
        return False


def cuda_device_count() -> int:
    try:
        import ctranslate2
        return int(ctranslate2.get_cuda_device_count())
    except Exception:
        return 0


def gpu_gate() -> dict | None:
    """Who else is using this GPU, if anyone. None when free, unreadable, or unconfigured."""
    if not GATE_PY:
        return None                       # unconfigured: never spawn a doomed subprocess per poll
    try:
        out = current().run_hidden([sys.executable, GATE_PY, "status"],
                                   capture_output=True, text=True, timeout=20)
        for line in reversed(out.stdout.strip().splitlines()):
            line = line.strip()
            if line.startswith("{"):
                return (json.loads(line) or {}).get("holder")
    except Exception:
        return None
    return None


def gate_requires_yield(holder: dict | None) -> bool:
    """True when the current holder should not share the card with a transcriber."""
    purpose = str((holder or {}).get("purpose") or "").lower()
    return any(w in purpose for w in GATE_YIELD_PURPOSES)


def choose_device(cfg: dict, *, cuda: bool,
                  gate: Callable[[], dict | None] = gpu_gate) -> tuple[str, str, str]:
    """(device, compute_type, reason) for the configured model.

    reason is "" when nothing needed explaining, else the line the daemon prints.
    `gate` is only consulted when CUDA would otherwise be used.
    """
    want = str(cfg.get("device") or "auto").lower()
    if want == "cpu":
        return "cpu", "int8", "device: cpu (from config)"
    if want == "auto" and not cuda:
        return "cpu", "int8", "no CUDA device here -> CPU int8"
    holder = gate()
    if holder and gate_requires_yield(holder):
        return "cpu", "int8", (f"GPU busy with {holder.get('owner')} "
                               f"({holder.get('purpose')}) -> CPU int8")
    return "cuda", str(cfg.get("compute_type") or "float16"), ""


def load_model(name: str, device: str, compute_type: str, log=print):
    """Load faster-whisper's model, falling back CUDA float -> CUDA int8_float16 -> CPU int8.

    A CPU load failure is not hidden: it raises (there is nothing left to fall back to).
    """
    from faster_whisper import WhisperModel
    try:
        return WhisperModel(name, device=device, compute_type=compute_type)
    except Exception as e:
        log(f"[rawi] load failed ({type(e).__name__}: {e})")
        if device != "cuda":
            raise
    try:
        return WhisperModel(name, device="cuda", compute_type="int8_float16")
    except Exception as e2:
        log(f"[rawi] cuda int8_float16 failed ({type(e2).__name__}: {e2}) -> CPU int8")
        return WhisperModel(name, device="cpu", compute_type="int8")

