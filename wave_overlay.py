"""The Rawi pill: a small, click-through capsule over whatever is on screen while you speak.

Tkinter only, no dependencies. Runs its own thread so the recorder never waits for a frame.
"""
from __future__ import annotations

import math
import queue
import threading
import time

BARS = 22
BAR_W = 4
GAP = 3
W = 300
H = 64
M = 6                    # margin, so the transparent key shows around the capsule
BG = "#010203"           # the transparent key colour
CARD = "#14100a"
EDGE = "#5a4320"
GOLD = "#e8b34a"
EMBER = "#9a6a26"
DIM = "#3a2a12"
TEXT = "#d8c49b"


def _capsule(x0, y0, x1, y1, r, n=24):
    pts = []
    for cx, cy, a0 in ((x1 - r, y0 + r, -90), (x1 - r, y1 - r, 0),
                       (x0 + r, y1 - r, 90), (x0 + r, y0 + r, 180)):
        for i in range(n // 4 + 1):
            a = math.radians(a0 + 90 * i / (n // 4))
            pts.append((cx + r * math.cos(a), cy + r * math.sin(a)))
    return [c for p in pts for c in p]


class Wave:
    def __init__(self, fps: int = 30):
        self.q: queue.Queue = queue.Queue()
        self._cmd: queue.Queue = queue.Queue()
        self._levels = [0.0 for _ in range(BARS)]
        self._fps = fps
        self._t = None
        self._ready = threading.Event()
        t = threading.Thread(target=self._run, daemon=True)
        t.start()
        self._ready.wait(timeout=5)

    # ---------- public ----------
    def flash(self, text: str, ok: bool = True, seconds: float = 2.2) -> None:
        """A receipt: say what just landed, in place, then go back to 'listening'."""
        try:
            self._cmd.put_nowait(("flash", str(text)[:64], bool(ok), float(seconds)))
        except Exception:
            pass

    def push(self, level: float) -> None:
        try:
            self.q.put_nowait(max(0.0, min(1.0, level)))
        except Exception:
            pass

    def show(self) -> None:
        try:
            self._cmd.put_nowait("show")
        except Exception:
            pass

    def hide(self) -> None:
        try:
            self._cmd.put_nowait("hide")
        except Exception:
            pass

    # ---------- internals ----------
    def _run(self) -> None:
        import tkinter as tk

        root = tk.Tk()
        root.overrideredirect(True)
        root.attributes("-topmost", True)
        root.attributes("-transparentcolor", BG)
        root.configure(bg=BG)
        sw = root.winfo_screenwidth()
        sh = root.winfo_screenheight()
        x = (sw - W) // 2
        y = sh - H - 72
        root.geometry(f"{W}x{H}+{x}+{y}")

        canvas = tk.Canvas(root, width=W, height=H, bg=BG, highlightthickness=0, bd=0)
        canvas.pack()

        canvas.create_polygon(_capsule(M, M, W - M, H - M, (H - 2 * M) // 2),
                              smooth=True, fill=CARD, outline=EDGE, width=1)
        dot = canvas.create_oval(17, H // 2 - 4, 25, H // 2 + 4, fill=GOLD, outline="")
        label = canvas.create_text(33, H // 2, text="listening", anchor="w",
                                   fill=TEXT, font=("Segoe UI", 9))

        right = W - M - 14
        cx0 = right - BARS * (BAR_W + GAP) + GAP
        cy = H // 2
        rects = []
        for i in range(BARS):
            cx = cx0 + i * (BAR_W + GAP)
            rects.append(canvas.create_rectangle(cx, cy - 2, cx + BAR_W, cy + 2, fill=DIM, outline=""))

        root.withdraw()
        self._t = root
        self._canvas = canvas
        self._rects = rects
        self._dot = dot
        self._label = label
        self._flash_until = 0.0
        self._flash_text = ""
        self._flash_ok = True
        self._cx0 = cx0
        self._cy = cy
        self._visible = False
        self._ready.set()

        # click-through + never take focus
        try:
            import ctypes
            GWL_EXSTYLE = -20
            WS_EX_TRANSPARENT = 0x00000020
            WS_EX_NOACTIVATE = 0x08000000
            WS_EX_TOOLWINDOW = 0x00000080
            hwnd = ctypes.windll.user32.GetParent(root.winfo_id())
            styles = ctypes.windll.user32.GetWindowLongW(hwnd, GWL_EXSTYLE)
            ctypes.windll.user32.SetWindowLongW(
                hwnd, GWL_EXSTYLE,
                styles | WS_EX_TRANSPARENT | WS_EX_NOACTIVATE | WS_EX_TOOLWINDOW)
        except Exception:
            pass

        dt = 1.0 / self._fps

        def tick():
            try:
                while True:
                    c = self._cmd.get_nowait()
                    if isinstance(c, tuple) and c[0] == "flash":
                        self._flash_text, self._flash_ok, secs = c[1], c[2], c[3]
                        self._flash_until = time.time() + secs
                        if not self._visible:
                            self._visible = True
                            root.deiconify()
                            root.attributes("-topmost", True)
                        continue
                    if c == "show" and not self._visible:
                        self._visible = True
                        root.deiconify()
                        root.attributes("-topmost", True)
                    elif c == "hide" and self._visible:
                        self._visible = False
                        root.withdraw()
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
                    raw = self._levels[i]
                    v = math.pow(min(1.0, raw * 3.2), 0.65)
                    wobble = 0.82 + 0.18 * math.sin(now * 6.0 + i * 0.55)
                    h = 1.5 + v * amp * wobble
                    col = GOLD if v > 0.55 else (EMBER if v > 0.18 else DIM)
                    cx = self._cx0 + i * (BAR_W + GAP)
                    canvas.coords(r, cx, self._cy - h, cx + BAR_W, self._cy + h)
                    canvas.itemconfig(r, fill=col)
                if now < self._flash_until:
                    canvas.itemconfig(self._label, text=self._flash_text,
                                      fill=("#7fd6bd" if self._flash_ok else "#e8836a"))
                    canvas.itemconfig(self._dot, fill=("#7fd6bd" if self._flash_ok else "#e8836a"))
                else:
                    if canvas.itemcget(self._label, "text") != "listening":
                        canvas.itemconfig(self._label, text="listening", fill=TEXT)
                pulse = 0.75 + 0.25 * math.sin(now * 4.2)
                g = int(0.55 * pulse * 255)
                canvas.itemconfig(self._dot, fill="#%02x%02x%02x" % (0xe8, 0xb3, g if g > 0x4a else 0x4a))
            root.after(int(dt * 1000), tick)

        tick()
        root.mainloop()

    def demo(self, seconds: float = 6.0) -> None:
        """Fake speech so the pill can be seen without a microphone."""
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
