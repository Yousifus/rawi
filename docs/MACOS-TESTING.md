# Verifying rawi on macOS

This is the ten-minute checklist that proves or breaks the macOS path. Report each step as
**pass / fail**, with the exact command and the exact error. Nothing here is destructive: no audio
leaves the machine, and history is off by default.

## Results so far

**2026-10-31 · MacBook Pro M1 Pro · macOS 15.1 · Python 3.12** — by Abdulrahman Aldosari, who made the
port work: the pill on the main thread, left Ctrl under its macOS name, the hub's Windows-only calls,
the single-instance guard, and a menu bar icon in place of the dock (a click on the Tk dock took focus
from the app being typed into, so the paste landed in the dock). Dictation tested end to end; `small`
took 2.5–3.5 s a line, `large-v3-turbo` about 10 s. The double-tap latch, F8, F9 and F10 were not tried.

**2026-10-31 · macOS 27.0.1 · Apple silicon · Python 3.12 · CPU · Accessibility not granted** — the
refactor onto the system adapter (`rawi_core/`), merged with the port above:

| step | result |
|---|---|
| 1. install (`pip install -r requirements.txt`) | pass: every wheel installs, no compiler needed |
| 2. `--doctor` | pass: `summary: ready`; warns no CUDA (correct) and that Accessibility is not granted |
| `--test` English (`base`) / Arabic (`small`) | pass: 0.6 s / 1.8 s; Arabic comes back in Arabic script |
| `--demo` (the pill) | pass |
| live daemon: mic, IPC toggle → transcribe, ear on/off, menu bar process, single instance | pass |
| quitting: Ctrl+C, SIGTERM (hub restart, menu bar Quit), closing the terminal window | pass: the daemon exits and removes `daemon.pid` |
| 5. hub: opens, JS bridge boots in WKWebView, restarts and stops the daemon, copy | pass |
| 3–4. hold key, F-keys, paste into TextEdit | **not verified on this machine**: needs Accessibility + Input Monitoring |
| how the pill and the menu bar icon look | **not verified on this machine**: the screen was locked |

Found on this run beyond the port: Tk's start-up on macOS replaces the SIGTERM handler, so a stopped
daemon skipped its cleanup; and a daemon the hub had stopped lingered as a zombie that still counted as
alive. Both are fixed.

## 0. What must be true first

- macOS 13 or newer, Apple silicon or Intel.
- Homebrew present (`brew --version`), Python 3.10–3.12 (`python3 --version`).
- **Accessibility** and **Input Monitoring** granted to the app that will own the keyboard listener
  (Terminal / iTerm / the Python binary). Without them the hotkeys or the paste do nothing — the
  most likely failure, and a silent one. `python rawi.py --doctor` warns when Accessibility is missing.
- **Microphone permission** granted to the same app on first record.

## 1. Install

```bash
git clone https://github.com/Yousifus/rawi.git && cd rawi
python3.12 -m venv .venv && source .venv/bin/activate   # stock macOS python3 is 3.9: too old
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
hub window closes the hub. The daemon keeps running and the hotkeys keep working. The **menu bar icon**
is the Mac's tray: its menu starts and stops dictation, opens the hub, and **Quit Rawi** stops the
daemon and the hub. A daemon started with `python rawi.py` also stops with Ctrl+C or by closing its
terminal window.

## 6. The ear's thresholds

A Mac's built-in mic has a different noise floor. If the ear never fires, raise the floor by speaking
louder for calibration, or set `listen.min_s` to `0.6` and `silence_ms` to `900`.

## What to report back

For each numbered step: **pass / fail**, the macOS version, whether it is Apple silicon, and the exact
error text where a step failed. A short video of step 4 is worth more than a paragraph.
