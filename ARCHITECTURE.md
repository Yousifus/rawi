# Architecture

rawi is small enough to read in an evening. This page is the map: what the processes are, how a
sentence travels from your mouth to someone else's text field, and why the parts are split the way
they are.

```
                        ┌──────────────────────────────┐
   microphone ─────────►│  rawi.py  (the daemon)       │
                        │                              │
   keyboard (pynput) ──►│  hotkeys · recorder · ear    │──► clipboard ──► Ctrl/⌘+V
                        │  faster-whisper · cleaner    │
                        │  dictionary · snippets       │──► transcripts.jsonl
                        └───────────┬──────────────────┘
                                    │  JSON lines on 127.0.0.1:18090
                    ┌───────────────┴────────────────┐
                    │                                │
             rawi_app.py (hub + tray)          rawi_dock.py (optional dock)
                    │
                app.html  (the window you see)
```

## The processes

| file | what it is | lifetime |
|---|---|---|
| `rawi.py` | **the daemon.** Owns the microphone, the hotkeys and the model. Everything else talks to it. | one instance, guarded by `daemon.pid` |
| `rawi_app.py` | **the hub.** A `pywebview` window plus a tray icon; renders `app.html` and calls the daemon over IPC. | starts with you, can be closed independently |
| `rawi_dock.py` | an optional always-visible strip; purely a view. | optional |
| `wave_overlay.py` | the pill that appears while you speak. Runs in the daemon's process on its own thread. | per utterance |
| `voice_out.py` | an optional resident TTS server (Kokoro) used by *voice mode* (`F7`). | optional, own process |

Only the daemon touches hardware. The hub and the dock are thin clients — which is why closing the
window never stops dictation, and why a crashed UI cannot take the microphone down with it.

## The path of one sentence

1. **Capture.** `sounddevice.InputStream` delivers 16 kHz mono float32 blocks on a callback thread.
   The callback does almost nothing: it appends to a buffer and drops a copy into a `queue.Queue`.
2. **Trigger.** Either the hold key (`Ctrl` by default, released → stop) or the ear (`F8`, voice-activity
   decides when the sentence ended). Both end in the same place: one finished utterance.
3. **Transcribe.** A worker thread runs `faster-whisper`. The model is loaded **once**, at startup, and
   kept warm — loading per utterance would cost seconds. `beam_size=5` for Arabic, greedy for English.
4. **Language.** `auto` means: detect on the first two segments, then keep that language for the rest of
   the utterance (stickiness 0.85) so a borrowed English word cannot flip an Arabic sentence. `F10`
   overrides detection entirely.
5. **Repair.** Snippets (exact whole utterance → expansion), then the dictionary: explicit `wrong=>right`
   pairs first, then a conservative fuzzy match on known names (≥ 0.86 similarity, length-gated).
6. **Clean** *(optional)*. If a local OpenAI-compatible endpoint is configured, the text is sent there
   with a per-app profile. **The raw transcript is stored next to the cleaned one**, and a fidelity
   meter scores the difference, so the cleaner can never quietly edit your meaning.
7. **Insert.** Clipboard → `Ctrl+V` (`⌘V` on macOS) → clipboard restored. If the paste cannot land, the
   text stays on the clipboard and the pill says so: **nothing is ever lost to a failed paste.**
8. **Record.** With `log: true`, one JSON line lands in `transcripts.jsonl`: raw, cleaned, language,
   timings, the focused app, the fidelity score, and a one-word mood tag.

## Why it is built this way

**Threads, not asyncio.** Audio callbacks must not block, and `faster-whisper` is a blocking native call.
One thread records, one transcribes, one owns the hotkeys; a `busy` lock allows exactly one
transcription at a time. An utterance recorded while the model is working goes into a short FIFO
(`Rawi.pending`, up to `QUEUE_MAX = 8`) and is processed in the order it was spoken — on a GPU the wait
is invisible, and on CPU it is the difference between "slow" and "eats my words". Past 8 waiting
recordings the oldest habit is abandoned in favour of the newest, and the pill says so.

**Device choice is measured, not assumed.** `cuda_available()` asks `ctranslate2` how many CUDA devices
it can actually see; `"device": "auto"` means "use CUDA when the answer is not zero". A config value of
`"cpu"` forces `int8`, and a failed CUDA load falls back to CPU rather than exiting — which is what the
README has always promised.

**The `injecting` flag.** rawi types `Ctrl+V` system-wide, so its own keystrokes come back through the
hotkey listener. Every synthetic key is bracketed by `self.injecting = True`, and the listener ignores
what it sees inside that window. Without this, releasing the paste chord can re-trigger a recording.

**Fail open, always.** Every optional part — the pill, the dock, the cleaner, the voice server — is
wrapped so that its absence degrades the experience instead of breaking dictation. A missing `tkinter`
must not cost you your words.

**The ear is a latch.** `F8` opens the microphone and leaves it open; voice-activity decides sentence
boundaries. It learns its noise floor only from *non-voice* frames, so a noisy room raises the floor
without the floor chasing your voice. It writes to the clipboard, never into a window you did not choose.

**One daemon.** A second instance means two hotkey listeners fighting over one microphone, so the pid
file is checked at startup and the newcomer exits politely.

## IPC (loopback only)

Newline-delimited JSON on `127.0.0.1:18090`. Commands: `toggle`, `start`, `stop`, `listen`, `talk`,
`swap`, `dumb`, `raw`, `status`, `wake`. The hub and the dock use it; so can a script of your own:

```bash
printf '{"cmd":"toggle"}\n' | nc 127.0.0.1 18090
```

## Files on disk

| file | contents |
|---|---|
| `config.json` | your settings; created on first run |
| `daemon.pid` | the single-instance guard |
| `transcripts.jsonl` | history — **only if `log` is true** (off by default) |
| `voice.json` | optional voice-mode settings (voice, device, autostart) |

## Porting

Platform differences are isolated at the top of `rawi.py` (`IS_WIN`, `IS_MAC`, `NO_WINDOW`, `PASTE_MOD`)
and in three small functions: `beep()`, `active_app()` and the paste chord. Windows is the tested path.
macOS uses `osascript` for the frontmost app and `⌘V`; it needs Accessibility permission for whichever
process owns the keyboard listener. Linux expects `xdotool` and an X11 session.

## Testing

`python -m unittest discover -s tests` — 23 tests over the pure logic (normalisation, fidelity,
snippets, dictionary, config invariants). They touch no hardware, take about a millisecond, and are the
first thing to run after changing anything above the transcription layer.
