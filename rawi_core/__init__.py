"""rawi_core — the library behind rawi's entry-point scripts.

The scripts at the repository root (`rawi.py`, `rawi_app.py`, `rawi_dock.py`, `stats.py`,
`voice_out.py`, `launch_voice.py`) are what you run. Everything they share lives here, one
concern per module, so each piece can be read, reused and tested on its own.

Module map (see docs/CODE_INDEX.md for the full index):

    paths       every file rawi reads or writes, in one place
    config      DEFAULTS, load / save config.json (nested sections merged with defaults)
    system      the system adapter: every Windows / macOS / Linux difference lives here
    text        pure text logic: normalisation, fidelity, snippets, dictionary, prompts
    mood        one emoji per dictation, earned by the words said
    ear         the always-on listener's voice-activity detector (pure numpy)
    hotkeys     the hold / tap / double-tap state machine for the hold key
    ipc         loopback JSON-lines client and server (daemon <-> hub / dock)
    voice       client for the optional local TTS server (voice mode)
    gpu         CUDA probing, the optional GPU gate, device choice, model loading
    history     transcripts.jsonl: append, load, and small shared helpers
    analytics   the numbers the hub and stats.html show, computed from history rows
    overlay     the pill: a small Tk capsule shown while you speak
    doctor      `python rawi.py --doctor`: one line per check, for a stranger's machine

Rules that keep this package healthy:

1. No `sys.platform` checks outside `system.py`. Need a new OS-specific behaviour? Add a
   method to the adapter there.
2. Heavy or optional imports (faster_whisper, sounddevice, pynput, pyautogui, tkinter, webview)
   happen inside functions, never at module import time, so the unit tests run with numpy only.
3. Optional parts fail open: a missing pill, cleaner or voice server must never cost the user a
   dictation.
"""
