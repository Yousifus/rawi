# Verifying rawi on macOS

This is the ten-minute checklist that proves or breaks the macOS path. Report each step as
**pass / fail**, with the exact command and the exact error. Nothing here is destructive: no audio
leaves the machine, and history is off by default.

## Results so far

**2026-10-31 · macOS 27.0.1 · Apple Silicon (arm64) · Python 3.12 · CPU only · Accessibility not granted**

| step | result |
|---|---|
| 1. install (`pip install -r requirements.txt`) | pass: every wheel installs, no compiler needed |
| 2. `--doctor` | pass: `summary: ready`; warns no CUDA (correct) and that Accessibility is not granted |
| `--test` English (`base`) / Arabic (`small`) | pass: 0.6 s / 1.8 s; Arabic comes back in Arabic script |
| `--demo` (the pill) | pass, after the fix: it used to abort with `NSWindow should only be instantiated on the main thread` |
| live daemon: mic, IPC toggle → transcribe, ear on/off, dock process, single instance, Ctrl+C / SIGTERM cleanup | pass |
| 5. hub: opens, JS bridge boots in WKWebView, restarts and stops the daemon, copy | pass, after the fix: it used to crash at launch on `creationflags` |
| 3–4. hold key, F-keys, paste into TextEdit | **not yet verified**: needs Accessibility + Input Monitoring granted (the machine above had neither) |
| how the pill and dock look | **not yet verified**: the screen was locked during the run |

Bugs the run found and fixed: the pill crashing the daemon (Tk off the main thread), the hold key never
firing (pynput reports left Ctrl as `ctrl` on macOS), the hub crashing at launch (Windows-only process
flags), `launch_voice.py` crashing the same way, SIGTERM skipping cleanup (Tk replaces the handler),
and a stopped daemon counting as alive (zombie process).

## 0. What must be true first

- macOS 13 or newer, Apple silicon or Intel.
- Homebrew present (`brew --version`), Python 3.10–3.12 (`python3 --version`).
- **Accessibility permission** granted to the app that will own the keyboard listener
  (Terminal / iTerm / the Python binary). Without it the hotkeys fire but the paste does nothing —
  the most likely failure, and a silent one.
- **Microphone permission** granted to the same app on first record.

## 1. Install

```bash
git clone https://github.com/Yousifus/rawi.git && cd rawi
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp config.example.json config.json
```

Expected: `pip` builds `ctranslate2` and `faster-whisper` without a compiler. On Intel Macs, set
`"compute_type": "int8"` and `"model": "small"` in `config.json` — there is no CUDA, and `float16`
will fall back slowly or fail.

## 2. Doctor

```bash
python rawi.py --doctor
```

- `[1] interpreter` — ok
- `[2] packages` — all ok (pywebview/webview optional)
- `[3] microphone` — a device is listed **and** the 0.1 s open succeeds
- `[4] GPU` — expect `warn "no CUDA device visible"`. That is correct on a Mac; it is not a failure.
- `[5] model` — either downloaded or "will fetch on first run"
- `[7] paste path` — `macos: clipboard + cmd+v`, then either `Accessibility granted` or a warning
  telling you where to grant it
- exit code 0

```bash
python rawi.py --doctor --deep     # also loads the model (slow, ~1-2 min on CPU)
```

Pass = it loads and prints a load time.

## 3. The paste chord

The one call that matters most is `MacPlatform.send_chord(("command", "v"))` in
`rawi_core/system.py`, which is `pyautogui.hotkey("command", "v")`. Test the clipboard half in
isolation before trusting the daemon:

```bash
python -c "import pyautogui, pyperclip; pyperclip.copy('rawi paste test'); print('copied')"
```

Then focus TextEdit and press ⌘V by hand: the text appears. If it does, the clipboard half is fine and
any later failure is the synthetic keystroke (permissions), not the logic.

## 4. End to end

```bash
python rawi.py
```

The F-keys below are media keys on a Mac keyboard unless you hold `fn` (or switch the F-row to
standard function keys in System Settings → Keyboard).

1. Focus TextEdit, hold **left Ctrl**, say *"this is a rawi test"*, release.
2. Expected: the pill appears while speaking; the sentence is pasted at the cursor; the clipboard is
   restored to whatever it held before.
3. Double-tap left Ctrl, say a second sentence, tap once to stop: the latch works.
4. Press **F8** (the ear), speak a sentence, stop, wait for silence: **the text goes to the clipboard
   only** — it must not type itself into the window.
5. Press **⌘V**: the sentence appears.
6. Press **F9**: the last line is pasted again.
7. Press **F10** twice: the language lock cycles; say a short Arabic sentence with the lock on العربية
   and confirm the script is Arabic, not transliterated.

## 5. The hub

```bash
python rawi_app.py
```

Window opens, shows the Room, status reads "Daemon listening", History empty (log off). Two theme
button in the lower left flips paper/night. macOS uses WKWebView rather than WebView2: if the window is
blank, that is the finding — report macOS version and the console output.

There is no tray icon on macOS (pystray needs the main thread, which the window owns), so closing the
hub window closes the hub. The daemon keeps running and the hotkeys keep working. To stop a daemon the
hub started, run `pkill -f rawi.py`; one you started with `python rawi.py` stops with Ctrl+C in its
terminal.

## 6. The ear's thresholds

A Mac's built-in mic has a different noise floor. If the ear never fires, raise the floor by speaking
louder for calibration, or set `listen.min_s` to `0.6` and `silence_ms` to `900`.

## What to report back

For each numbered step: **pass / fail**, the macOS version, whether it is Apple silicon, and the exact
error text where a step failed. A short video of step 4 is worth more than a paragraph.
