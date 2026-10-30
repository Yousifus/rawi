# Verifying rawi on macOS

The macOS path exists but has never been run on a Mac by the maintainer. This is the ten-minute
checklist that proves or breaks it. Report each step as **pass / fail**, with the exact command and
the exact error. Nothing here is destructive: no audio leaves the machine, and history is off by default.

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
- `[7] paste path` — `macos: clipboard + cmd+v`
- exit code 0

```bash
python rawi.py --doctor --deep     # also loads the model (slow, ~1-2 min on CPU)
```

Pass = it loads and prints a load time.

## 3. The paste chord

The one line that matters most is `pyautogui.hotkey(PASTE_MOD, "v")` with `PASTE_MOD = "command"`.
Test it in isolation before trusting the daemon:

```bash
python -c "import pyautogui, pyperclip; pyperclip.copy('rawi paste test'); print('copied')"
```

Then focus TextEdit and press ⌘V by hand: the text appears. If it does, the clipboard half is fine and
any later failure is the synthetic keystroke (permissions), not the logic.

## 4. End to end

```bash
python rawi.py
```

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

## 6. The ear's thresholds

A Mac's built-in mic has a different noise floor. If the ear never fires, raise the floor by speaking
louder for calibration, or set `listen.min_s` to `0.6` and `silence_ms` to `900`.

## What to report back

For each numbered step: **pass / fail**, the macOS version, whether it is Apple silicon, and the exact
error text where a step failed. A short video of step 4 is worth more than a paragraph.
