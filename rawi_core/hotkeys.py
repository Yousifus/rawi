"""The hold key: hold to talk, double-tap to latch, and never steal a shortcut.

Left Ctrl is also half of Ctrl+C, Ctrl+V and every other shortcut, so a press is not a
recording until it has been held alone for HOLD seconds. The rules, in order:

    held alone >= HOLD               start recording; release stops it and transcribes
    another key while still pending  a shortcut: nothing happens ("combo")
    another key after it started     a shortcut after all: discard the recording, no beep
    two taps inside DOUBLE           latch: recording runs hands-free; the next tap stops it
    mode "toggle" (or "latch")       every press flips recording on / off

Function keys (paste last, language lock, voice mode, the ear) are dispatched first.

Key names
---------
pynput names keys by its `Key` enum. On macOS left Ctrl and plain Ctrl share one keycode, so
`Key.ctrl_l` *is* `Key.ctrl` and events arrive named "ctrl", never "ctrl_l". Comparing the
config's "ctrl_l" against that name meant the hold key never fired on a Mac. `canonical_key()`
maps a config name to the name events actually carry on this OS.

The machine takes its clock and timer as arguments, so tests drive it without real time:

    m = HoldKeyMachine(recorder, hold_key="ctrl_l", clock=fake.now, timer=fake.timer)
    m.on_press("ctrl_l"); fake.advance(0.3); m.on_release("ctrl_l")
"""
from __future__ import annotations

import threading
import time
from typing import Callable, Iterable, Protocol

TAP = 0.30      # a press shorter than this is a tap, not a hold
DOUBLE = 0.55   # two taps inside this window latch the recorder on
HOLD = 0.22     # the hold key alone this long, with no other key, is a recording


class Recorder(Protocol):
    """What the machine drives. `rawi.Rawi` is the real one; tests pass a fake."""

    recording: bool
    injecting: bool

    def start_rec(self) -> None: ...
    def stop_and_process(self) -> None: ...
    def abort_rec(self) -> None: ...


def canonical_key(name: str | None, keys=None) -> str:
    """The name pynput will report for the key called `name` on this OS.

    `keys` is the enum to resolve against (pynput's `keyboard.Key` by default). Enum lookup by
    name returns the canonical member for aliases, which is exactly what events carry.
    Character keys ("a", "`") and unknown names pass through lower-cased.
    """
    name = str(name or "").strip().lower()
    if keys is None:
        try:
            from pynput import keyboard
            keys = keyboard.Key
        except Exception:
            return name
    try:
        return keys[name].name
    except Exception:
        return name


def key_to_name(k) -> str:
    """A pynput key event -> the name compared against config ("ctrl_l", "f8", "a")."""
    from pynput import keyboard
    if isinstance(k, keyboard.Key):
        return k.name
    try:
        return k.char
    except Exception:
        return str(k)


class HoldKeyMachine:
    """Turns hold-key and function-key events into recorder calls.

    recorder  anything with recording / injecting / start_rec / stop_and_process / abort_rec
    hold_key  config name of the hold key ("ctrl_l"); canonicalised for this OS
    mode      "hold" (default), "toggle", or "latch" (treated as toggle)
    actions   [(config key name, callable), ...] for single-press keys (F7..F10 by default),
              in priority order: checked before the hold key, and the first entry wins when two
              share a key (a dict is accepted too; its insertion order is the priority)
    on_latch  called after a double-tap latches (the daemon plays its two-note beep)
    clock     returns seconds; time.time by default
    timer     threading.Timer-compatible factory: timer(seconds, fn) -> object with start/cancel
    keys      the key enum used for canonical names (tests pass a fake)
    """

    def __init__(self, recorder: Recorder, *, hold_key: str, mode: str = "hold",
                 actions: Iterable[tuple[str, Callable[[], object]]] | dict | None = None,
                 on_latch: Callable[[], None] | None = None,
                 clock: Callable[[], float] = time.time, timer=threading.Timer, keys=None):
        self.rec = recorder
        self.hold_key = canonical_key(hold_key, keys)
        self.mode = "toggle" if str(mode).lower() in ("toggle", "latch") else "hold"
        self.actions: dict[str, Callable[[], object]] = {}
        pairs = actions.items() if isinstance(actions, dict) else (actions or [])
        for k, fn in pairs:
            if k:
                self.actions.setdefault(canonical_key(k, keys), fn)   # earlier entries win a clash
        self.on_latch = on_latch
        self.clock = clock
        self.timer_factory = timer
        # state, named as in the original closures
        self.down = False           # the hold key is physically down
        self.latched = False        # a double-tap recording is running hands-free
        self.tap_at = 0.0           # when the last short tap ended (for double-tap)
        self.press_at = 0.0         # when the current press began
        self.pending = False        # pressed, waiting HOLD seconds to see if it is a shortcut
        self.combo = False          # another key arrived during this press: a shortcut
        self.by_hold = False        # the current recording was started by holding
        self._timer = None

    # ---------------------------------------------------------------- events
    def on_press(self, name: str) -> None:
        if self.rec.injecting:                   # our own Ctrl+V, not the speaker
            return
        action = self.actions.get(name)
        if action is not None:
            action()
            return
        if name != self.hold_key:
            if self.pending:
                self.combo = True
                if self.rec.recording and self.by_hold:
                    self.rec.abort_rec()          # held long enough, then a letter: a shortcut
            return
        if self.down:                             # key repeat while held
            return
        self.down = True
        self.press_at = self.clock()
        self.combo = False

        if self.latched:                          # a single tap ends a latched recording
            self.latched = False
            self.rec.stop_and_process()
            return

        if self.mode == "toggle":
            if self.rec.recording:
                self.rec.stop_and_process()
            else:
                self.rec.start_rec()
            return

        # hold mode: wait to see whether this is the key alone or the start of a shortcut
        self.pending = True
        self.by_hold = False
        self._timer = self.timer_factory(HOLD, self._fire)
        try:
            self._timer.daemon = True
        except Exception:
            pass
        self._timer.start()

    def on_release(self, name: str) -> None:
        if name != self.hold_key or not self.down:
            return
        self.down = False
        held = self.clock() - self.press_at
        was_pending = self.pending
        self._cancel_pending()

        if self.mode != "hold":
            return
        if self.combo and not self.rec.recording:
            return                                # a shortcut: nothing happened, nothing to undo
        if self.rec.recording and not was_pending:
            self.rec.stop_and_process()           # a latched tap ends here
            return
        if held < TAP and not self.rec.recording:
            now = self.clock()
            if now - self.tap_at < DOUBLE:
                self.tap_at = 0.0
                self.latched = True
                self.rec.start_rec()
                if self.on_latch is not None:
                    self.on_latch()
            else:
                self.tap_at = now
            return
        if self.rec.recording:
            self.rec.stop_and_process()

    # ---------------------------------------------------------------- internals
    def _fire(self) -> None:
        """HOLD elapsed. Only a plain, still-held key starts the microphone: Ctrl+C, Ctrl+V and
        every other shortcut set `combo` and never get here."""
        if self.pending and self.down and not self.combo and not self.rec.recording:
            self.by_hold = True
            self.rec.start_rec()

    def _cancel_pending(self) -> None:
        self.pending = False
        if self._timer is not None:
            try:
                self._timer.cancel()
            except Exception:
                pass
            self._timer = None
