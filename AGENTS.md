# Working on rawi — notes for coding agents (and humans)

rawi is hold-to-talk dictation: hold a key, speak Arabic or English, and the words are pasted
where the cursor is. Everything runs locally. The maintainer uses **Windows** daily; **macOS**
is supported through the system adapter and was verified on Apple Silicon (see
`docs/MACOS-TESTING.md`). Linux/X11 has code paths but no regular tester.

Read these first, in this order:

1. [`docs/CODE_INDEX.md`](docs/CODE_INDEX.md) — every file and public function, and "where to
   change X".
2. [`ARCHITECTURE.md`](ARCHITECTURE.md) — the processes, the path of one sentence, and why.

## Commands

```bash
python -m unittest discover -s tests      # all unit tests, ~0.5 s, no mic / GPU / clipboard
python rawi.py --doctor                   # checks this machine; add --deep to load the model
python rawi.py --test some.wav            # transcribe a file (needs the model; prints only)
python rawi.py --demo --seconds 4         # show the pill with fake audio
```

Set up once with `python -m venv .venv`, then `pip install -r requirements.txt` inside it.

## Rules every change must keep

1. **No `sys.platform` checks outside `rawi_core/system.py`.** Need an OS-specific behaviour?
   Add a method to `Platform` and override it in `WindowsPlatform` / `MacPlatform`. Never pass
   `creationflags`, call `taskkill`, `os.startfile`, `ctypes.windll` or `pythonw.exe` directly:
   they crash or do nothing on macOS. Use `plat.spawn`, `plat.kill`, `plat.open_path`,
   `plat.background_python()`.
2. **Tk on macOS lives on the main thread.** Creating a Tk window anywhere else aborts the
   whole process (`NSWindow should only be instantiated on the main thread`). The pill
   (`rawi_core/overlay.py`) handles this through `owns_main_thread` / `run()`; the dock is its own
   process with Tk on its main thread. Do not add a Tk window to the daemon any other way. On macOS
   the dock is replaced by the menu bar icon (`rawi_menubar.py`): clicking a Tk window there takes
   focus from the app being dictated into.
3. **Hotkey names go through `canonical_key()`.** pynput reports left Ctrl as `ctrl` on macOS
   (it shares a keycode with `ctrl`), so comparing raw config names breaks the hold key there.
4. **Fail open.** The pill, dock, menu bar, cleaner, voice server and tray are optional. Their failure must
   be printed and survived, never allowed to cost the user a dictation. A failed paste leaves the
   text on the clipboard.
5. **Keep `rawi_core` importable with numpy alone.** Import faster_whisper, sounddevice, pynput,
   pyautogui, tkinter, webview and pyobjc inside the functions that need them. CI installs only
   numpy.
6. **The `injecting` flag brackets every synthetic keystroke** (`Rawi.paste`, `Rawi.unpaste`),
   or rawi's own Ctrl+V re-triggers the hold key.
7. **`app.html` and `rawi_core/analytics.py` share a contract.** Reply shapes from `Api` must not
   change without changing the page.
8. **Privacy is a feature.** Audio stays in memory. History is off by default. The only network
   calls go to `127.0.0.1`. Do not add telemetry, update checks or cloud calls.
9. **Partial config writes are normal.** `Api.save_config` must touch only the keys in the patch.
   `config.load()` merges nested sections with `DEFAULTS`, so a partial section is fine.

## Style

- Python 3.10+ (`from __future__ import annotations` at the top of every module).
- Each module starts with a docstring that says what it is for and lists its public names.
  Keep it current when you add or rename something, and update `docs/CODE_INDEX.md`.
- Comments explain *why* (a past bug, an OS quirk, a tuned number). The code says *what*.
- Pure logic gets a unit test. Code that touches hardware gets a fake (`tests/_helpers.py`).
- stdlib `unittest` only. No new test dependencies.

## Verifying a change

1. `python -m unittest discover -s tests` passes.
2. `python rawi.py --doctor` still prints `summary: ready` on your machine.
3. If you touched the daemon's live path, the pill, the dock, the menu bar or the hub, run it and use it.
   Unit tests cannot see a window. On macOS, Terminal (or your IDE) needs Accessibility and
   Input Monitoring before hotkeys and paste work.
