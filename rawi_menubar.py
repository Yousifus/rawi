"""The Rawi menu bar item: the macOS stand-in for the dock.

A mic in the menu bar that shows what the daemon is doing, and a menu that drives it:
    mic        -> idle, waiting for the hold key
    mic.fill   -> recording
    ear        -> the ear is on (F8)
    mic.slash  -> the daemon is not running

A status-bar menu never takes focus from the app being dictated into, which is why this
works on macOS where the Tk dock cannot. Its own process, started by rawi.py (start_dock,
which asks the system adapter for `dock_script`). By Abdulrahman Aldosari, tested on an M1 Pro.
"""
from __future__ import annotations

import json
import subprocess
import time

import objc
from AppKit import (NSApplication, NSImage, NSMenu, NSMenuItem, NSRunningApplication,
                    NSStatusBar, NSVariableStatusItemLength)
from Foundation import NSObject, NSTimer, NSUserDefaults
from PyObjCTools import AppHelper

from rawi_core import ipc, paths
from rawi_core.system import current as current_platform

PLAT = current_platform()
PREFERRED_POSITION = 330.0      # points from the right edge: just left of Wi-Fi and the clock
KEY_NAMES = {"ctrl_l": "left Ctrl", "ctrl": "left Ctrl", "ctrl_r": "right Ctrl",
             "alt_l": "left Option", "alt_r": "right Option", "caps_lock": "Caps Lock"}


def hold_key_name() -> str:
    try:
        with open(paths.CONFIG, encoding="utf-8") as f:
            key = json.load(f).get("hold_key") or "ctrl_l"
    except Exception:
        key = "ctrl_l"
    return KEY_NAMES.get(key, key)


def daemon_pid() -> int:
    """The running daemon's pid, or 0 (a pid file left by a crashed daemon does not count)."""
    try:
        with open(paths.DAEMON_PID, encoding="utf-8") as f:
            pid = int(f.read().strip())
        return pid if PLAT.pid_alive(pid) else 0
    except Exception:
        return 0


def hub_pid() -> int:
    out = subprocess.run(["pgrep", "-f", paths.HUB_SCRIPT],
                         capture_output=True, text=True).stdout.split()
    return int(out[0]) if out else 0


def start_daemon() -> None:
    PLAT.spawn([PLAT.background_python(), paths.DAEMON_SCRIPT], log_path=paths.DAEMON_SPAWN_LOG,
               cwd=paths.ROOT, detached=True, new_group=True)


def symbol(name: str):
    img = NSImage.imageWithSystemSymbolName_accessibilityDescription_(name, "Rawi")
    if img is not None:
        img.setTemplate_(True)          # follows the menu bar's light/dark tint
    return img


class Bar(NSObject):
    def init(self):
        self = objc.super(Bar, self).init()
        # New items go left of the others; on a full menu bar that is behind the notch.
        # Ask for a spot near the clock the first time (points from the right edge);
        # after that macOS remembers wherever it was ⌘-dragged.
        prefs = NSUserDefaults.standardUserDefaults()
        if prefs.objectForKey_("NSStatusItem Preferred Position rawi") is None:
            prefs.setDouble_forKey_(PREFERRED_POSITION, "NSStatusItem Preferred Position rawi")
        self.item = NSStatusBar.systemStatusBar().statusItemWithLength_(NSVariableStatusItemLength)
        self.item.setAutosaveName_("rawi")
        self.item.button().setToolTip_("Rawi")
        menu = NSMenu.alloc().init()
        menu.setAutoenablesItems_(False)
        self.status = self._add(menu, "Rawi", None)
        self.status.setEnabled_(False)
        menu.addItem_(NSMenuItem.separatorItem())
        self.dictate = self._add(menu, "Start dictation", "toggle:")
        self.ear = self._add(menu, "Always listening (F8)", "ear:")
        menu.addItem_(NSMenuItem.separatorItem())
        self._add(menu, "Open Rawi", "openHub:")
        self.restart = self._add(menu, "Restart dictation", "restart:")
        menu.addItem_(NSMenuItem.separatorItem())
        self._add(menu, "Quit Rawi", "quit:")
        self.item.setMenu_(menu)
        self.state = None
        self.tick_(None)
        NSTimer.scheduledTimerWithTimeInterval_target_selector_userInfo_repeats_(
            1.0, self, "tick:", None, True)
        return self

    @objc.python_method
    def _add(self, menu, title, action):
        it = NSMenuItem.alloc().initWithTitle_action_keyEquivalent_(title, action, "")
        if action:
            it.setTarget_(self)
        menu.addItem_(it)
        return it

    # ---- the poll: one IPC status a second, repaint only on change ----
    def tick_(self, _timer):
        st = ipc.call("status", timeout=0.4) if daemon_pid() else {"ok": False}
        if not st.get("ok"):
            state = ("off", False)
        else:
            state = ("rec" if st.get("recording") else "idle", bool(st.get("listening")))
        if state == self.state:
            return
        self.state = state
        mode, listening = state
        running = mode != "off"
        icon = {"off": "mic.slash", "rec": "mic.fill"}.get(mode, "ear" if listening else "mic")
        self.item.button().setImage_(symbol(icon))
        self.status.setTitle_({"off": "Rawi is not running",
                               "rec": "Recording…"}.get(mode, "Ear on: listening for speech" if listening
                                                         else f"Hold {hold_key_name()} to dictate"))
        self.dictate.setTitle_("Stop dictation" if mode == "rec" else
                               ("Start dictation" if running else "Start Rawi"))
        self.ear.setEnabled_(running)
        self.ear.setState_(1 if listening else 0)
        self.restart.setEnabled_(running)

    # ---- actions ----
    def toggle_(self, _sender):
        if daemon_pid():
            ipc.call("toggle")
        else:
            start_daemon()
        self.state = None

    def ear_(self, _sender):
        ipc.call("listen")
        self.state = None

    def openHub_(self, _sender):
        pid = hub_pid()
        app = NSRunningApplication.runningApplicationWithProcessIdentifier_(pid) if pid else None
        if app is not None:
            app.activateWithOptions_(1 << 1)        # NSApplicationActivateIgnoringOtherApps
            return
        PLAT.spawn([PLAT.background_python(), paths.HUB_SCRIPT], cwd=paths.ROOT, detached=True)

    def restart_(self, _sender):
        pid = daemon_pid()
        if pid:
            PLAT.kill(pid)
            for _ in range(30):                     # up to 3 s, or the new one sees it alive and exits
                if not daemon_pid():
                    break
                time.sleep(0.1)
        start_daemon()
        self.state = None

    def quit_(self, _sender):
        """Quit means quit: the daemon too, so the memory comes back."""
        pid = daemon_pid()
        if pid:
            PLAT.kill(pid)
        hub = hub_pid()
        if hub:
            PLAT.kill(hub)
        AppHelper.stopEventLoop()


def main():
    app = NSApplication.sharedApplication()
    app.setActivationPolicy_(1)                   # Accessory: no Dock icon, never takes focus
    bar = Bar.alloc().init()                      # noqa: F841 (kept alive by the run loop)
    AppHelper.runEventLoop()


if __name__ == "__main__":
    main()
