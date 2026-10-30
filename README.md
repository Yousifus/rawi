# rawi · راوي

**Local-first dictation.** Hold a key, speak Arabic or English, and the words land in whatever you were
typing into — transcribed on your own machine, by your own GPU.

`rawi` (راوي) means *narrator*. The premise is simple: your voice is yours. Nothing is uploaded, there
is no account, and no service decides when you may speak.

![the hub](docs/screens/hub-room.png)

---

## What it does

- **Hold to talk** — `Ctrl` (left) held: speak, release, done. A **double-tap** latches it hands-free.
- **Arabic and English, mixed freely.** Language is detected per utterance with a stickiness rule so it
  does not flip mid-sentence; `F10` cycles a hard lock (`auto → العربية → English`) when you want one.
- **Any app.** It pastes at the cursor and restores your clipboard afterwards.
- **A pill on screen** while you speak, so you can see it heard you.
- **The ear** (`F8`) — an always-listening mode with a noise floor that learns, so you can talk without
  holding anything. It never writes into your document by itself.
- **`F9` re-pastes the last line** — for when the paste landed in the wrong window.
- **A hub window** (`rawi-app.bat`): Room, Voice, Usage, History, Dictionary, Style, Settings. Two themes.
- **An optional local cleaner** — any OpenAI-compatible server on `localhost` can tidy filler words. It is
  off by default, and when it is on, **the raw transcript is stored next to the cleaned one**.
- **A dictionary** of names and terms, applied as a bias before transcription, not as a find-and-replace
  after.
- **Nothing leaves your machine.** No telemetry, no cloud key, no subscription.

| usage | style |
|---|---|
| ![usage](docs/screens/hub-usage.png) | ![style](docs/screens/hub-style.png) |

## What it is not

- Not a meeting recorder, not a notes app: one utterance in, one paste out.
- Not a speech engine of its own — it wraps [`faster-whisper`](https://github.com/SYSTRAN/faster-whisper),
  which does the hard part.

## Requirements

- **Windows 10/11** — the tested path. **macOS** and X11 Linux have code paths (see *Platforms*) but no
  maintainer running them daily.
- Python 3.10 – 3.12.
- An NVIDIA GPU is optional but strongly recommended: `large-v3-turbo` transcribes a sentence in
  well under a second on a modern GPU. On CPU use `small` or `base` with `compute_type: int8`.

## Quickstart

```bat
git clone https://github.com/Yousifus/rawi.git
cd rawi
python -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt
copy config.example.json config.json
python rawi.py --doctor
```

`--doctor` checks the interpreter, the packages, the microphone, CUDA, the model, the config, the paste
path and the ports — and tells you which one is wrong before you spend an evening guessing. Add `--deep`
to also load the model (slow, but it proves the whole path).

![rawi --doctor](docs/screens/doctor.png)

Then start it:

```bat
python rawi.py
```

The first run downloads the Whisper weights (about 1.5 GB for `large-v3-turbo`) from Hugging Face into
your local cache. After that, no network is needed at all.

## Keys

| key | what it does |
|---|---|
| `Ctrl` (left, hold) | record while held · **double-tap** to latch |
| `F8` | the ear: always-listening on / off |
| `F9` | paste the last transcript again |
| `F10` | language lock: `auto → العربية → English` |
| `F7` | voice mode: speak the transcript instead of pasting it (needs the optional voice server) |

All of them are config keys, so you can remap any of them.

## Configuration

`config.json` sits next to `rawi.py` and is created on first run (or copy `config.example.json`).
Every key is optional; the table below covers the ones worth knowing.

| key | default | meaning |
|---|---|---|
| `hold_key` | `ctrl_l` | the key held while speaking |
| `mode` | `hold` | `hold`, `toggle`, or `latch` |
| `model` | `large-v3-turbo` | any `faster-whisper` size, or a path to a local CTranslate2 model |
| `compute_type` | `float16` | `float16` on GPU, `int8` on CPU |
| `language` | `auto` | `auto`, `ar`, `en`, … |
| `paste` | `true` | paste at the cursor instead of only copying |
| `restore_clipboard` | `true` | put your old clipboard back after pasting |
| `log` | `false` | keep a transcript history (`transcripts.jsonl`) |
| `min_seconds` / `max_seconds` | `0.25` / `180` | ignore taps / cap a runaway recording |
| `listen` | `{key: "f8", silence_ms: 700, …}` | the always-on ear and its thresholds |

## Platforms

| | Windows | macOS | Linux (X11) |
|---|---|---|---|
| record, transcribe, dictionary, ear | ✅ | ✅ expected | ✅ expected |
| paste | `Ctrl+V` | `⌘V` + **Accessibility permission** | `Ctrl+V`, needs `xdotool` |
| focused-app name (for per-app profiles) | `pygetwindow` | `osascript` | `xdotool` |
| pill / dock / hub | ✅ | pywebview + tkinter, **untested** | untested |
| beep | `winsound` | `afplay` | terminal bell |

macOS and Linux paths are implemented and deliberately small — three functions at the top of `rawi.py`
(`beep`, `active_app`, the paste chord). **They have not been validated on real hardware by the
maintainer.** If you run macOS, the first thing to grant is Accessibility to whichever process owns the
keyboard listener, or the paste will silently do nothing.


### On a machine without an NVIDIA GPU

`rawi` runs on CPU. It is slower and it is honest about it: with `small` on `int8`, expect **a few
seconds per line** (measured: 8–11 s for ordinary Arabic lines on a laptop CPU, 23 s for the first one
while the model warms, 41 s for a long sentence). Set `"device": "cpu"` to force it, or leave the default
`"auto"` and rawi will notice there is no CUDA device and choose CPU itself — and fall back to CPU if a
CUDA load fails for any reason.

Two things follow from the slower path, and both are handled:

- **Nothing is dropped while the model is busy.** A recording made during a transcription is held in a
  short queue (up to 8) and processed in order, so a slow machine never eats a sentence.
- **The pill tells you.** You'll see `queued · one at a time` instead of a silent gap.

## Privacy, precisely

- Audio lives in memory and is discarded after transcription.
- **History is off by default** in this build. Turn `log` on and it writes a local `transcripts.jsonl`
  that you can delete, grep, or ignore.
- The optional cleaner talks to a `localhost` endpoint you configure. If none is configured, nothing is
  sent anywhere — the code path does not exist.
- No analytics, no crash reporting, no update pings.

## Tests

```bash
python -m unittest discover -s tests
```

23 tests over the pure logic — normalisation, the fidelity meter, snippets, dictionary repair, config
invariants. No microphone, no GPU, no clipboard: they run in a millisecond, so you can run them after
every change.

## Repository layout

```
rawi.py            the daemon: hotkeys, recording, transcription, paste, the ear, IPC
rawi_app.py        the hub window (pywebview) + tray icon
rawi_dock.py       the small dock
wave_overlay.py    the pill
rawi_mood.py       a one-word mood tag for the history
stats.py           local usage statistics
app.html           the hub's UI (two themes)
voice_out.py       optional: a resident Kokoro TTS server for voice mode
tests/             unit tests (stdlib unittest)
docs/screens/      the screenshots above
ARCHITECTURE.md    how a sentence travels from your mouth to the cursor
```

## Roadmap

- [x] macOS / Linux paste paths (implemented; **macOS verification pending**)
- [ ] Streaming transcription (words appear while you speak)
- [ ] A benchmark script so people can compare models on their own hardware
- [ ] Package as a single executable (`pyinstaller`)

## Credits

Built by [Yousifus](https://github.com/Yousifus), for daily use in Arabic and English.
Stands on `faster-whisper`, `ctranslate2`, `sounddevice`, `pynput`, `pywebview` and, for voice mode,
`kokoro`. Whisper is OpenAI's model, MIT licensed.

## License

MIT. Take it, fork it, teach with it.
