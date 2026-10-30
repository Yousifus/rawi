"""The Rawi dock: a small always-on-top capsule at the bottom of the screen.

Two buttons, with a hover label above them, the way Wispr Flow's dock reads:
    ( mic )  ( chevron )
    mic     -> toggle a latched dictation (the daemon does the work; this is a hand)
    chevron -> open the Hub window, or bring it forward if it is already open

Tkinter only. Its own root, its own thread, transparent colour key, no focus stealing.
"""
from __future__ import annotations

import ctypes
import json
import math
import os
import socket
import subprocess
import sys
import threading
import time
import tkinter as tk

ROOT = os.path.dirname(os.path.abspath(__file__))
POS = os.path.join(ROOT, "dock.json")
IPC = ("127.0.0.1", 18090)

BG = "#010203"
CARD = "#14100a"
EDGE = "#3d2f1c"
GOLD = "#e8b34a"
DIM = "#8a7a5e"
TEXT = "#e8dcc2"
SUB = "#9c8a6a"

W, H = 132, 52
R = 14


def _capsule(x0, y0, x1, y1, r, n=28):
    pts = []
    for cx, cy, a0 in ((x1 - r, y0 + r, -90), (x1 - r, y1 - r, 0),
                       (x0 + r, y1 - r, 90), (x0 + r, y0 + r, 180)):
        for i in range(n // 4 + 1):
            a = math.radians(a0 + 90 * i / (n // 4))
            pts.append((cx + r * math.cos(a), cy + r * math.sin(a)))
    return [c for p in pts for c in p]


def ipc(cmd: str, timeout: float = 0.6):
    try:
        with socket.create_connection(IPC, timeout=timeout) as s:
            s.sendall((json.dumps({"cmd": cmd}) + "\n").encode())
            s.settimeout(timeout)
            buf = b""
            while not buf.endswith(b"\n"):
                chunk = s.recv(4096)
                if not chunk:
                    break
                buf += chunk
        return json.loads(buf.decode() or "{}")
    except Exception as e:
        return {"ok": False, "error": type(e).__name__}


def hub_window():
    """The Hub's hwnd if it is open, else None."""
    try:
        import win32gui
    except Exception:
        return None
    found = []
    def cb(hwnd, res):
        try:
            if win32gui.IsWindowVisible(hwnd) and win32gui.GetWindowText(hwnd).strip() == "Rawi":
                res.append(hwnd)
        except Exception:
            pass
    win32gui.EnumWindows(cb, found)
    return found[0] if found else None


def open_hub():
    hwnd = hub_window()
    if hwnd:
        try:
            import win32gui
            import win32con
            if win32gui.IsIconic(hwnd):
                win32gui.ShowWindow(hwnd, win32con.SW_RESTORE)
            win32gui.SetForegroundWindow(hwnd)
            return
        except Exception:
            pass
    exe = sys.executable
    pyw = os.path.join(os.path.dirname(exe), "pythonw.exe")
    if os.path.exists(pyw):
        exe = pyw
    try:
        subprocess.Popen([exe, os.path.join(ROOT, "rawi_app.py")],
                         creationflags=0x08000000)
    except Exception:
        pass


class Dock:
    def __init__(self, on_ready=None):
        self._ready = threading.Event()
        self._cmd = []
        self._recording = False
        self._swap = False
        self._ear = False
        self.w = W
        self._t = threading.Thread(target=self._run, daemon=True)
        self._t.start()
        self._ready.wait(timeout=6)

    # ---- public ----
    def state(self, recording: bool):
        try:
            self._cmd.append(("state", recording))
        except Exception:
            pass

    def ear(self, on: bool):
        on = bool(on)
        if on != self._ear:
            self._ear = on
            try:
                self._cmd.append(("ear", on))
            except Exception:
                pass

    def quit(self):
        self._cmd.append(("quit", None))

    # ---- thread ----
    def _run(self):
        try:
            root = tk.Tk()
        except Exception:
            self._ready.set()
            return
        self.root = root
        root.overrideredirect(True)
        root.attributes("-topmost", True)
        try:
            root.attributes("-transparentcolor", BG)
        except Exception:
            pass
        root.configure(bg=BG)

        sw = root.winfo_screenwidth()
        sh = root.winfo_screenheight()
        x, y = (sw - W) // 2, sh - H - 56
        try:
            pos = json.load(open(POS, encoding="utf-8"))
            x, y = int(pos["x"]), int(pos["y"])
        except Exception:
            pass
        root.geometry(f"{W}x{H}+{x}+{y}")

        self.cv = tk.Canvas(root, width=W, height=H, bg=BG, highlightthickness=0)
        self.cv.pack()
        self._paint()

        self.tip = tk.Toplevel(root)
        self.tip.overrideredirect(True)
        self.tip.attributes("-topmost", True)
        try:
            self.tip.attributes("-transparentcolor", BG)
        except Exception:
            pass
        self.tip.configure(bg=BG)
        self.tip.withdraw()
        self.tipcv = tk.Canvas(self.tip, bg=BG, highlightthickness=0)
        self.tipcv.pack()

        self.cv.bind("<Enter>", lambda e: self._tip("mic"))
        self.cv.bind("<Leave>", lambda e: self._untip())
        self.cv.bind("<Button-1>", self._click)
        self.cv.bind("<B1-Motion>", self._drag)
        self.cv.bind("<ButtonRelease-1>", self._drop)
        self._drag_from = None
        self._moved = False

        self._no_activate(root)
        self._ready.set()
        self._tick()
        root.mainloop()

    def _no_activate(self, root):
        try:
            hwnd = ctypes.windll.user32.GetParent(root.winfo_id()) or root.winfo_id()
            GWL_EXSTYLE = -20
            WS_EX_TOOLWINDOW, WS_EX_NOACTIVATE = 0x00000080, 0x08000000
            cur = ctypes.windll.user32.GetWindowLongW(hwnd, GWL_EXSTYLE)
            ctypes.windll.user32.SetWindowLongW(hwnd, GWL_EXSTYLE,
                                                cur | WS_EX_TOOLWINDOW | WS_EX_NOACTIVATE)
        except Exception:
            pass

    def _paint(self):
        cv = self.cv
        cv.delete("all")
        w = self.w
        cv.create_polygon(_capsule(1, 1, w - 1, H - 1, R), smooth=True,
                          fill=CARD, outline=EDGE, width=1)
        # mic glyph, left: body, cradle, stem
        cx, cy = 36, H // 2
        col = GOLD if self._recording else TEXT
        cv.create_oval(cx - 17, cy - 17, cx + 17, cy + 17,
                       fill="#3a1e14" if self._recording else ("#241a0c" if self._ear else "#1d1710"),
                       outline=("#e8b34a" if self._ear and not self._recording else ""), width=1.4)
        cv.create_line(cx, cy - 9, cx, cy + 1, width=6.5, fill=col, capstyle="round")
        cv.create_arc(cx - 9, cy - 7, cx + 9, cy + 9, start=180, extent=180, style="arc",
                      outline=col, width=1.7)
        cv.create_line(cx, cy + 9, cx, cy + 12, width=1.7, fill=col, capstyle="round")
        # divider
        cv.create_line(66, 14, 66, H - 14, fill="#2b2114")
        if self._swap:
            # undo the cleaner, put his own words back: a small turning arrow
            ux, uy = 90, H // 2
            cv.create_text(ux, uy - 1, text="\u21ba", fill=DIM,
                           font=("Segoe UI Symbol", 15))
            cv.create_line(116, 14, 116, H - 14, fill="#2b2114")
            hx = 146
        else:
            hx = 96
        hy = H // 2
        for dx in (-5, 0, 5):
            cv.create_oval(hx + dx - 2, hy - 2, hx + dx + 2, hy + 2, fill=DIM, outline="")

    def set_swap(self, on):
        on = bool(on)
        if on != self._swap:
            self._swap = on
            self._cmd.append(("swapflag", on))

    def _click(self, e):
        self._drag_from = (e.x_root, e.y_root)
        self._moved = False

    def _drag(self, e):
        if not self._drag_from:
            return
        dx = e.x_root - self._drag_from[0]
        dy = e.y_root - self._drag_from[1]
        if abs(dx) + abs(dy) > 3:
            self._moved = True
            x = self.root.winfo_x() + dx
            y = self.root.winfo_y() + dy
            self.root.geometry(f"{W}x{H}+{x}+{y}")
            self._drag_from = (e.x_root, e.y_root)
            self._untip()

    def _drop(self, e):
        if not self._moved:
            if e.x < 62:
                r = ipc("toggle")
                self.state(bool(r.get("recording")))
            elif self._swap and e.x < 118:
                r = ipc("swap")
                if not r.get("ok"):
                    self._tip("swap_err")
            else:
                open_hub()
        else:
            try:
                json.dump({"x": self.root.winfo_x(), "y": self.root.winfo_y()},
                          open(POS, "w", encoding="utf-8"))
            except Exception:
                pass
        self._drag_from = None

    # ---- hover label ----
    def _tip(self, which):
        title, sub = ("Dictation on", "Click to stop") if self._recording else \
                     ("Dictate", "Click to start, or hold Left Ctrl")
        key = self._key_label()
        w, h = 262, 54
        x = self.root.winfo_x() + (W - w) // 2
        y = self.root.winfo_y() - h - 6
        self.tip.geometry(f"{w}x{h}+{x}+{y}")
        c = self.tipcv
        c.configure(width=w, height=h)
        c.delete("all")
        c.create_polygon(_capsule(1, 1, w - 1, h - 1, 10), smooth=True,
                         fill="#14100a", outline="#3d2f1c")
        c.create_text(12, 18, anchor="w", text=title, fill=TEXT,
                      font=("Segoe UI Semibold", 9))
        c.create_text(12, 37, anchor="w", text=sub, fill=SUB, font=("Segoe UI", 8))
        if key:
            tmp = c.create_text(w - 12, 18, anchor="e", text=key, fill=TEXT,
                                font=("Segoe UI Semibold", 8))
            kw = c.bbox(tmp)
            c.delete(tmp)
            c.create_polygon(_capsule(kw[0] - 7, kw[1] - 4, kw[2] + 7, kw[3] + 4, 6),
                             smooth=True, fill="#241c12", outline="#4a3a22")
            c.create_text(w - 12, 18, anchor="e", text=key, fill=TEXT,
                          font=("Segoe UI Semibold", 8))
        self.tip.deiconify()
        try:
            self.tip.attributes("-topmost", True)
        except Exception:
            pass

    def _key_label(self):
        try:
            cfg = json.load(open(os.path.join(ROOT, "config.json"), encoding="utf-8"))
            k = str(cfg.get("hold_key") or "ctrl_l")
        except Exception:
            k = "ctrl_l"
        return {"ctrl_l": "Left Ctrl", "ctrl_r": "Right Ctrl", "alt_l": "Left Alt",
                "caps_lock": "Caps Lock"}.get(k, k)

    def _untip(self):
        try:
            self.tip.withdraw()
        except Exception:
            pass

    # ---- loop ----
    def _tick(self):
        try:
            while self._cmd:
                k, v = self._cmd.pop(0)
                if k == "state":
                    if bool(v) != self._recording:
                        self._recording = bool(v)
                        self._paint()
                elif k == "ear":
                    self._ear = bool(v)
                    self._paint()
                elif k == "swapflag":
                    self.w = W + 44 if v else W
                    try:
                        self.root.geometry(f"{self.w}x{H}+{self.root.winfo_x()}+{self.root.winfo_y()}")
                        self.cv.configure(width=self.w)
                    except Exception:
                        pass
                    self._paint()
                elif k == "quit":
                    self.root.quit()
                    return
            if self._recording:
                self.cv.itemconfigure("all")
        except Exception:
            pass
        try:
            self.root.after(250, self._tick)
        except Exception:
            pass


def _status_poll(dock, seconds=2.0):
    while True:
        st = ipc("status", timeout=0.4)
        if st.get("ok"):
            dock.state(bool(st.get("recording")))
            dock.set_swap(bool(st.get("swap")))
            dock.ear(bool(st.get("listening")))
        time.sleep(seconds)


def main():
    d = Dock()
    if "--tip" in sys.argv:
        threading.Timer(1.2, lambda: d._tip("mic")).start()
    threading.Thread(target=_status_poll, args=(d,), daemon=True).start()
    while True:
        time.sleep(3600)


if __name__ == "__main__":
    main()
