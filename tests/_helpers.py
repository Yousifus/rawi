"""Shared test setup: put the repository root on sys.path, plus small fakes used across files.

Imported first by every test module, so `python -m unittest discover -s tests` works from the
repository root with nothing but numpy installed.
"""
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)


class FakeClock:
    """A clock and threading.Timer stand-in that only moves when a test says so."""

    def __init__(self, start: float = 1000.0):
        self.t = start
        self.timers = []

    def now(self) -> float:
        return self.t

    def timer(self, seconds, fn):
        tm = FakeTimer(self, seconds, fn)
        self.timers.append(tm)
        return tm

    def advance(self, seconds: float) -> None:
        self.t += seconds
        for tm in list(self.timers):
            if tm.started and not tm.cancelled and not tm.fired and self.t >= tm.due:
                tm.fired = True
                tm.fn()


class FakeTimer:
    def __init__(self, clock, seconds, fn):
        self.clock, self.seconds, self.fn = clock, seconds, fn
        self.started = self.cancelled = self.fired = False
        self.daemon = False
        self.due = None

    def start(self):
        self.started = True
        self.due = self.clock.t + self.seconds

    def cancel(self):
        self.cancelled = True


class FakePlatform:
    """The system adapter, minus the system: records chords, never beeps or shells out."""

    name = "fake"
    tk_needs_main_thread = False

    def __init__(self, app: str = "Notes"):
        self.app = app
        self.chords = []

    def beep(self, freq, ms):
        pass

    def active_app(self):
        return self.app

    def paste_chord(self):
        return ("ctrl", "v")

    def undo_chord(self):
        return ("ctrl", "z")

    def send_chord(self, keys):
        self.chords.append(tuple(keys))

    def input_permission(self):
        return None
