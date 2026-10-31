"""The Rawi dock: a small always-on-top capsule at the bottom of the screen.

Two buttons, with a hover label above them, the way Wispr Flow's dock reads:
    ( mic )  ( chevron )
    mic     -> toggle a latched dictation (the daemon does the work; this is a hand)
    chevron -> open the Hub window, or bring it forward if it is already open
A third button (an undo arrow) appears while the last paste can be swapped for the raw words.

The daemon starts this in its own process. Tk runs on this process's main thread (which macOS
requires and every other OS accepts); a worker thread polls the daemon's status every two
seconds and posts changes through a queue that the Tk loop drains.
"""
from __future__ import annotations

import json
import queue
import sys
import threading
import time
import tkinter as tk

from rawi_core import ipc, paths
from rawi_core.overlay import capsule
from rawi_core.system import current as current_platform

PLAT = current_platform()
HUB_TITLE = "Rawi"

BG = "#010203"           # the transparent key colour (Windows)
CARD = "#14100a"
EDGE = "#3d2f1c"
GOLD = "#e8b34a"
DIM = "#8a7a5e"
TEXT = "#e8dcc2"
SUB = "#9c8a6a"

W, H = 132, 52
R = 14
SWAP_EXTRA = 44          # the dock widens by this much while the undo arrow shows

KEY_LABELS = {"ctrl_l": "Left Ctrl", "ctrl_r": "Right Ctrl", "alt_l": "Left Alt",
              "caps_lock": "Caps Lock"}


def open_hub() -> None:
    """Bring the hub forward if it is visible, else start it."""
    if PLAT.focus_window(HUB_TITLE):
        return
    try:
        PLAT.spawn([PLAT.background_python(), paths.HUB_SCRIPT], cwd=paths.ROOT)
    except Exception:
        pass


def hold_key_label() -> str:
    """'Left Ctrl' for the configured hold key (re-read each time: the hub may change it)."""
    try:
        with open(paths.CONFIG, encoding="utf-8") as f:
            k = str(json.load(f).get("hold_key") or "ctrl_l")
    except Exception:
        k = "ctrl_l"
    return KEY_LABELS.get(k, k)


class Dock:
    """The capsule window. Build it on the main thread, then call run()."""

    def __init__(self):
        self._cmd: queue.Queue = queue.Queue()     # (kind, value) from any thread
        self._recording = False
        self._swap = False
        self._ear = False
        self.w = W
        self._drag_from = None
        self._moved = False

        root = tk.Tk()
        self.root = root
        root.overrideredirect(True)
        root.attributes("-topmost", True)
        self.bg = PLAT.prepare_overlay(root, BG, CARD)
        root.configure(bg=self.bg)

        sw, sh = root.winfo_screenwidth(), root.winfo_screenheight()
        x, y = (sw - W) // 2, sh - H - 56
        try:
            with open(paths.DOCK_POSITION, encoding="utf-8") as f:
                pos = json.load(f)
            x, y = int(pos["x"]), int(pos["y"])
        except Exception:
            pass
        root.geometry(f"{W}x{H}+{x}+{y}")

        self.cv = tk.Canvas(root, width=W, height=H, bg=self.bg, highlightthickness=0)
        self.cv.pack()
        self._paint()

        self.tip = tk.Toplevel(root)
        self.tip.overrideredirect(True)
        self.tip.attributes("-topmost", True)
        PLAT.prepare_overlay(self.tip, BG, CARD)
        self.tip.configure(bg=self.bg)
        self.tip.withdraw()
        self.tipcv = tk.Canvas(self.tip, bg=self.bg, highlightthickness=0)
        self.tipcv.pack()

        self.cv.bind("<Enter>", lambda e: self._tip("mic"))
        self.cv.bind("<Leave>", lambda e: self._untip())
        self.cv.bind("<Button-1>", self._click)
        self.cv.bind("<B1-Motion>", self._drag)
        self.cv.bind("<ButtonRelease-1>", self._drop)

        PLAT.make_passive(root, click_through=False)   # clickable, but never steals focus
        self._tick()

    # ---- public (any thread) ----
    def state(self, recording: bool) -> None:
        self._cmd.put(("state", bool(recording)))

    def ear(self, on: bool) -> None:
        self._cmd.put(("ear", bool(on)))

    def set_swap(self, on: bool) -> None:
        self._cmd.put(("swap", bool(on)))

    def quit(self) -> None:
        self._cmd.put(("quit", None))

    def run(self) -> None:
        """The Tk loop. Call on the main thread; returns after quit()."""
        self.root.mainloop()

    # ---- drawing ----
    def _paint(self) -> None:
        cv = self.cv
        cv.delete("all")
        w = self.w
        cv.create_polygon(capsule(1, 1, w - 1, H - 1, R, n=28), smooth=True,
                          fill=CARD, outline=EDGE, width=1)
        # mic glyph, left: body, cradle, stem
        cx, cy = 36, H // 2
        col = GOLD if self._recording else TEXT
        cv.create_oval(cx - 17, cy - 17, cx + 17, cy + 17,
                       fill="#3a1e14" if self._recording else ("#241a0c" if self._ear else "#1d1710"),
                       outline=(GOLD if self._ear and not self._recording else ""), width=1.4)
        cv.create_line(cx, cy - 9, cx, cy + 1, width=6.5, fill=col, capstyle="round")
        cv.create_arc(cx - 9, cy - 7, cx + 9, cy + 9, start=180, extent=180, style="arc",
                      outline=col, width=1.7)
        cv.create_line(cx, cy + 9, cx, cy + 12, width=1.7, fill=col, capstyle="round")
        # divider
        cv.create_line(66, 14, 66, H - 14, fill="#2b2114")
        if self._swap:
            # undo the cleaner, put the raw words back: a small turning arrow
            cv.create_text(90, H // 2 - 1, text="↺", fill=DIM, font=PLAT.font(15, symbol=True))
            cv.create_line(116, 14, 116, H - 14, fill="#2b2114")
            hx = 146
        else:
            hx = 96
        hy = H // 2
        for dx in (-5, 0, 5):                      # the chevron: three dots
            cv.create_oval(hx + dx - 2, hy - 2, hx + dx + 2, hy + 2, fill=DIM, outline="")

    # ---- mouse ----
    def _click(self, e) -> None:
        self._drag_from = (e.x_root, e.y_root)
        self._moved = False

    def _drag(self, e) -> None:
        if not self._drag_from:
            return
        dx = e.x_root - self._drag_from[0]
        dy = e.y_root - self._drag_from[1]
        if abs(dx) + abs(dy) > 3:
            self._moved = True
            x = self.root.winfo_x() + dx
            y = self.root.winfo_y() + dy
            self.root.geometry(f"{self.w}x{H}+{x}+{y}")
            self._drag_from = (e.x_root, e.y_root)
            self._untip()

    def _drop(self, e) -> None:
        if not self._moved:
            if e.x < 62:
                r = ipc.call("toggle", timeout=0.6)
                self.state(bool(r.get("recording")))
            elif self._swap and e.x < 118:
                r = ipc.call("swap", timeout=0.6)
                if not r.get("ok"):
                    self._tip("swap_err")
            else:
                open_hub()
        else:
            try:
                with open(paths.DOCK_POSITION, "w", encoding="utf-8") as f:
                    json.dump({"x": self.root.winfo_x(), "y": self.root.winfo_y()}, f)
            except Exception:
                pass
        self._drag_from = None

    # ---- hover label ----
    def _tip(self, which: str) -> None:
        title, sub = ("Dictation on", "Click to stop") if self._recording else \
                     ("Dictate", "Click to start, or hold " + hold_key_label())
        key = hold_key_label()
        w, h = 262, 54
        x = self.root.winfo_x() + (W - w) // 2
        y = self.root.winfo_y() - h - 6
        self.tip.geometry(f"{w}x{h}+{x}+{y}")
        c = self.tipcv
        c.configure(width=w, height=h)
        c.delete("all")
        c.create_polygon(capsule(1, 1, w - 1, h - 1, 10, n=28), smooth=True,
                         fill="#14100a", outline="#3d2f1c")
        c.create_text(12, 18, anchor="w", text=title, fill=TEXT, font=PLAT.font(9, bold=True))
        c.create_text(12, 37, anchor="w", text=sub, fill=SUB, font=PLAT.font(8))
        if key:
            tmp = c.create_text(w - 12, 18, anchor="e", text=key, fill=TEXT, font=PLAT.font(8, bold=True))
            kw = c.bbox(tmp)
            c.delete(tmp)
            c.create_polygon(capsule(kw[0] - 7, kw[1] - 4, kw[2] + 7, kw[3] + 4, 6, n=28),
                             smooth=True, fill="#241c12", outline="#4a3a22")
            c.create_text(w - 12, 18, anchor="e", text=key, fill=TEXT, font=PLAT.font(8, bold=True))
        self.tip.deiconify()
        try:
            self.tip.attributes("-topmost", True)
        except Exception:
            pass

    def _untip(self) -> None:
        try:
            self.tip.withdraw()
        except Exception:
            pass

    # ---- loop ----
    def _tick(self) -> None:
        """Apply queued state changes, then check again in 250 ms."""
        try:
            while True:
                kind, v = self._cmd.get_nowait()
                if kind == "state" and v != self._recording:
                    self._recording = v
                    self._paint()
                elif kind == "ear" and v != self._ear:
                    self._ear = v
                    self._paint()
                elif kind == "swap" and v != self._swap:
                    self._swap = v
                    self.w = W + SWAP_EXTRA if v else W
                    try:
                        self.root.geometry(f"{self.w}x{H}+{self.root.winfo_x()}+{self.root.winfo_y()}")
                        self.cv.configure(width=self.w)
                    except Exception:
                        pass
                    self._paint()
                elif kind == "quit":
                    self.root.quit()
                    return
        except queue.Empty:
            pass
        except Exception:
            pass
        try:
            self.root.after(250, self._tick)
        except Exception:
            pass


def _status_poll(dock: Dock, seconds: float = 2.0) -> None:
    """Mirror the daemon's state onto the dock, forever (a daemon thread)."""
    while True:
        st = ipc.call("status", timeout=0.4)
        if st.get("ok"):
            dock.state(bool(st.get("recording")))
            dock.set_swap(bool(st.get("swap")))
            dock.ear(bool(st.get("listening")))
        time.sleep(seconds)


def main() -> int:
    try:
        d = Dock()
    except Exception as e:
        print(f"[dock] could not open ({type(e).__name__}: {e})", flush=True)
        return 1
    if "--tip" in sys.argv:
        d.root.after(1200, lambda: d._tip("mic"))
    threading.Thread(target=_status_poll, args=(d,), daemon=True).start()
    d.run()
    return 0


if __name__ == "__main__":
    sys.exit(main())
