"""Rawi — local speech to text, pasted into whatever box has focus.

Hold the key, speak, release. Everything runs on this machine:
mic -> faster-whisper -> clipboard -> Ctrl+V (Cmd+V on macOS).

Usage:
  pythonw rawi.py               run in the background (Windows, no console)
  python rawi.py                run with a console (see what it hears)
  python rawi.py --test f.wav   transcribe a file, print, do not paste
  python rawi.py --doctor       check the machine, the mic, the model and the config
  python rawi.py --demo         show the pill for a few seconds

This file is the daemon: the one process that owns the microphone, the hotkeys and the model.
The hub (rawi_app.py) and the dock (rawi_dock.py) talk to it over loopback IPC. The pieces it
is built from live in rawi_core/ — see docs/CODE_INDEX.md for a map.
"""
from __future__ import annotations

import argparse
import atexit
import collections
import datetime
import os
import signal
import sys
import threading
import time

import numpy as np

from rawi_core import config, doctor, gpu, history, ipc, paths, voice
from rawi_core import mood as rawi_mood
from rawi_core.ear import Ear
from rawi_core.hotkeys import HoldKeyMachine, key_to_name
from rawi_core.overlay import Wave
from rawi_core.system import current as current_platform
from rawi_core.text import (apply_dictionary, apply_snippets, count_arabic, dict_terms, fidelity,
                            profile_for, prompt_for, romanised_arabic_check, tone_hint)

SR = 16000             # Whisper's sample rate; the microphone is opened at this rate
QUEUE_MAX = 8          # recordings held while one is still transcribing
KEYS_SEEN_MAX = 8      # recent key names kept for the status reply (a debugging aid)


class Rawi:
    """The daemon's state and every action a key press or an IPC request can trigger.

    Threads that touch it: the audio callback (`_cb`), the hotkey listener, the IPC server,
    the ear's worker and one transcription worker at a time (guarded by `busy`).
    """

    def __init__(self, cfg: dict):
        self.cfg = cfg
        self.plat = current_platform()
        self.model = None
        self.frames: list[np.ndarray] = []
        self.stream = None
        self.recording = False
        self.started_at = 0.0
        self.busy = threading.Semaphore(1)
        self.pending = collections.deque()      # recordings waiting for the model, in order
        self.wave = None                        # the pill (rawi_core.overlay.Wave), optional
        self.dock = None                        # the dock's Popen, optional
        # True while we are injecting our own Ctrl+V. pynput sees injected keys as real,
        # so without this every paste would start a recording (Claude, 2026-10-30).
        self.injecting = False
        # the last dictation, kept so it can be un-pasted: trust lives in the undo
        self.last: dict | None = None
        self._talk_buf: list[str] = []
        self._talk_timer = None
        # the ear: always-on listening, off until latched
        self.ear: Ear | None = None
        self.lang_hint = None
        self.last_lang = (None, 0.0)
        self.lang_probs: dict = {}
        self.keys_seen: list[str] = []

    # ---------- small helpers ----------
    def beep(self, freq: int, ms: int = 55) -> None:
        """A short tone when beeps are on. Never raises."""
        if self.cfg.get("beeps", True):
            self.plat.beep(freq, ms)

    def flash(self, msg: str, ok: bool = True, seconds: float = 2.2) -> None:
        """Show a receipt on the pill, if there is one."""
        if self.wave is not None:
            self.wave.flash(msg, ok=ok, seconds=seconds)

    def is_busy(self) -> bool:
        """True while a transcription is running."""
        busy = not self.busy.acquire(blocking=False)
        if not busy:
            self.busy.release()
        return busy

    def note_key(self, name) -> None:
        self.keys_seen.append(str(name))
        del self.keys_seen[:-KEYS_SEEN_MAX]

    def make_ear(self) -> Ear:
        """The ear, created on first use. Its finished sentences go through `_work`."""
        if self.ear is None:
            self.ear = Ear(get_cfg=lambda: self.cfg.get("listen") or {},
                           on_segment=lambda seg: self._work(seg, len(seg) / float(SR), listen=True))
        return self.ear

    # ---------- model ----------
    def pick_lang(self, audio):
        """Ask the encoder what it hears before deciding how to write it. A short line
        from a bilingual speaker is exactly where a blind auto-detect gets it wrong."""
        try:
            lang, prob, allp = self.model.detect_language(audio, language_detection_segments=2)
            self.lang_probs = dict(allp or [])
            return lang, float(prob or 0.0)
        except Exception as e:
            print(f"[rawi] detect_language failed ({type(e).__name__}: {e})", flush=True)
            return None, 0.0

    def _prompt_for(self, lang):
        names, _pairs = dict_terms(self.cfg)
        return prompt_for(lang, names, self.cfg.get("arabic_prompt"))

    def _whisper(self, audio: np.ndarray, lang, listen: bool):
        """One faster-whisper pass. Beam search for Arabic, greedy otherwise.
        The ear has already cut silence, so Whisper's own VAD is only used for held recordings."""
        segs, info = self.model.transcribe(
            audio, language=lang, task="transcribe",
            initial_prompt=self._prompt_for(lang),
            beam_size=5 if lang == "ar" else 1,
            condition_on_previous_text=False,
            without_timestamps=True,
            vad_filter=not listen,
            vad_parameters={"min_silence_duration_ms": 350},
        )
        return "".join(s.text for s in segs).strip(), info

    def transcribe(self, audio: np.ndarray, language: str | None, listen: bool = False):
        """Whisper, tuned for a speaker who uses Arabic and English in the same breath.

        Arabic must come back in Arabic script. The killer bug was an English prompt on
        Arabic audio: it steered the model into writing English for what it heard.
        `language` None means auto-detect; a value means the user chose it (config or F10).
        Returns (text, language_written, audio_seconds).
        """
        lang, prob = language, 1.0
        if lang is None:
            got, prob = self.pick_lang(audio)
            need = 0.85 if self.lang_hint else 0.60   # mid-conversation, the language sticks
            if prob >= need and got:
                lang = got
            elif self.lang_hint:
                lang = self.lang_hint
            else:
                lang = None          # unsure and no history: let Whisper decide, mixed prompt
        out, info = self._whisper(audio, lang, listen)
        wrote = lang or getattr(info, "language", None)
        # An English opener can make the probe say "en" and the Arabic after it comes back
        # romanised. Look again — unless the user locked English, which is their call to make.
        if wrote == "en" and language is None:
            ar_runner = float(self.lang_probs.get("ar") or 0.0)
            retry, markers, en_stop = romanised_arabic_check(out, ar_runner)
            if retry:
                t2, _info2 = self._whisper(audio, "ar", listen)
                ar_letters = count_arabic(t2)
                if ar_letters >= 2:
                    print(f"[rawi] second look: arabic kept ({ar_letters} letters, "
                          f"markers={markers[:2]}, runner={ar_runner:.2f})", flush=True)
                    out, wrote = t2, "ar"
                else:
                    print(f"[rawi] second look: stayed english (markers={markers[:2]}, "
                          f"runner={ar_runner:.2f}, en_stop={en_stop})", flush=True)
        out = apply_dictionary(out, self.cfg)
        if wrote in ("ar", "en") and (prob >= 0.60 or lang is not None):
            self.lang_hint = wrote
        self.last_lang = (lang, round(prob, 2))
        print(f"[rawi] wrote as {wrote} (probe {prob:.2f})", flush=True)
        return out, (wrote or "?"), float(getattr(info, "duration", 0.0) or 0.0)

    # ---------- keys: single-press actions ----------
    def paste_last(self) -> bool:
        """Put the last line wherever the caret is now (F9) — the manual half of the ear."""
        if not self.last or not self.last.get("clean"):
            self.beep(300, 70)
            return False
        try:
            import pyperclip
            pyperclip.copy(self.last["clean"])
        except Exception:
            pass
        self.paste(self.last["clean"])
        self.flash("✓ pasted · " + self.last["clean"][:40])
        return True

    def set_listen(self, on: bool) -> None:
        """Open or close the ear (the always-on listener)."""
        ear = self.make_ear()
        on = bool(on)
        if on == ear.on:
            return
        ear.on = on
        if self.wave is not None:
            try:
                self.wave.show() if on else self.wave.hide()
            except Exception:
                pass
        if on:
            self.beep(880, 45); time.sleep(0.08); self.beep(1175, 55)
            print("[rawi] ear open — speak; it stops on silence", flush=True)
        else:
            ear.reset()
            self.beep(1175, 45); time.sleep(0.08); self.beep(880, 55)
            print("[rawi] ear closed", flush=True)

    def toggle_listen(self) -> None:
        """F8: one press opens the ear, another closes it."""
        self.set_listen(not bool(self.ear and self.ear.on))

    def toggle_talk(self) -> None:
        """F7: voice mode on/off. Voice mode speaks each line instead of pasting it."""
        on = not bool(self.cfg.get("talk"))
        self.cfg["talk"] = on
        config.save(self.cfg)
        ok = voice.post("/mode", {"on": on}) if on else True
        if on and not (self.ear and self.ear.on):
            self.set_listen(True)          # voice mode means the ear is open
        self.beep(1046 if on else 523, 70)
        if on and not ok:
            msg = "voice on · no voice server"
        else:
            msg = "voice · on" if on else "voice · off"
        self.flash(msg, ok=ok or not on, seconds=2.6)
        print(f"[rawi] voice={on}", flush=True)

    def cycle_lang_lock(self) -> None:
        """F10: auto -> Arabic -> English -> auto."""
        order = ["auto", "ar", "en"]
        cur = str(self.cfg.get("lang_lock") or "auto")
        nxt = order[(order.index(cur) + 1) % 3] if cur in order else "auto"
        self.cfg["lang_lock"] = nxt
        config.save(self.cfg)
        self.flash({"auto": "language: auto",
                    "ar": "language: Arabic · عربي",
                    "en": "language: English"}[nxt])
        self.beep(700 if nxt != "auto" else 560, 60)
        print(f"[rawi] lang_lock={nxt}", flush=True)

    def latch_beep(self) -> None:
        """Two rising notes: a double-tap latched the recorder on."""
        self.beep(880, 45); time.sleep(0.09); self.beep(1100, 45)

    # ---------- audio ----------
    def _cb(self, indata, frames, time_info, status):  # sounddevice callback: keep it light
        if self.ear is not None and self.ear.on and not self.recording:
            self.ear.feed(indata)
            if self.wave is not None:
                try:
                    self.wave.push(min(1.0, float(np.sqrt(np.mean(np.square(indata)))) * 9.0))
                except Exception:
                    pass
        if self.recording:
            self.frames.append(indata.copy())
            if self.wave is not None:
                try:
                    rms = float(np.sqrt(np.mean(np.square(indata))))
                    self.wave.push(min(1.0, rms * 6.0))
                except Exception:
                    pass

    def start_rec(self) -> None:
        if self.recording:
            return
        self.frames = []
        self.recording = True
        self.started_at = time.time()
        self.beep(880, 55)
        if self.wave is not None:
            self.wave.show()
        print("[rawi] recording...", flush=True)

    def abort_rec(self) -> None:
        """Discard a recording that turned out to be a shortcut, without a beep."""
        self.recording = False
        self.frames = []
        if self.wave is not None:
            self.wave.hide()

    def stop_and_process(self) -> None:
        if not self.recording:
            return
        self.recording = False
        self.beep(520, 55)
        if self.wave is not None:
            self.wave.hide()
        dur = time.time() - self.started_at
        frames = self.frames
        self.frames = []
        if not frames:
            print("[rawi] no audio, ignored", flush=True)
            return
        audio = np.concatenate(frames, axis=0).reshape(-1).astype(np.float32)
        if len(audio) / float(SR) < float(self.cfg["min_seconds"]):
            print("[rawi] too short, ignored", flush=True)   # the audio's length, not the wall clock
            return
        print(f"[rawi] captured {dur:.1f}s ({len(audio)/SR:.1f}s audio), transcribing...", flush=True)
        threading.Thread(target=self._work, args=(audio, dur), daemon=True).start()

    # ---------- work ----------
    def _log_record(self, cleaned, raw, lg, app, took, ctook, cstatus, dur, fid, listen):
        """One history row (only when `log` is on and dumb mode is off)."""
        try:
            history.append({"ts": datetime.datetime.now().isoformat(timespec="seconds"),
                            "seconds": round(dur, 2), "language": lg, "model": self.cfg["model"],
                            "transcribe_s": round(took, 2), "clean_s": round(ctook, 2),
                            "clean": cstatus, "text": cleaned, "raw": raw, "app": app,
                            "fid": fid,
                            "mood": (rawi_mood.tag(cleaned)
                                     if str(self.cfg.get("mood", "auto")) != "off" else None),
                            "to": ("voice" if self.cfg.get("talk") else ""),
                            "listen": bool(listen)},
                           self.cfg["log"] and not self.cfg.get("dumb"))
        except Exception as e:
            print(f"[rawi] log failed {type(e).__name__}: {e}", flush=True)

    def _talk_add(self, line: str) -> None:
        """Hold the line, then send one thought instead of four fragments.

        People speak, stop to think, speak again. Without this, a stop-to-think becomes a turn
        of its own and the conversation stutters. The timer is reset by every new fragment, so
        only a real silence flushes.
        """
        self._talk_buf.append(line)
        joined = " ".join(self._talk_buf)
        delay = max(0.4, float(self.cfg.get("talk_merge_ms", 1200)) / 1000.0)
        if len(joined) > 400:                    # never hold a paragraph hostage to a timer
            delay = 0.05
        if self._talk_timer is not None:
            try:
                self._talk_timer.cancel()
            except Exception:
                pass
        timer = threading.Timer(delay, self._flush_talk)
        timer.daemon = True
        self._talk_timer = timer
        timer.start()
        if len(self._talk_buf) > 1:
            self.flash(f"holding {len(self._talk_buf)} lines", ok=True, seconds=1.2)
        print(f"[rawi] talk buffer {len(self._talk_buf)} ({len(joined)} chars, "
              f"flush in {delay:.1f}s)", flush=True)

    def _flush_talk(self) -> None:
        buf = " ".join(self._talk_buf or []).strip()
        self._talk_buf = []
        self._talk_timer = None
        if not buf:
            return
        ok = voice.post("/say", {"text": buf})
        self.beep(880 if ok else 330, 50)
        if not ok:      # never lose the words because a bridge is down
            try:
                import pyperclip
                pyperclip.copy(buf)
            except Exception:
                pass
        self.flash(("→ voice · " if ok else "voice offline · copied · ") + buf[:34],
                   ok=ok, seconds=2.6)
        print(f"[rawi] talk send via voice ok={ok}: {buf[:90]}", flush=True)

    def _work(self, audio: np.ndarray, dur: float, listen: bool = False) -> None:
        """Transcribe -> clean -> snippets -> paste / clipboard / voice -> history.

        One at a time: a recording that arrives while the model is busy waits in `pending`
        (up to QUEUE_MAX) and runs next, in the order it was spoken.
        """
        if not self.busy.acquire(blocking=False):
            # Never lose words to a slow machine: hold the audio, do it next.
            if len(self.pending) < QUEUE_MAX:
                self.pending.append((audio, dur, listen))
                print(f"[rawi] still transcribing the last one; queued ({len(self.pending)})", flush=True)
                self.flash("queued · one at a time", ok=True, seconds=1.8)
            else:
                print(f"[rawi] queue full ({QUEUE_MAX}); dropped", flush=True)
                self.beep(300, 90)
            return
        try:
            lock = str(self.cfg.get("lang_lock") or "auto").lower()
            if lock in ("ar", "en"):
                lang = lock                      # the user decided, not the model
            else:
                lang = None if str(self.cfg["language"]).lower() == "auto" else self.cfg["language"]
            t0 = time.time()
            raw, lg, _ = self.transcribe(audio, lang, listen=listen)
            took = time.time() - t0
            if not raw:
                if listen:
                    if self.ear is not None:
                        self.ear.empty += 1
                    self.flash("heard nothing", ok=False, seconds=1.6)
                return
            app = self.plat.active_app()
            c0 = time.time()
            if self.cfg.get("dumb"):
                cleaned, cstatus = raw, "dumb"
            else:
                cleaned, cstatus = self.clean(raw, app=app)
            ctook = time.time() - c0
            snip = apply_snippets(cleaned, self.cfg)
            if snip != cleaned:
                cleaned, cstatus = snip, "snippet"
            print(f"[rawi] {lg} in {took:.2f}s (+clean {ctook:.2f}s {cstatus}): {cleaned}", flush=True)
            fid = fidelity(raw, cleaned)
            self.last = {"raw": raw, "clean": cleaned, "app": app,
                         "ts": time.time(), "pasted": bool(self.cfg["paste"]) and not listen}
            if listen and self.cfg.get("talk"):
                # Voice mode: pauses are breathing, not sentences ending.
                self._talk_add(cleaned)
            elif listen:
                # the words, ready — never pasted into a window the speaker did not choose
                try:
                    import pyperclip
                    pyperclip.copy(cleaned)
                except Exception:
                    pass
                self.beep(660, 45)
                self.flash("✓ copied · " + cleaned[:44])
            elif self.cfg["paste"]:
                self.paste(cleaned)
            self._log_record(cleaned, raw, lg, app, took, ctook, cstatus, dur, fid, listen)
        except Exception as e:
            print(f"[rawi] ERROR {type(e).__name__}: {e}", flush=True)
            self.beep(220, 120)
        finally:
            self.busy.release()
            if self.pending:                     # drain, one at a time, in order
                nxt = self.pending.popleft()
                threading.Thread(target=self._work, args=nxt, daemon=True).start()

    # ---------- cleanup ----------
    def clean(self, raw: str, level: str | None = None, tone: str | None = None,
              app: str | None = None) -> tuple[str, str]:
        """Return (clean_text, status). Never raises: on any failure the raw text wins.

        status: "off", "off:<profile>", "ok", "empty", "runaway", or the exception's name.
        """
        c = self.cfg.get("clean") or {}
        if not c.get("enabled"):
            return raw, "off"
        prof = profile_for(app, self.cfg.get("profiles"))
        level = (level or prof.get("level") or c.get("level") or "light").lower()
        tone = (tone or prof.get("tone") or c.get("tone") or "default")
        if level == "none":
            return raw, f"off:{prof.get('name', 'level')}"
        try:
            import json
            import urllib.request
            defaults = config.DEFAULTS["clean"]
            names, _ = dict_terms(self.cfg)
            sysmsg = c.get("system") or defaults["system"]
            if level == "full":
                sysmsg += (" Also tighten the result: remove repeated words and false starts, "
                           "split run-on sentences, and keep it to the fewest words that carry "
                           "the meaning. Do not change the meaning.")
            sysmsg += tone_hint(tone)
            if names:
                sysmsg += (" Keep these names spelled exactly like this if you see them: "
                           + ", ".join(names) + ".")
            body = json.dumps({
                "messages": [{"role": "system", "content": sysmsg},
                             {"role": "user", "content": raw}],
                "temperature": 0,
                "max_tokens": int(c.get("max_tokens", defaults["max_tokens"])),
                "chat_template_kwargs": {"enable_thinking": False},
            }).encode("utf-8")
            req = urllib.request.Request(c.get("endpoint") or defaults["endpoint"], data=body,
                                         headers={"Content-Type": "application/json"})
            with urllib.request.urlopen(req, timeout=float(c.get("timeout", defaults["timeout"]))) as resp:
                j = json.loads(resp.read().decode("utf-8"))
            out = (j["choices"][0]["message"].get("content") or "").strip()
            if not out:
                return raw, "empty"
            if len(out) > max(3 * len(raw), len(raw) + 200):
                return raw, "runaway"
            return out, "ok"
        except Exception as e:
            return raw, f"{type(e).__name__}"

    # ---------- output ----------
    def unpaste(self) -> dict:
        """Undo the last paste in place and put the raw words back.

        Undo reverts the paste in every editor and text field worth using; then the raw
        goes in the same way the clean did, so the same caret rule applies.
        """
        L = self.last
        if not L or not L.get("pasted"):
            return {"ok": False, "error": "nothing to undo"}
        app = self.plat.active_app()
        if L.get("app") and app and L["app"] != app:
            return {"ok": False, "error": f"focus moved to {app[:40]}"}
        self.injecting = True
        try:
            self.plat.send_chord(self.plat.undo_chord())
            time.sleep(0.20)
        finally:
            self.injecting = False
        self.paste(L["raw"])
        L["pasted"] = False
        return {"ok": True, "raw": L["raw"]}

    def paste(self, line: str) -> None:
        """Clipboard -> paste chord -> old clipboard back. If the chord cannot be sent, the text
        stays on the clipboard: nothing is ever lost to a failed paste."""
        import pyperclip
        old = None
        if self.cfg["restore_clipboard"]:
            try:
                old = pyperclip.paste()
            except Exception:
                old = None
        pyperclip.copy(line)
        time.sleep(0.10)
        self.injecting = True
        try:
            self.plat.send_chord(self.plat.paste_chord())
        finally:
            time.sleep(0.12)
            self.injecting = False
        time.sleep(0.35)
        if old is not None:
            try:
                pyperclip.copy(old)
            except Exception:
                pass

    # ---------- IPC ----------
    def status(self) -> dict:
        """The reply to every command that has no reply of its own."""
        ear = self.ear
        return {"ok": True, "recording": bool(self.recording), "busy": self.is_busy(),
                "dumb": bool(self.cfg.get("dumb")),
                "swap": bool(self.last and self.last.get("pasted")),
                "listening": bool(ear and ear.on),
                "dropped": int(ear.dropped) if ear else 0,
                "lvl": round(float(ear.lvl) if ear else 0.0, 4),
                "floor": round(float(ear.floor) if ear else 0.0, 4),
                "heard": bool(ear.talking) if ear else False,
                "peak": round(float(ear.peak) if ear else 0.0, 4),
                "keys": list(self.keys_seen),
                "ear": ({"emits": int(ear.emits), "done": int(ear.done), "short": int(ear.short),
                         "dropped": int(ear.dropped), "errors": int(ear.errors)} if ear else {})}

    def handle_command(self, req: dict) -> dict:
        """One IPC request -> one reply. Commands: toggle start stop listen talk swap dumb raw
        wake status. Unknown commands are answered with the status."""
        cmd = str(req.get("cmd") or "status")
        if cmd == "toggle":
            if self.recording:
                self.stop_and_process()
            else:
                self.start_rec()
        elif cmd == "start":
            self.start_rec()
        elif cmd == "stop":
            self.stop_and_process()
        elif cmd == "wake":
            # a wave is not a click: the eye's own verb, so Rawi can say who woke it
            already = bool(self.ear and self.ear.on)
            if not already:
                self.set_listen(True)
            voice.post("/say", {"text": "Yes?" if not already else ""})
            self.flash("woken by a wave", ok=True, seconds=2.2)
            print(f"[rawi] woken by a wave (already={already})", flush=True)
            return {"ok": True, "wake": True, "listening": bool(self.ear and self.ear.on)}
        elif cmd == "talk":
            want = req.get("on")
            if want is None:
                want = not bool(self.cfg.get("talk"))
            self.cfg["talk"] = bool(want)
            config.save(self.cfg)
            bridge = voice.post("/mode", {"on": True}) if want else True
            return {"ok": True, "talk": bool(want), "bridge": bridge}
        elif cmd == "listen":
            want = req.get("on")
            if want is None:
                want = not bool(self.ear and self.ear.on)
            self.set_listen(bool(want))
        elif cmd == "swap":
            res = self.unpaste()
            return dict(res, recording=bool(self.recording), busy=self.is_busy())
        elif cmd == "dumb":
            self.cfg["dumb"] = bool(req.get("on", not self.cfg.get("dumb")))
            config.save(self.cfg)
        elif cmd == "raw":
            line = str(req.get("text") or "")
            if line:
                self.paste(line)
        return self.status()


# ---------------------------------------------------------------- helper processes
def start_dock():
    """Best effort: the small always-on-top capsule, in its own process.

    Its own process, not a thread: Tk wants one interpreter per thread and the pill already owns
    one (and on macOS the main thread). Two roots in one process is a crash waiting for a busy
    morning.
    """
    plat = current_platform()
    try:
        with open(paths.DOCK_PID, encoding="utf-8") as f:
            old = int(f.read().strip() or 0)
        if old and plat.pid_alive(old):
            print(f"[rawi] dock already up (pid {old})", flush=True)
            return None
    except Exception:
        pass
    try:
        p = plat.spawn([plat.background_python(), paths.DOCK_SCRIPT], log_path=paths.DOCK_LOG,
                       cwd=paths.ROOT)
        try:
            with open(paths.DOCK_PID, "w", encoding="utf-8") as f:
                f.write(str(p.pid))
        except Exception:
            pass
        print(f"[rawi] dock pid {p.pid}", flush=True)
        return p
    except Exception as e:
        print(f"[rawi] dock unavailable ({type(e).__name__}: {e})", flush=True)
        return None


# ---------------------------------------------------------------- startup
def _setup_console() -> None:
    """UTF-8 prints everywhere; under pythonw (no console) send them to rawi.log."""
    if sys.stdout is None or sys.stderr is None:
        logf = open(paths.DAEMON_LOG, "a", encoding="utf-8", buffering=1)
        if sys.stdout is None:
            sys.stdout = logf
        if sys.stderr is None:
            sys.stderr = logf
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass


def claim_single_instance() -> bool:
    """One daemon: a second one means two hotkey listeners fighting over one microphone.

    Returns False (and says so) when another live daemon holds daemon.pid. Otherwise writes our
    pid and removes the file again on a clean exit (run_live makes SIGTERM one of those).
    """
    plat = current_platform()
    try:
        with open(paths.DAEMON_PID, encoding="utf-8") as f:
            old_pid = int(f.read().strip())
    except Exception:
        old_pid = 0
    if old_pid and old_pid != os.getpid() and plat.pid_alive(old_pid):
        print(f"[rawi] already running as {old_pid}; this one exits", flush=True)
        return False
    try:
        with open(paths.DAEMON_PID, "w", encoding="utf-8") as f:
            f.write(str(os.getpid()))
    except Exception:
        pass

    def release() -> None:
        try:
            with open(paths.DAEMON_PID, encoding="utf-8") as f:
                if int(f.read().strip()) == os.getpid():
                    os.remove(paths.DAEMON_PID)
        except Exception:
            pass

    atexit.register(release)
    return True


def exit_cleanly_on_sigterm() -> None:
    """Turn SIGTERM (the hub's restart / quit on macOS and Linux) into SystemExit, so atexit
    handlers such as the pid-file cleanup run. Python's default would skip them.

    Call it after the pill exists: on macOS, Tk's start-up replaces the process's SIGTERM
    handler at the C level (Python's getsignal() does not notice), and the process then dies
    with exit code 1 and no cleanup.
    """
    try:
        signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))
    except Exception:
        pass


def load_model(cfg: dict):
    """Pick the device and load Whisper once; it is kept warm for the daemon's lifetime."""
    device, ctype, why = gpu.choose_device(cfg, cuda=gpu.cuda_available())
    if why:
        print(f"[rawi] {why}", flush=True)
    print(f"[rawi] loading {cfg['model']} ({ctype} on {device}) ...", flush=True)
    t0 = time.time()
    model = gpu.load_model(cfg["model"], device, ctype, log=lambda m: print(m, flush=True))
    print(f"[rawi] model ready in {time.time()-t0:.1f}s", flush=True)
    return model


def transcribe_file(d: Rawi, path: str) -> int:
    """--test: transcribe a wav/flac/ogg file and print it. Nothing is pasted or logged."""
    import soundfile as sf
    audio, sr = sf.read(path, dtype="float32", always_2d=False)
    if audio.ndim > 1:
        audio = audio.mean(axis=1)
    if sr != SR:                                   # linear resample: plenty for speech
        n = int(len(audio) * SR / sr)
        idx = np.linspace(0, len(audio) - 1, n)
        audio = np.interp(idx, np.arange(len(audio)), audio).astype(np.float32)
    lang = None if str(d.cfg["language"]).lower() == "auto" else d.cfg["language"]
    t0 = time.time()
    out, lg, _dur = d.transcribe(audio, lang)
    print(f"[{lg}] {time.time()-t0:.2f}s for {len(audio)/SR:.1f}s audio")
    print(out)
    return 0


def run_live(d: Rawi) -> int:
    """Live mode: pill, ear, IPC, dock, microphone, then the hotkeys until Ctrl+C."""
    cfg = d.cfg
    print(f"[rawi] key={cfg['hold_key']} mode={cfg['mode']} lang={cfg['language']} "
          f"paste={cfg['paste']}", flush=True)

    try:
        d.wave = Wave()
        print("[rawi] wave overlay ready", flush=True)
    except Exception as e:
        print(f"[rawi] wave overlay failed ({type(e).__name__}: {e}); continuing without it", flush=True)

    d.make_ear()
    threading.Thread(target=voice.ensure_running, daemon=True).start()
    ipc.serve(d.handle_command)
    if os.environ.get("RAWI_NO_DOCK") != "1":
        d.dock = start_dock()

    import sounddevice as sd
    d.stream = sd.InputStream(samplerate=SR, channels=1, dtype="float32", callback=d._cb, blocksize=0)
    d.stream.start()

    machine = HoldKeyMachine(
        d, hold_key=cfg["hold_key"], mode=cfg["mode"], on_latch=d.latch_beep,
        # a list, not a dict: if two keys are configured the same, the earlier one wins,
        # in the original order (voice > language > paste last > ear)
        actions=[(cfg.get("talk_key") or "f7", d.toggle_talk),
                 (cfg.get("lang_key") or "f10", d.cycle_lang_lock),
                 (cfg.get("paste_key") or "f9", d.paste_last),
                 ((cfg.get("listen") or {}).get("key") or "f8", d.toggle_listen)])

    from pynput import keyboard

    def on_press(k):
        name = key_to_name(k)
        d.note_key(name)
        machine.on_press(name)

    def on_release(k):
        machine.on_release(key_to_name(k))

    if d.plat.input_permission() is False:
        print("[rawi] macOS has not granted Accessibility to this app: hotkeys and paste will "
              "do nothing. System Settings -> Privacy & Security -> Accessibility (and Input "
              "Monitoring): add your terminal, then restart it.", flush=True)

    listener = keyboard.Listener(on_press=on_press, on_release=on_release)
    listener.start()
    exit_cleanly_on_sigterm()
    print("[rawi] listening. Ctrl+C here to quit.", flush=True)
    if d.wave is not None and d.wave.owns_main_thread:
        # macOS: Tk keeps the main thread and nothing joins the listener, so watch it here.
        # A crash ends the daemon, as listener.join() did; a listener that simply never started
        # (no Accessibility yet) keeps it up, since the dock and the hub still work over IPC.
        def watch_listener():
            try:
                listener.join()
            except Exception as e:
                print(f"[rawi] key listener crashed ({type(e).__name__}: {e}); stopping", flush=True)
                d.wave.quit()

        threading.Thread(target=watch_listener, daemon=True).start()
        d.wave.run()
        listener.stop()
    else:
        try:
            listener.join()
        except KeyboardInterrupt:
            pass
    return 0


def demo(seconds: float) -> int:
    """--demo: show the pill with fake speech, no microphone or model needed."""
    try:
        w = Wave()
    except Exception as e:
        print(f"[rawi] wave overlay unavailable ({type(e).__name__}: {e})", flush=True)
        return 1
    print("[rawi] demo: watch the bottom of the screen", flush=True)
    w.demo(seconds)
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description="rawi: hold-to-talk local dictation")
    ap.add_argument("--test", metavar="AUDIO", help="transcribe a file and print; no paste")
    ap.add_argument("--model", help="override model name")
    ap.add_argument("--demo", action="store_true", help="show the voice wave for a few seconds and exit")
    ap.add_argument("--doctor", action="store_true", help="check the machine, the mic, the model and the config")
    ap.add_argument("--deep", action="store_true", help="with --doctor: also load the model (slow)")
    ap.add_argument("--seconds", type=float, default=8.0, help="demo duration")
    args = ap.parse_args()

    _setup_console()
    current_platform().register_gpu_libraries()    # before anything imports ctranslate2

    if args.doctor:
        return doctor.run(deep=args.deep)
    if args.demo:
        return demo(args.seconds)

    cfg = config.load()
    if args.model:
        cfg["model"] = args.model
    # --test is a one-off run beside a live daemon; only the live daemon is single-instance
    if not args.test and not claim_single_instance():
        return 0

    d = Rawi(cfg)
    d.model = load_model(cfg)
    if args.test:
        return transcribe_file(d, args.test)
    return run_live(d)


if __name__ == "__main__":
    sys.exit(main())
