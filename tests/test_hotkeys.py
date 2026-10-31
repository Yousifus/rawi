"""rawi_core.hotkeys: hold to talk, double-tap to latch, never steal a shortcut.

Driven by a fake clock and timer, so every case runs instantly and deterministically.
"""
import enum
import unittest

import _helpers
from rawi_core import hotkeys
from rawi_core.hotkeys import DOUBLE, HOLD, TAP, HoldKeyMachine, canonical_key


class FakeKey(enum.Enum):
    """pynput's Key enum as macOS builds it: ctrl_l shares ctrl's keycode, so it is an alias."""
    ctrl = 59
    ctrl_l = 59
    ctrl_r = 62
    f8 = 100
    f9 = 101


class WinKey(enum.Enum):
    """...and as Windows builds it: three distinct keys."""
    ctrl = 17
    ctrl_l = 162
    ctrl_r = 163
    f8 = 119


class FakeRecorder:
    def __init__(self):
        self.recording = False
        self.injecting = False
        self.calls = []

    def start_rec(self):
        if not self.recording:
            self.recording = True
            self.calls.append("start")

    def stop_and_process(self):
        if self.recording:
            self.recording = False
            self.calls.append("stop")

    def abort_rec(self):
        self.recording = False
        self.calls.append("abort")


class Base(unittest.TestCase):
    MODE = "hold"
    KEYS = WinKey

    def setUp(self):
        self.clock = _helpers.FakeClock()
        self.rec = FakeRecorder()
        self.fired = []
        self.latched = []
        self.m = HoldKeyMachine(self.rec, hold_key="ctrl_l", mode=self.MODE,
                                actions={"f8": lambda: self.fired.append("f8")},
                                on_latch=lambda: self.latched.append(True),
                                clock=self.clock.now, timer=self.clock.timer, keys=self.KEYS)

    def hold(self, seconds, key="ctrl_l"):
        self.m.on_press(key)
        self.clock.advance(seconds)
        self.m.on_release(key)

    def tap(self, key="ctrl_l"):
        self.hold(0.1, key)


class TestCanonicalKey(unittest.TestCase):
    def test_mac_alias_resolves_to_the_name_events_carry(self):
        self.assertEqual(canonical_key("ctrl_l", FakeKey), "ctrl")
        self.assertEqual(canonical_key("ctrl_r", FakeKey), "ctrl_r")

    def test_windows_keeps_left_ctrl(self):
        self.assertEqual(canonical_key("ctrl_l", WinKey), "ctrl_l")

    def test_characters_and_unknown_names_pass_through_lower_cased(self):
        self.assertEqual(canonical_key("A", WinKey), "a")
        self.assertEqual(canonical_key("not_a_key", WinKey), "not_a_key")
        self.assertEqual(canonical_key(None, WinKey), "")
        self.assertEqual(canonical_key(" F8 ", WinKey), "f8")


class TestHold(Base):
    def test_holding_alone_records_until_release(self):
        self.m.on_press("ctrl_l")
        self.clock.advance(HOLD + 0.01)
        self.assertEqual(self.rec.calls, ["start"])
        self.clock.advance(1.0)
        self.m.on_release("ctrl_l")
        self.assertEqual(self.rec.calls, ["start", "stop"])

    def test_a_shortcut_before_hold_does_nothing(self):
        self.m.on_press("ctrl_l")
        self.clock.advance(0.05)
        self.m.on_press("c")                      # Ctrl+C
        self.clock.advance(1.0)                   # the hold timer fires, but it is a combo
        self.m.on_release("ctrl_l")
        self.assertEqual(self.rec.calls, [])

    def test_a_shortcut_after_hold_discards_the_recording(self):
        self.m.on_press("ctrl_l")
        self.clock.advance(HOLD + 0.01)
        self.m.on_press("v")                      # held a beat, then Ctrl+V after all
        self.m.on_release("ctrl_l")
        self.assertEqual(self.rec.calls, ["start", "abort"])
        self.assertFalse(self.rec.recording)

    def test_key_repeat_does_not_restart_the_timer(self):
        self.m.on_press("ctrl_l")
        self.m.on_press("ctrl_l")
        self.m.on_press("ctrl_l")
        self.assertEqual(len(self.clock.timers), 1)

    def test_other_keys_alone_are_ignored(self):
        self.m.on_press("a")
        self.m.on_release("a")
        self.assertEqual(self.rec.calls, [])


class TestDoubleTap(Base):
    def test_double_tap_latches_and_a_tap_ends_it(self):
        self.tap()
        self.clock.advance(DOUBLE / 2)
        self.tap()
        self.assertEqual(self.rec.calls, ["start"])
        self.assertTrue(self.m.latched)
        self.assertEqual(self.latched, [True])
        self.clock.advance(5.0)                   # hands-free speech
        self.tap()
        self.assertEqual(self.rec.calls, ["start", "stop"])
        self.assertFalse(self.m.latched)

    def test_slow_taps_do_not_latch(self):
        self.tap()
        self.clock.advance(DOUBLE + 0.2)
        self.tap()
        self.assertEqual(self.rec.calls, [])

    def test_a_press_longer_than_a_tap_is_a_hold(self):
        self.hold(TAP + 0.1)                      # long enough to have started recording
        self.assertEqual(self.rec.calls, ["start", "stop"])


class TestFunctionKeys(Base):
    def test_actions_run_and_do_not_count_as_a_shortcut(self):
        self.m.on_press("f8")
        self.assertEqual(self.fired, ["f8"])
        self.assertEqual(self.rec.calls, [])

    def test_injected_keys_are_ignored(self):
        self.rec.injecting = True                 # rawi's own Ctrl+V coming back
        self.hold(1.0)
        self.m.on_press("f8")
        self.assertEqual(self.rec.calls, [])
        self.assertEqual(self.fired, [])

    def test_first_action_wins_a_clash(self):
        hits = []
        m = HoldKeyMachine(self.rec, hold_key="ctrl_l", keys=WinKey,
                           actions={"f9": lambda: hits.append(1), "F9": lambda: hits.append(2)})
        m.on_press("f9")
        self.assertEqual(hits, [1])

    def test_the_daemons_priority_order_survives_identical_keys(self):
        # talk_key and listen.key both "f8": voice mode wins, as it always did
        hits = []
        m = HoldKeyMachine(self.rec, hold_key="ctrl_l", keys=WinKey,
                           actions=[("f8", lambda: hits.append("talk")),
                                    ("f8", lambda: hits.append("ear"))])
        m.on_press("f8")
        self.assertEqual(hits, ["talk"])


class TestToggle(Base):
    MODE = "toggle"

    def test_each_press_flips_recording(self):
        self.hold(0.05)
        self.assertEqual(self.rec.calls, ["start"])
        self.hold(0.05)
        self.assertEqual(self.rec.calls, ["start", "stop"])


class TestLatchModeIsToggle(TestToggle):
    MODE = "latch"


class TestMacKeyNames(Base):
    """On a Mac the event for left Ctrl is named "ctrl"; the config still says "ctrl_l"."""
    KEYS = FakeKey

    def test_hold_works_with_the_aliased_name(self):
        self.m.on_press("ctrl")
        self.clock.advance(HOLD + 0.01)
        self.m.on_release("ctrl")
        self.assertEqual(self.rec.calls, ["start", "stop"])

    def test_right_ctrl_is_still_a_different_key(self):
        self.hold(1.0, key="ctrl_r")
        self.assertEqual(self.rec.calls, [])


class TestRealPynputNames(unittest.TestCase):
    def test_canonical_hold_key_matches_what_pynput_reports_here(self):
        try:
            from pynput import keyboard
        except Exception:
            self.skipTest("pynput not installed (CI installs numpy only)")
        self.assertEqual(canonical_key("ctrl_l"), keyboard.Key.ctrl_l.name)
        self.assertEqual(hotkeys.key_to_name(keyboard.Key.ctrl_l), canonical_key("ctrl_l"))


if __name__ == "__main__":
    unittest.main(verbosity=2)
