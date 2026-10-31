"""`python rawi.py --doctor`: tell a stranger why rawi is not working.

One line per check, grouped and numbered so a bug report can say "[3] failed". Exits 1 on a
hard failure, 0 otherwise (warnings are fine). `--deep` also loads the model, which is slow but
proves the whole path. Nothing here changes the machine; it does not even create config.json.
"""
from __future__ import annotations

import json
import os
import sys
import time

from . import config, ipc, paths, voice
from .gpu import cuda_device_count
from .system import current

SR = 16000


def _short(path: str) -> str:
    """Home-relative for display: a doctor line should not print somebody's account name."""
    home = os.path.expanduser("~")
    try:
        if home and path.startswith(home):
            return "~" + path[len(home):]
    except Exception:
        pass
    return path


def _cache_dir(model: str) -> str:
    """Where faster-whisper's downloader would put this model: any `*faster-whisper-<model>` repo."""
    hub = os.path.join(os.path.expanduser("~"), ".cache", "huggingface", "hub")
    suffix = f"faster-whisper-{model.replace('/', '--')}"
    try:
        for name in os.listdir(hub):
            if name.endswith(suffix) and os.path.isdir(os.path.join(hub, name, "snapshots")):
                return os.path.join(hub, name)
    except Exception:
        pass
    return os.path.join(hub, f"models--Systran--{suffix}")


def run(deep: bool = False, cfg_path: str = paths.CONFIG) -> int:
    """Print the checks; return the process exit code."""
    fails = warns = 0
    plat = current()
    cfg = config.load(cfg_path, create=False)

    def ok(msg):
        print(f"  ok    {msg}")

    def warn(msg):
        nonlocal warns
        warns += 1
        print(f"  warn  {msg}")

    def bad(msg):
        nonlocal fails
        fails += 1
        print(f"  FAIL  {msg}")

    print(f"rawi doctor · {sys.platform} ({plat.name}) · python {sys.version.split()[0]}")

    print("\n[1] interpreter")
    if sys.version_info < (3, 10):
        bad(f"python {sys.version.split()[0]} is too old; 3.10-3.12 required")
    elif sys.version_info >= (3, 13):
        warn(f"python {sys.version.split()[0]}: 3.13+ is untested (3.10-3.12 recommended)")
    else:
        ok(f"python {sys.version.split()[0]}")

    print("\n[2] packages")
    for m in ("numpy", "faster_whisper", "sounddevice", "pynput", "pyperclip", "requests"):
        try:
            __import__(m)
            ok(m)
        except Exception as e:
            bad(f"{m} missing ({type(e).__name__}) — pip install -r requirements.txt")
    # the paste path: without these, a dictation is transcribed and then silently lost
    if str(cfg.get("paste", True)).lower() != "false":
        # pygetwindow names the focused window on Windows only (macOS asks osascript)
        for m in ("pyautogui", "pygetwindow") if plat.name == "windows" else ("pyautogui",):
            try:
                __import__(m)
                ok(m)
            except Exception:
                bad(f"{m} missing — paste cannot work; pip install -r requirements.txt")
    for m, label, why in (("soundfile", "soundfile", "--test / wav input"),
                          ("webview", "webview (hub window)", "the hub window")):
        try:
            __import__(m)
            ok(label)
        except Exception:
            warn(f"{m} missing (optional: {why})")

    print("\n[3] microphone")
    try:
        import sounddevice as sd
        dev = sd.query_devices(kind="input")
        ok(f"default input: {dev['name'][:48]} ({int(dev['default_samplerate'])} Hz)")
        try:
            st = sd.InputStream(samplerate=SR, channels=1, dtype="float32")
            st.start()
            st.stop()
            st.close()
            ok("opened the microphone for 0.1 s")
        except Exception as e:
            bad(f"could not open the mic ({type(e).__name__}: {e})")
    except Exception as e:
        bad(f"no input device visible ({type(e).__name__})")

    print("\n[4] GPU")
    n = cuda_device_count()
    if n:
        ok(f"{n} CUDA device(s) visible to ctranslate2")
    else:
        warn("no CUDA device visible — will run on CPU (int8; a `small` model is a good start)")

    print("\n[5] model")
    model, ctype = str(cfg.get("model")), str(cfg.get("compute_type"))
    if os.path.isdir(model) or os.path.isfile(model):
        ok(f"local model path: {model}")
    elif os.path.isdir(_cache_dir(model)):
        ok(f"{model} is downloaded ({_short(_cache_dir(model))})")
    else:
        warn(f"{model} is not downloaded yet; the first run will fetch it (needs network once)")
    if deep:
        try:
            from faster_whisper import WhisperModel
            dev2 = "cuda" if n else "cpu"
            ct2 = ctype if n else "int8"
            t0 = time.time()
            WhisperModel(model, device=dev2, compute_type=ct2)
            ok(f"loaded {model} on {dev2}/{ct2} in {time.time()-t0:.1f} s (--deep)")
        except Exception as e:
            bad(f"model failed to load ({type(e).__name__}: {e})")

    print("\n[6] config")
    if not os.path.exists(cfg_path):
        warn("no config.json yet (one will be created with defaults on first run)")
    else:
        try:
            with open(cfg_path, encoding="utf-8") as f:
                json.load(f)
            ok("config.json parses (next to rawi.py)")
        except Exception as e:
            bad(f"config.json is not valid JSON ({type(e).__name__}) — delete it and start over")
    hk = str(cfg.get("hold_key"))
    try:
        from pynput import keyboard
        if not (hasattr(keyboard.Key, hk) or len(hk) == 1):
            warn(f"hold_key {hk!r} is not a known key name; use e.g. ctrl_l, alt_r, f6")
    except Exception:
        pass

    print("\n[7] paste path")
    ok(plat.paste_path_note())
    trusted = plat.input_permission()
    if trusted is True:
        ok("this app may watch the keyboard and type (Accessibility granted)")
    elif trusted is False:
        warn("Accessibility is NOT granted to the app running rawi: hotkeys and paste will "
             "silently do nothing. System Settings → Privacy & Security → Accessibility "
             "(and Input Monitoring): add your terminal, then restart it")

    print("\n[8] ports")
    for port, label in ((ipc.DAEMON_PORT, "daemon IPC"), (voice.PORT, "voice server")):
        if ipc.port_busy(port):
            warn(f"{port} ({label}) already in use — another rawi may be running")
        else:
            ok(f"{port} ({label}) free")

    print("\nsummary:", "ready" if not fails else f"{fails} failure(s), {warns} warning(s)")
    return 0 if not fails else 1
