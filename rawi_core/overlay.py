"""The Rawi pill: a small, click-through capsule over whatever is on screen while you speak.

Tkinter only, no extra dependencies. Every public method (show / hide / flash / push) only puts a
message on a queue, so any thread may call them; the Tk thread drains the queues ~30 times a
second and is the only thread that touches a widget.

Which thread is "the Tk thread" depends on the OS (see system.Platform.tk_needs_main_thread):

    Windows / Linux   Wave() starts its own thread and returns; nothing else to do.
    macOS             AppKit aborts the process if a window is created off the main thread, so
                      Wave() builds the window right here (call it on the main thread) and the
                      caller hands the main thread over with wave.run() once everything else is
                      started. `wave.owns_main_thread` says which case you are in.
"""
from __future__ import annotations

import math
import queue
import signal
import threading
import time

from .system import current

BARS = 22
BAR_W = 4
GAP = 3
W = 300
H = 64
M = 6                    # margin, so the transparent key shows around the capsule
BG = "#010203"           # the transparent key colour (Windows); macOS fills with CARD
CARD = "#14100a"
EDGE = "#5a4320"
GOLD = "#e8b34a"
EMBER = "#9a6a26"
DIM = "#3a2a12"
TEXT = "#d8c49b"
OK_COLOUR = "#7fd6bd"
BAD_COLOUR = "#e8836a"


def capsule(x0, y0, x1, y1, r, n=24) -> list[float]:
    """Polygon points for a rounded rectangle (a capsule when r is half the height).
    Pass to canvas.create_polygon(..., smooth=True)."""
    pts = []
    for cx, cy, a0 in ((x1 - r, y0 + r, -90), (x1 - r, y1 - r, 0),
                       (x0 + r, y1 - r, 90), (x0 + r, y0 + r, 180)):
        for i in range(n // 4 + 1):
            a = math.radians(a0 + 90 * i / (n // 4))
            pts.append((cx + r * math.cos(a), cy + r * math.sin(a)))
    return [c for p in pts for c in p]


class Wave:
    """The pill. Construct once; call show / hide / flash / push from any thread."""

    def __init__(self, fps: int = 30, own_thread: bool | None = None):
        self.q: queue.Queue = queue.Queue()        # audio levels 0..1, newest last
        self._cmd: queue.Queue = queue.Queue()     # "show" | "hide" | "quit" | ("flash", ...)
        self._levels = [0.0 for _ in range(BARS)]
        self._fps = fps
        self._t = None
        self._build_error: BaseException | None = None
        self._ready = threading.Event()
        self._plat = current()
        if own_thread is None:
            own_thread = not self._plat.tk_needs_main_thread
        self._own_thread = own_thread
        if own_thread:
            threading.Thread(target=self._thread_main, daemon=True).start()
            # A slow Tk start (a busy disk at login) only delays the pill: messages queue up and
            # play once it is built. Only a build that actually failed is reported, so the
            # caller can fail open.
            self._ready.wait(timeout=5)
            if self._build_error is not None:
                raise RuntimeError(f"the pill window could not be created ({self._build_error})")
        else:
            self._build()                           # here and now, on the caller's (main) thread

    # ---------- public ----------
    @property
    def owns_main_thread(self) -> bool:
        """True when the caller must hand over the main thread with run()."""
        return not self._own_thread

    def flash(self, text: str, ok: bool = True, seconds: float = 2.2) -> None:
        """A receipt: say what just landed, in place, then go back to 'listening'."""
        try:
            self._cmd.put_nowait(("flash", str(text)[:64], bool(ok), float(seconds)))
        except Exception:
            pass

    def push(self, level: float) -> None:
        """One audio level (0..1) for the bars."""
        try:
            self.q.put_nowait(max(0.0, min(1.0, level)))
        except Exception:
            pass

    def show(self) -> None:
        self._put("show")

    def hide(self) -> None:
        self._put("hide")

    def quit(self) -> None:
        """Stop the Tk loop (run() returns)."""
        self._put("quit")

    def run(self) -> None:
        """Run the Tk loop on this (main) thread until quit() or Ctrl+C.

        Only needed when owns_main_thread is True; with its own thread it returns at once.
        """
        if self._own_thread or self._t is None:
            return
        try:
            # Tk swallows exceptions raised inside its callbacks, so a plain KeyboardInterrupt
            # would only print a traceback. Turn Ctrl+C into a clean quit instead.
            signal.signal(signal.SIGINT, lambda *_: self.quit())
        except Exception:
            pass
        self._t.mainloop()

    def demo(self, seconds: float = 6.0) -> None:
        """Fake speech so the pill can be seen without a microphone."""
        if self._own_thread:
            self._feed_demo(seconds)
            return
        threading.Thread(target=self._feed_demo, args=(seconds, True), daemon=True).start()
        self.run()

    # ---------- internals ----------
    def _put(self, cmd: str) -> None:
        try:
            self._cmd.put_nowait(cmd)
        except Exception:
            pass

    def _feed_demo(self, seconds: float, quit_after: bool = False) -> None:
        self.show()
        t0 = time.time()
        i = 0
        while time.time() - t0 < seconds:
            ph = (time.time() - t0)
            base = 0.25 + 0.22 * math.sin(ph * 2.4) + 0.10 * math.sin(ph * 7.3)
            burst = max(0.0, math.sin(ph * 1.1)) * 0.25
            noise = 0.06 * math.sin(ph * 31.0 + i)
            self.push(max(0.02, base + burst + noise))
            i += 1
            time.sleep(0.033)
        self.hide()
        if quit_after:
            time.sleep(0.1)
            self.quit()

    def _thread_main(self) -> None:
        try:
            self._build()
        except Exception as e:
            self._build_error = e
        finally:
            self._ready.set()                       # never leave the constructor waiting 5 s
        if self._t is not None:
            self._t.mainloop()

    def _build(self) -> None:
        """Create the window (hidden) and start the frame timer. Runs on the Tk thread."""
        import tkinter as tk

        plat = self._plat
        root = tk.Tk()
        root.overrideredirect(True)
        root.attributes("-topmost", True)
        bg = plat.prepare_overlay(root, BG, CARD)
        root.configure(bg=bg)
        sw = root.winfo_screenwidth()
        sh = root.winfo_screenheight()
        x = (sw - W) // 2
        y = sh - H - 72
        root.geometry(f"{W}x{H}+{x}+{y}")

        canvas = tk.Canvas(root, width=W, height=H, bg=bg, highlightthickness=0, bd=0)
        canvas.pack()

        canvas.create_polygon(capsule(M, M, W - M, H - M, (H - 2 * M) // 2),
                              smooth=True, fill=CARD, outline=EDGE, width=1)
        self._dot = canvas.create_oval(17, H // 2 - 4, 25, H // 2 + 4, fill=GOLD, outline="")
        self._label = canvas.create_text(33, H // 2, text="listening", anchor="w",
                                         fill=TEXT, font=plat.font(9))

        right = W - M - 14
        self._cx0 = right - BARS * (BAR_W + GAP) + GAP
        self._cy = H // 2
        self._rects = []
        for i in range(BARS):
            cx = self._cx0 + i * (BAR_W + GAP)
            self._rects.append(canvas.create_rectangle(cx, self._cy - 2, cx + BAR_W, self._cy + 2,
                                                       fill=DIM, outline=""))

        root.withdraw()
        self._t = root
        self._canvas = canvas
        self._flash_until = 0.0
        self._flash_text = ""
        self._flash_ok = True
        self._visible = False
        plat.make_passive(root, click_through=True)     # never take focus, let clicks through
        self._ready.set()
        self._tick()

    def _set_visible(self, on: bool) -> None:
        root = self._t
        if on and not self._visible:
            self._visible = True
            root.deiconify()
            root.attributes("-topmost", True)
            self._plat.make_passive(root, click_through=True)   # macOS: the NSWindow exists now
        elif not on and self._visible:
            self._visible = False
            root.withdraw()

    def _tick(self) -> None:
        """One frame: apply commands, roll the level history, redraw the bars and the label."""
        root, canvas = self._t, self._canvas
        try:
            while True:
                c = self._cmd.get_nowait()
                if isinstance(c, tuple) and c[0] == "flash":
                    self._flash_text, self._flash_ok, secs = c[1], c[2], c[3]
                    self._flash_until = time.time() + secs
                    self._set_visible(True)
                elif c == "show":
                    self._set_visible(True)
                elif c == "hide":
                    self._set_visible(False)
                elif c == "quit":
                    root.quit()
                    return
        except queue.Empty:
            pass

        try:
            while True:
                lv = self.q.get_nowait()
                self._levels.pop(0)
                self._levels.append(lv)
        except queue.Empty:
            self._levels.pop(0)
            self._levels.append(0.0)

        if self._visible:
            amp = 20.0
            now = time.time()
            for i, r in enumerate(self._rects):
                v = math.pow(min(1.0, self._levels[i] * 3.2), 0.65)
                wobble = 0.82 + 0.18 * math.sin(now * 6.0 + i * 0.55)
                h = 1.5 + v * amp * wobble
                col = GOLD if v > 0.55 else (EMBER if v > 0.18 else DIM)
                cx = self._cx0 + i * (BAR_W + GAP)
                canvas.coords(r, cx, self._cy - h, cx + BAR_W, self._cy + h)
                canvas.itemconfig(r, fill=col)
            if now < self._flash_until:
                colour = OK_COLOUR if self._flash_ok else BAD_COLOUR
                canvas.itemconfig(self._label, text=self._flash_text, fill=colour)
                canvas.itemconfig(self._dot, fill=colour)
            else:
                if canvas.itemcget(self._label, "text") != "listening":
                    canvas.itemconfig(self._label, text="listening", fill=TEXT)
                pulse = 0.75 + 0.25 * math.sin(now * 4.2)
                g = int(0.55 * pulse * 255)
                canvas.itemconfig(self._dot, fill="#%02x%02x%02x" % (0xe8, 0xb3, max(g, 0x4a)))
        root.after(int(1000 / self._fps), self._tick)
