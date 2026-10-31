"""One zombie check for the daemon, the hub and the menu bar (macOS and Linux only).

Call it after os.kill(pid, 0) has succeeded. A zombie (exited, not yet reaped) still answers kill, so ps is
asked for the process state. A missing or hung ps counts the pid as alive: kill already said it exists,
and a second daemon must not start beside a live one.
"""
import subprocess

PS_TIMEOUT = 2


def ps_pid_alive(pid: int) -> bool:
    try:
        st = subprocess.run(["ps", "-o", "stat=", "-p", str(int(pid))],
                            capture_output=True, text=True, timeout=PS_TIMEOUT).stdout.strip()
    except (OSError, subprocess.SubprocessError):
        return True
    return bool(st) and not st.startswith("Z")
