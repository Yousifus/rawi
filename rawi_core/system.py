"""The system adapter: every Windows / macOS / Linux difference rawi depends on.

Why this file exists
--------------------
rawi grew up on Windows, and Windows-only calls were spread through every script:
`creationflags=...`, `taskkill`, `pythonw.exe`, `ctypes.windll`, `os.startfile`, PowerShell.
On macOS several of those raise before anything useful happens (the hub crashed at launch on
`creationflags`), and Tk windows must be created on the main thread or the process aborts.

The rest of the code now says *what* it wants ("spawn this detached", "is this pid alive",
"send the paste chord") and this module decides *how* on the OS at hand:

    from rawi_core.system import current
    plat = current()
    plat.spawn([plat.background_python(), paths.DAEMON_SCRIPT], log_path=..., detached=True)

Rules
-----
1. No `sys.platform` checks anywhere else. A new OS difference becomes a method here.
2. Methods fail open: they return False / "" / None instead of raising, unless the docstring
   says otherwise. A missing beep or window title must never break a dictation.
3. Imports of optional, OS-specific packages (ctypes.windll, pyobjc, pyautogui) stay inside the
   methods, so `for_platform("win32")` can be built and tested on any machine.

Classes
-------
    Platform          shared POSIX-ish behaviour (also the Linux / X11 adapter)
    WindowsPlatform   the tested path: winsound, pygetwindow, taskkill, pythonw, CUDA DLLs
    MacPlatform       osascript, afplay, `open`, Accessibility check, main-thread Tk
    current()         the adapter for this machine (cached)
    for_platform(n)   the adapter for a given sys.platform string (tests use this)
"""
from __future__ import annotations

import os
import signal
import subprocess
import sys
from functools import lru_cache

# Windows process-creation flags. subprocess.CREATE_NO_WINDOW and friends only exist on Windows
# builds of Python, so they are spelled out here and passed only by WindowsPlatform.
CREATE_NO_WINDOW = 0x08000000
DETACHED_PROCESS = 0x00000008
CREATE_NEW_PROCESS_GROUP = 0x00000200


class Platform:
    """Behaviour shared by POSIX systems; used as-is for Linux (X11).

    Subclasses override only what differs. Class attributes are plain facts other modules read.
    """

    name = "linux"
    #: the modifier of the paste / undo chords, as pyautogui spells it
    command_key = "ctrl"
    #: True when Tk windows must be created and driven from the main thread (macOS)
    tk_needs_main_thread = False
    #: True when a pystray tray icon may run on a worker thread beside the hub window
    tray_supported = True
    #: the optional cleaner server's launcher, looked for next to rawi.py
    cleaner_script = "cleaner-server.sh"
    #: font families for the Tk pill and dock (regular, bold, symbols)
    font_family = "TkDefaultFont"
    font_family_bold = "TkDefaultFont"
    font_family_symbol = "TkDefaultFont"
    #: Tk point sizes were tuned on Windows (96 dpi); other systems scale them by this factor
    font_scale = 1.0

    # ------------------------------------------------------------------ sound
    def beep(self, freq: int, ms: int) -> None:
        """A short tone. Here: the terminal bell (the pitch cannot be chosen)."""
        try:
            sys.stdout.write("\a")
            sys.stdout.flush()
        except Exception:
            pass

    # ------------------------------------------------------------------ focus
    def active_app(self) -> str:
        """Title or app name of whatever will receive the paste. "" when it cannot be read."""
        return self._first_line(["xdotool", "getactivewindow", "getwindowname"])

    def focus_window(self, title: str) -> bool:
        """Bring a visible window with exactly this title forward. False when not possible."""
        return False

    # ------------------------------------------------------------------ keystrokes
    def paste_chord(self) -> tuple[str, str]:
        """The keys that paste in the focused app, for pyautogui.hotkey."""
        return (self.command_key, "v")

    def undo_chord(self) -> tuple[str, str]:
        """The keys that undo in the focused app, for pyautogui.hotkey."""
        return (self.command_key, "z")

    def send_chord(self, keys: tuple[str, ...]) -> None:
        """Type a key chord into the focused app. Raises if pyautogui is missing (callers decide)."""
        import pyautogui
        pyautogui.hotkey(*keys)

    def input_permission(self) -> bool | None:
        """Whether this process may watch the keyboard and type into other apps.

        True / False where the OS has such a switch (macOS Accessibility), None where it does not.
        """
        return None

    def paste_path_note(self) -> str:
        """One line for the doctor: how a dictation reaches the focused app here."""
        return "linux: clipboard + ctrl+v (needs xdotool and an X11 session)"

    # ------------------------------------------------------------------ processes
    def background_python(self) -> str:
        """The interpreter to start background helpers with (no console window where that matters)."""
        return sys.executable

    def spawn(self, args: list[str], *, log_path: str | None = None, detached: bool = False,
              new_group: bool = False, cwd: str | None = None) -> subprocess.Popen:
        """Start a helper process and return it. Raises OSError when it cannot start.

        log_path   append the child's stdout + stderr to this file (else they go to /dev/null)
        detached   the child should outlive this process and its terminal
        new_group  the child should not receive this terminal's Ctrl+C
        """
        kwargs = self._spawn_kwargs(detached=detached, new_group=new_group)
        return self._popen(args, log_path=log_path, cwd=cwd, **kwargs)

    def run_hidden(self, args: list[str], **kwargs) -> subprocess.CompletedProcess:
        """subprocess.run without flashing a console window. Raises like subprocess.run."""
        return subprocess.run(args, **kwargs)

    def pid_alive(self, pid: int) -> bool:
        """True when `pid` names a running process (a zombie that has exited does not count)."""
        try:
            pid = int(pid)
        except (TypeError, ValueError):
            return False
        if pid <= 0:                        # 0 and negatives address process groups, not a process
            return False
        try:
            os.kill(pid, 0)                 # signal 0: existence check, nothing is delivered
        except PermissionError:             # it exists, it just is not ours
            return True
        except Exception:
            return False
        return not self._exited_zombie(pid)

    @staticmethod
    def _exited_zombie(pid: int) -> bool:
        """True when `pid` has exited but lingers as a zombie its parent has not reaped yet.

        The hub starts the daemon and never waits on it, so a stopped daemon stays a zombie
        that `kill -0` still reports as alive. Our own children are reaped here; anyone
        else's are recognised by their `ps` state ("Z").
        """
        try:
            done, _status = os.waitpid(pid, os.WNOHANG)
            if done == pid:
                return True                 # our child, exited, now reaped
        except ChildProcessError:
            pass                            # not our child: ask ps below
        except Exception:
            return False
        try:
            r = subprocess.run(["ps", "-o", "stat=", "-p", str(pid)],
                               capture_output=True, text=True, timeout=2)
            return r.stdout.strip().startswith("Z")
        except Exception:
            return False

    def kill(self, pid: int) -> bool:
        """Ask a process to stop (SIGTERM here). True when the request was delivered."""
        try:
            os.kill(int(pid), signal.SIGTERM)
            return True
        except Exception:
            return False

    def cleaner_command(self, root: str) -> list[str] | None:
        """The command that starts the optional cleaner server, or None when no launcher exists."""
        script = os.path.join(root, self.cleaner_script)
        return ["/bin/sh", script] if os.path.exists(script) else None

    # ------------------------------------------------------------------ files
    def open_path(self, path: str) -> bool:
        """Open a file or folder in the system's file browser / default app."""
        try:
            subprocess.Popen(["xdg-open", path], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            return True
        except Exception:
            return False

    # ------------------------------------------------------------------ GPU
    def register_gpu_libraries(self) -> None:
        """Make pip-installed CUDA libraries loadable before ctranslate2 imports. No-op here."""

    # ------------------------------------------------------------------ Tk windows
    def font(self, size: int, *, bold: bool = False, symbol: bool = False) -> tuple:
        """A Tk font tuple that looks the same size on every OS."""
        family = self.font_family_symbol if symbol else (self.font_family_bold if bold else self.font_family)
        return (family, max(1, round(size * self.font_scale)))

    def prepare_overlay(self, root, key_color: str) -> str:
        """Make `key_color` see-through on a borderless Tk window. Returns the colour to paint
        transparent areas with (canvases must use it as their background too)."""
        try:
            root.attributes("-transparentcolor", key_color)
        except Exception:
            pass                            # X11 has no colour key: the corners stay dark
        return key_color

    def make_passive(self, root, *, click_through: bool) -> None:
        """Stop a floating Tk window from taking focus (and, optionally, from taking clicks)."""

    # ------------------------------------------------------------------ helpers
    @staticmethod
    def _first_line(args: list[str], timeout: float = 3.0) -> str:
        """Run a short command and return its trimmed output (max 120 chars), or ""."""
        try:
            r = subprocess.run(args, capture_output=True, text=True, timeout=timeout)
            return (r.stdout or "").strip()[:120]
        except Exception:
            return ""

    def _spawn_kwargs(self, *, detached: bool, new_group: bool) -> dict:
        # POSIX: a new session detaches from the terminal (no SIGHUP, no Ctrl+C) in one step.
        return {"start_new_session": True} if (detached or new_group) else {}

    @staticmethod
    def _popen(args: list[str], *, log_path: str | None, cwd: str | None, **kwargs) -> subprocess.Popen:
        log = open(log_path, "a", encoding="utf-8", errors="replace") if log_path else None
        try:
            out = log if log is not None else subprocess.DEVNULL
            return subprocess.Popen(args, cwd=cwd, stdin=subprocess.DEVNULL, stdout=out, stderr=out,
                                    **kwargs)
        finally:
            if log is not None:
                log.close()                 # the child holds its own copy of the handle


class WindowsPlatform(Platform):
    """Windows 10/11: the maintainer's daily path. Behaviour here matches the original code."""

    name = "windows"
    tray_supported = True
    cleaner_script = "cleaner-server.bat"
    font_family = "Segoe UI"
    font_family_bold = "Segoe UI Semibold"
    font_family_symbol = "Segoe UI Symbol"

    def beep(self, freq: int, ms: int) -> None:
        try:
            import winsound
            winsound.Beep(freq, ms)
        except Exception:
            pass

    def active_app(self) -> str:
        try:
            import pygetwindow as gw
            w = gw.getActiveWindow()
            return (w.title or "").strip()[:120] if w else ""
        except Exception:
            return ""

    def focus_window(self, title: str) -> bool:
        """Only a visible window whose title is exactly `title` (case-sensitive). FindWindowW is
        not enough: it ignores case, so an Explorer window on the "rawi" folder would match."""
        try:
            import ctypes
            from ctypes import wintypes
            user32 = ctypes.windll.user32
            found: list = []

            @ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
            def visit(hwnd, _lparam):
                if user32.IsWindowVisible(hwnd):
                    buf = ctypes.create_unicode_buffer(256)
                    user32.GetWindowTextW(hwnd, buf, 256)
                    if buf.value.strip() == title:
                        found.append(hwnd)
                        return False                # stop enumerating
                return True

            user32.EnumWindows(visit, 0)
            if not found:
                return False
            hwnd = found[0]
            if user32.IsIconic(hwnd):
                user32.ShowWindow(hwnd, 9)          # SW_RESTORE
            user32.SetForegroundWindow(hwnd)
            return True
        except Exception:
            return False

    def paste_path_note(self) -> str:
        return "windows: clipboard + ctrl+v"

    def background_python(self) -> str:
        # pythonw.exe has no console window; fall back to python.exe when it is not installed
        pyw = os.path.join(os.path.dirname(sys.executable), "pythonw.exe")
        return pyw if os.path.exists(pyw) else sys.executable

    def run_hidden(self, args: list[str], **kwargs) -> subprocess.CompletedProcess:
        kwargs.setdefault("creationflags", CREATE_NO_WINDOW)
        return subprocess.run(args, **kwargs)

    def pid_alive(self, pid: int) -> bool:
        """OpenProcess + GetExitCodeProcess, not os.kill(pid, 0): on Windows that would kill it."""
        try:
            import ctypes
            k = ctypes.windll.kernel32
            h = k.OpenProcess(0x1000, False, int(pid))     # PROCESS_QUERY_LIMITED_INFORMATION
            if not h:
                return False
            code = ctypes.c_ulong(0)
            k.GetExitCodeProcess(h, ctypes.byref(code))
            k.CloseHandle(h)
            return code.value == 259                        # STILL_ACTIVE
        except Exception:
            return False

    def kill(self, pid: int) -> bool:
        try:
            r = self.run_hidden(["taskkill", "/PID", str(int(pid)), "/F"], capture_output=True)
            return r.returncode == 0
        except Exception:
            return False

    def cleaner_command(self, root: str) -> list[str] | None:
        script = os.path.join(root, self.cleaner_script)
        return ["cmd.exe", "/c", script] if os.path.exists(script) else None

    def open_path(self, path: str) -> bool:
        try:
            os.startfile(path)                              # Windows-only API
            return True
        except Exception:
            return False

    def register_gpu_libraries(self) -> None:
        """cuBLAS/cuDNN 12 ship as pip packages (nvidia-*-cu12); put their bin dirs on the
        DLL search path before ctranslate2 loads, or it dies with 'cublas64_12.dll not found'."""
        import glob
        import site
        dirs: list[str] = []
        try:
            roots = list(site.getsitepackages()) + [site.getusersitepackages()]
        except Exception:
            roots = []
        for sp in roots:
            for pat in ("nvidia/*/bin", "nvidia/*/lib"):
                dirs += glob.glob(os.path.join(sp, pat))
        for dp in dirs:
            try:
                os.add_dll_directory(dp)
            except Exception:
                pass
            os.environ["PATH"] = dp + os.pathsep + os.environ.get("PATH", "")

    def prepare_overlay(self, root, key_color: str) -> str:
        try:
            root.attributes("-transparentcolor", key_color)
        except Exception:
            pass
        return key_color

    def make_passive(self, root, *, click_through: bool) -> None:
        """WS_EX_NOACTIVATE + WS_EX_TOOLWINDOW (+ WS_EX_TRANSPARENT for click-through)."""
        try:
            import ctypes
            user32 = ctypes.windll.user32
            hwnd = user32.GetParent(root.winfo_id()) or root.winfo_id()
            GWL_EXSTYLE = -20
            WS_EX_TRANSPARENT = 0x00000020
            WS_EX_TOOLWINDOW = 0x00000080
            WS_EX_NOACTIVATE = 0x08000000
            style = user32.GetWindowLongW(hwnd, GWL_EXSTYLE) | WS_EX_TOOLWINDOW | WS_EX_NOACTIVATE
            if click_through:
                style |= WS_EX_TRANSPARENT
            user32.SetWindowLongW(hwnd, GWL_EXSTYLE, style)
        except Exception:
            pass

    def _spawn_kwargs(self, *, detached: bool, new_group: bool) -> dict:
        # DETACHED_PROCESS for long-lived GUI-less helpers (pythonw); otherwise a hidden console,
        # so console children (cmd.exe running a .bat) do not pop up a window.
        flags = DETACHED_PROCESS if detached else CREATE_NO_WINDOW
        if new_group:
            flags |= CREATE_NEW_PROCESS_GROUP
        return {"creationflags": flags}


class MacPlatform(Platform):
    """macOS 13+. Needs Accessibility (and Input Monitoring) for the app that runs rawi."""

    name = "macos"
    command_key = "command"
    # AppKit aborts the process ("NSWindow should only be instantiated on the main thread!")
    # when Tk creates a window anywhere else. The pill and the dock therefore own main.
    tk_needs_main_thread = True
    # pystray's macOS backend wants the main thread too, which pywebview already owns.
    tray_supported = False
    font_family = "Helvetica Neue"
    font_family_bold = "Helvetica Neue Medium"
    font_family_symbol = "Apple Symbols"
    font_scale = 4 / 3                      # Tk on macOS draws 1 pt = 1 px (72 dpi), Windows 96 dpi

    def beep(self, freq: int, ms: int) -> None:
        try:
            subprocess.Popen(["afplay", "/System/Library/Sounds/Tink.aiff"],
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        except Exception:
            pass

    def active_app(self) -> str:
        return self._first_line(["osascript", "-e",
                                 'tell application "System Events" to get name of first '
                                 'application process whose frontmost is true'])

    def input_permission(self) -> bool | None:
        try:
            from HIServices import AXIsProcessTrusted      # pyobjc, installed with pyautogui
            return bool(AXIsProcessTrusted())
        except Exception:
            return None

    def paste_path_note(self) -> str:
        return "macos: clipboard + cmd+v"

    def open_path(self, path: str) -> bool:
        try:
            subprocess.Popen(["open", path], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            return True
        except Exception:
            return False

    def prepare_overlay(self, root, key_color: str) -> str:
        # Aqua Tk has no colour key; it has true per-window transparency instead.
        try:
            root.attributes("-transparent", True)
            return "systemTransparent"
        except Exception:
            return key_color

    def make_passive(self, root, *, click_through: bool) -> None:
        """Run as an accessory app (no Dock icon, never steals focus from the app being typed
        into) and, for the pill, let clicks fall through to whatever is underneath."""
        try:
            from AppKit import NSApplication, NSApplicationActivationPolicyAccessory
            app = NSApplication.sharedApplication()
            app.setActivationPolicy_(NSApplicationActivationPolicyAccessory)
            if click_through:
                for w in app.windows():
                    w.setIgnoresMouseEvents_(True)
        except Exception:
            pass


def for_platform(platform_name: str) -> Platform:
    """The adapter for a `sys.platform` value ("win32", "darwin", "linux", ...)."""
    if platform_name.startswith("win"):
        return WindowsPlatform()
    if platform_name == "darwin":
        return MacPlatform()
    return Platform()


@lru_cache(maxsize=1)
def current() -> Platform:
    """The adapter for the machine this code is running on."""
    return for_platform(sys.platform)
