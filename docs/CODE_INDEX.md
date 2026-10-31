# Code index

A map of every file and every public function, written so a person or a coding agent can find
the right place to change something without reading the whole repository first. Symbols are
named, not line-numbered, so the index does not go stale when code moves; search for the name.

Start with [ARCHITECTURE.md](../ARCHITECTURE.md) for *why* the parts are split this way, and
[AGENTS.md](../AGENTS.md) for the rules every change must keep.

## Layout at a glance

```
rawi.py            entry point: the daemon (mic, hotkeys, model, paste, IPC server)
rawi_app.py        entry point: the hub window + tray icon (pywebview)
rawi_dock.py       entry point: the dock, a small always-on-top capsule (Tk, own process; Windows, Linux)
rawi_menubar.py    entry point: the menu bar icon, the macOS stand-in for the dock (AppKit, own process)
stats.py           entry point: writes stats.html from the history
voice_out.py       entry point: optional local TTS server (Kokoro), for voice mode
launch_voice.py    entry point: starts voice_out.py detached
app.html           the hub's whole UI (HTML + CSS + JS, two themes)
template.html      the stats.html template (__PLACEHOLDERS__)
rawi_core/         the library every entry point is built from (below)
tests/             unit tests (stdlib unittest, numpy is the only third-party import)
docs/              this index, the macOS checklist, screenshots
site/              the static landing page (not part of the app)
start.bat, rawi-app.bat           Windows double-click launchers
start.command, rawi-app.command   macOS double-click launchers
```

**Rule of thumb:** entry points wire things together and own processes and threads; logic lives in
`rawi_core/`. If you are about to add a function to an entry point that does not touch a window,
a socket or a process, it probably belongs in `rawi_core/`.

## Who imports whom

```
rawi.py ──────────► config, doctor, gpu, history, ipc, paths, voice, mood, ear, hotkeys,
                    overlay, system, text
rawi_app.py ──────► analytics, config, history, ipc, paths, gpu, system
rawi_dock.py ─────► ipc, paths, overlay (capsule), system
rawi_menubar.py ──► ipc, paths, system   (+ pyobjc: AppKit, Foundation)
stats.py ─────────► analytics, history, paths
launch_voice.py ──► paths, system

inside rawi_core:   system ◄── overlay, voice, gpu, doctor
                    text ◄──── mood ◄── history ◄── analytics
                    paths ◄─── config, history, voice, doctor
```

`rawi_core.system` and `rawi_core.text` depend on nothing else in the package; start there when
reading.

---

## Entry points

### `rawi.py` — the daemon
The one process that owns hardware. Everything else talks to it over IPC.

| symbol | what it does |
|---|---|
| `main()` | parses `--test / --doctor / --demo / --model / --deep / --seconds`, then routes |
| `claim_single_instance()` | `daemon.pid` guard; removes the file at exit |
| `exit_cleanly_on_sigterm()` | SIGTERM / SIGHUP → SystemExit so cleanup runs (must be called after the pill exists, see macOS notes) |
| `load_model(cfg)` | `gpu.choose_device` + `gpu.load_model`, prints why |
| `transcribe_file(d, path)` | `--test`: read a wav, resample to 16 kHz, transcribe, print |
| `run_live(d)` | pill → ear → voice → IPC → dock → mic stream → key listener → main loop |
| `start_dock()` | spawn the adapter's `dock_script` once (guarded by `dock.pid`): the dock, or the menu bar on macOS |
| `demo(seconds)` | `--demo`: the pill with fake levels |
| **`class Rawi`** | the daemon's state; every key and IPC action is a method |
| `Rawi.transcribe(audio, language, listen)` | language probe → Whisper → romanised-Arabic second look → dictionary |
| `Rawi._work(audio, dur, listen)` | the pipeline for one utterance: transcribe → clean → snippets → paste / clipboard / voice → history. One at a time; extras wait in `pending` |
| `Rawi.clean(raw, level, tone, app)` | optional OpenAI-compatible cleaner on localhost; never raises |
| `Rawi.paste(line)` / `unpaste()` | clipboard + paste chord + restore / undo chord + paste raw |
| `Rawi.start_rec / stop_and_process / abort_rec` | the hold-key recorder (driven by `HoldKeyMachine`) |
| `Rawi._cb(...)` | the sounddevice callback: feeds the ear or the recording, pushes levels to the pill |
| `Rawi.set_listen / toggle_listen` | the ear on/off (F8) |
| `Rawi.toggle_talk` (F7), `cycle_lang_lock` (F10), `paste_last` (F9) | single-press keys |
| `Rawi.handle_command(req)` / `status()` | the IPC protocol (see *IPC* below) |

### `rawi_app.py` — the hub
| symbol | what it does |
|---|---|
| `main()` | starts the cleaner (if any), (re)starts the daemon, opens the window, starts the tray |
| `class Api` | everything `app.html` calls as `pywebview.api.<name>()`; replies must keep their shapes |
| `Api.snapshot / voice / galaxy / room / style_samples / history` | thin wrappers over `rawi_core.analytics` |
| `Api.get_config / save_config(patch)` | the Settings tab; `save_config` applies **only the keys present** in the patch (blank values ignored), restarts the daemon, and replies `{ok, message}` |
| `Api.restart_daemon / stop_daemon` | process control through the system adapter |
| `daemon_pids()`, `status_block()`, `ensure_cleaner()`, `cleaner_up()` | live state for the page |
| `_tray()`, `_start_tray_later()` | pystray icon (Windows); skipped where `tray_supported` is False |

### `rawi_dock.py` — the dock
`class Dock` (Tk on the main thread, a queue fed by `_status_poll`), `open_hub()`,
`hold_key_label()`. Click left = toggle recording, middle (when shown) = swap to raw words,
right = open the hub. Drag to move; the position is saved in `dock.json`.

### `rawi_menubar.py` — the menu bar (macOS)
By Abdulrahman Aldosari. A status-bar item whose icon follows the daemon (`mic`, `mic.fill` while
recording, `ear` while the ear is on, `mic.slash` when it is not running), polled once a second over
IPC. Its menu: start / stop dictation, always listening, open the hub, restart, **Quit Rawi** (stops the
daemon and the hub). A menu bar item never takes focus, which is why it replaces the Tk dock on a Mac.
`class Bar` (the item and its actions), `daemon_pid()`, `hub_pid()`, `start_daemon()`, `main()`.

### `stats.py`
`render(rows, now, template)` (pure: fills `template.html`), `bars_svg()`, `main()`.

### `voice_out.py`, `launch_voice.py`
Standalone HTTP server on `127.0.0.1:18097` (`/say`, `/stop`, `/health`, `/devices`) that keeps
Kokoro TTS warm. Optional; nothing breaks when it is absent.

---

## `rawi_core/` — the library

### `system.py` — the system adapter (Windows / macOS / Linux)
**The only file allowed to check `sys.platform`.** Get the adapter with `current()`; tests build
any OS's adapter with `for_platform("win32" | "darwin" | "linux")`.

| member | Windows | macOS | Linux |
|---|---|---|---|
| `beep(freq, ms)` | `winsound.Beep` | `afplay Tink.aiff` | terminal bell |
| `active_app()` | `pygetwindow` title | `osascript` frontmost app | `xdotool` |
| `paste_chord()` / `undo_chord()` | ctrl+v / ctrl+z | command+v / command+z | ctrl+v / ctrl+z |
| `send_chord(keys)` | pyautogui | pyautogui | pyautogui |
| `input_permission()` | None | Accessibility granted? | None |
| `background_python()` | `pythonw.exe` if present | `sys.executable` | `sys.executable` |
| `spawn(args, log_path, detached, new_group, cwd)` | `creationflags` | `start_new_session` | `start_new_session` |
| `run_hidden(args, ...)` | no console window | plain `subprocess.run` | plain |
| `pid_alive(pid)` / `kill(pid)` | OpenProcess / taskkill | `kill -0` (zombie-aware) / SIGTERM | same as macOS |
| `open_path(path)` | `os.startfile` | `open` | `xdg-open` |
| `cleaner_command(root)` | `cleaner-server.bat` | `cleaner-server.sh` | `cleaner-server.sh` |
| `register_gpu_libraries()` | pip CUDA DLL dirs | no-op | no-op |
| `font(size, bold, symbol)` | Segoe UI | Helvetica Neue (×4/3) | Tk default |
| `prepare_overlay(root, key, fill)` | colour-key transparency | floating "help" window, filled with the card colour | none |
| `make_passive(root, click_through)` | WS_EX_NOACTIVATE (+TRANSPARENT) | accessory app, ignores mouse | no-op |
| `tk_needs_main_thread` | False | **True** | False |
| `dock_script` | `rawi_dock.py` | `rawi_menubar.py` | `rawi_dock.py` |
| `launcher_hint` | `start.bat` | `start.command` | `python rawi.py` |
| `tray_supported` | True | False | True |

### `config.py`
`DEFAULTS` (every key and its default), `merged(user)` (dict sections merge one level deep),
`load(path, create)`, `read_user(path)`, `save(cfg, path)`.

### `paths.py`
Every file name: `CONFIG`, `TRANSCRIPTS`, `DAEMON_PID`, `DOCK_PID`, logs, scripts. Change a file
name here and nowhere else.

### `text.py` — pure, fully tested
`norm`, `count_arabic`, `fidelity`, `apply_snippets`, `dict_terms`, `apply_dictionary`,
`prompt_for`, `romanised_arabic_check`, `profile_for`, `TONES`, `tone_hint`. Constants:
`FILLERS`, `NAME_MATCH`, `NAME_MIN_LEN`, `AR_PROMPT`, `EN_PROMPT`, `MIX_PROMPT`, `EN_STOP`,
`ROMAN_AR`.

### `mood.py`
`tag(text)` → `{"e": emoji, "m": mood, "why": [phrases]}` or None. Lexicon: `MOODS`.

### `ear.py`
`class Ear(get_cfg, on_segment, start_worker)`: `feed(block)` from the audio callback,
`reset()`, `limits()`. Finished sentences go to `on_segment` on the worker thread. Tuning
constants at the top (`START_SECONDS`, `KEEP_FACTOR`, `PRE_BLOCKS`, `MAX_WAITING`, ...).

### `hotkeys.py`
`class HoldKeyMachine(recorder, hold_key, mode, actions, on_latch, clock, timer, keys)` with
`on_press(name)` / `on_release(name)`. `canonical_key(name)` maps a config key name to the
name pynput reports on this OS. `key_to_name(k)`. Timing: `HOLD`, `TAP`, `DOUBLE`.

### `ipc.py`
`call(request, timeout, host, port)` (client, never raises), `serve(handler, host, port, ready)`
(server thread), `port_busy(port)`. `DAEMON_PORT = 18090`.

### `voice.py`
`settings()` (voice.json), `post(path, obj)`, `health()`, `ensure_running()`. `PORT = 18097`.

### `gpu.py`
`cuda_available()`, `cuda_device_count()`, `choose_device(cfg, cuda, gate)` (pure),
`load_model(name, device, ctype)` (CUDA → CUDA int8_float16 → CPU fallback), `gpu_gate()`,
`gate_requires_yield()`, `GATE_PY`.

### `history.py`
`append(entry, on, path)`, `load_rows(path, tag_mood)`, `words`, `fmt_mins`, `app_label`,
`day_streak`.

### `analytics.py` — pure, fully tested
`summary`, `snapshot` (with the 26-week `calendar_days`), `voice_report` (whose `insights` are the
Patterns: weekdays, languages by week, vocabulary, apps, moods, runs), `galaxy`, `room`,
`style_samples`, `search`, `script_of`. The output shapes are a contract with `app.html`.

### `overlay.py` — the pill
`class Wave(fps, own_thread)`: `show / hide / flash / push / quit` (any thread, queue only),
`run()` (macOS: hand over the main thread), `demo()`, `owns_main_thread`. `capsule()` is shared
with the dock.

### `doctor.py`
`run(deep, cfg_path)`: the numbered checks behind `python rawi.py --doctor`.

---

## IPC protocol (127.0.0.1:18090, one JSON line each way)

| `cmd` | extra fields | reply |
|---|---|---|
| `status` (default) | | status dict: `recording busy dumb swap listening dropped lvl floor heard peak keys ear` |
| `toggle` / `start` / `stop` | | status |
| `listen` | `on`: bool or absent (flip) | status |
| `dumb` | `on` | status |
| `raw` | `text` | status (after pasting `text`) |
| `swap` | | `{ok, raw | error, recording, busy}` |
| `talk` | `on` | `{ok, talk, bridge}` |
| `wake` | | `{ok, wake, listening}` |

## Files on disk (all next to `rawi.py`, all git-ignored)

`config.json`, `transcripts.jsonl` (only with `log: true`), `voice.json`, `dock.json`,
`daemon.pid`, `dock.pid`, `rawi.log`, `dict.log`, `docker.log`, `voiceout.log`, `app_debug.log`,
`stats.html`.

## Tests

`python -m unittest discover -s tests` — one file per module:

| file | covers |
|---|---|
| `test_text.py` | normalisation, fidelity, snippets, dictionary, prompts, romanised Arabic, tones |
| `test_config.py` | defaults, merging, first-run creation, unreadable files |
| `test_system.py` | each OS adapter, spawn flags, pid liveness (incl. zombies), queries |
| `test_hotkeys.py` | hold / tap / double-tap / shortcuts / toggle, macOS key aliases |
| `test_ear.py` | the voice-activity gate on synthetic audio |
| `test_ipc.py` | a real loopback round trip |
| `test_history_analytics.py` | the history file, every hub number, stats.html, moods |
| `test_daemon.py` | `Rawi` with a fake model / clipboard / OS, the hub's config API |
| `_helpers.py` | `FakeClock`, `FakeTimer`, `FakePlatform` |

## Where to change…

| I want to… | go to |
|---|---|
| support a new OS behaviour | a method on `Platform` in `rawi_core/system.py`, overridden per OS |
| add a config key | `DEFAULTS` in `rawi_core/config.py` (+ README table, + `config.example.json` if common) |
| add a hotkey | `DEFAULTS` key + a `Rawi` method + the `actions` dict in `run_live()` |
| add an IPC command | `Rawi.handle_command` + the table above + a test in `test_daemon.py` |
| change how text is repaired | `rawi_core/text.py` (+ `test_text.py`) |
| tune the ear | constants at the top of `rawi_core/ear.py` (+ `test_ear.py`) |
| add a number to the hub | a pure function in `rawi_core/analytics.py`, a wrapper on `Api`, then `app.html` |
| change the pill's look | `rawi_core/overlay.py` (colours and sizes at the top) |
| add a doctor check | `rawi_core/doctor.py` |
