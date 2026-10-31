"""Loopback IPC: newline-delimited JSON on 127.0.0.1.

The daemon serves; the hub, the dock and your own scripts call it:

    from rawi_core import ipc
    ipc.call("status")                       # -> {"ok": True, "recording": False, ...}
    ipc.call({"cmd": "listen", "on": True})

    printf '{"cmd":"toggle"}\\n' | nc 127.0.0.1 18090

One request line in, one reply line out, then the connection closes. Neither side ever raises:
the client returns {"ok": False, "error": ...} and the server replies with the same shape.
Only loopback is ever bound, so nothing on the network can reach the daemon.
"""
from __future__ import annotations

import json
import socket
import threading
import time
from typing import Callable

HOST = "127.0.0.1"
DAEMON_PORT = 18090
MAX_REQUEST = 65536          # bytes; a longer request is cut off and treated as malformed


def call(request: dict | str, timeout: float = 4.0, *, host: str = HOST,
         port: int = DAEMON_PORT) -> dict:
    """Send one request to the daemon and return its reply. Never raises.

    `request` is a command name ("status") or a full request dict ({"cmd": "listen", "on": True}).
    """
    obj = {"cmd": request} if isinstance(request, str) else dict(request)
    try:
        with socket.create_connection((host, port), timeout=timeout) as s:
            s.settimeout(timeout)
            s.sendall((json.dumps(obj, ensure_ascii=False) + "\n").encode("utf-8"))
            buf = _read_line(s)
        return json.loads(buf.decode("utf-8", "replace").strip() or "{}")
    except Exception as e:
        return {"ok": False, "error": f"{type(e).__name__}: {e}"}


def port_busy(port: int, host: str = HOST) -> bool:
    """True when something already accepts connections on host:port."""
    s = socket.socket()
    s.settimeout(0.4)
    try:
        return s.connect_ex((host, port)) == 0
    finally:
        s.close()


def serve(handler: Callable[[dict], dict], *, host: str = HOST, port: int = DAEMON_PORT,
          ready: threading.Event | None = None) -> dict:
    """Serve `handler` on a daemon thread. Returns immediately.

    handler(request_dict) -> reply_dict. A malformed request arrives as {} (a status query).
    An exception in the handler becomes {"ok": False, "error": ...}.

    The returned dict gains "port" (the bound port; pass port=0 for an ephemeral one in tests)
    or "error" once binding finishes; `ready` is set at that moment either way.
    """
    info: dict = {}

    def handle(conn: socket.socket) -> None:
        try:
            conn.settimeout(3)
            try:
                req = json.loads(_read_line(conn).decode("utf-8") or "{}")
                if not isinstance(req, dict):
                    req = {}
            except Exception:
                req = {}
            try:
                out = handler(req)
            except Exception as e:
                out = {"ok": False, "error": f"{type(e).__name__}: {e}"}
            try:
                conn.sendall((json.dumps(out, ensure_ascii=False) + "\n").encode("utf-8"))
            except Exception:
                pass
        finally:
            try:
                conn.close()
            except Exception:
                pass

    def loop() -> None:
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            s.bind((host, port))
            s.listen(8)
            info["port"] = s.getsockname()[1]
            print(f"[rawi] ipc on {host}:{info['port']}", flush=True)
        except Exception as e:
            s.close()
            info["error"] = f"{type(e).__name__}: {e}"
            print(f"[rawi] ipc unavailable ({info['error']})", flush=True)
            return
        finally:
            if ready is not None:
                ready.set()
        while True:
            try:
                conn, _ = s.accept()
                threading.Thread(target=handle, args=(conn,), daemon=True).start()
            except Exception:
                time.sleep(0.2)

    threading.Thread(target=loop, daemon=True).start()
    return info


def _read_line(sock: socket.socket) -> bytes:
    """Read until a newline, the peer closes, or MAX_REQUEST bytes."""
    buf = b""
    while not buf.endswith(b"\n"):
        chunk = sock.recv(4096)
        if not chunk:
            break
        buf += chunk
        if len(buf) > MAX_REQUEST:
            break
    return buf
