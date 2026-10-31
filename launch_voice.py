"""Start the optional local voice server (voice_out.py) detached, logging beside it."""
import os
import subprocess
import sys

root = os.path.dirname(os.path.abspath(__file__))
log = open(os.path.join(root, "voiceout.log"), "ab", buffering=0)
detach = ({"creationflags": 0x00000008} if sys.platform.startswith("win")   # DETACHED_PROCESS
          else {"start_new_session": True})
p = subprocess.Popen([sys.executable, os.path.join(root, "voice_out.py")], cwd=root,
                     stdout=log, stderr=log, close_fds=True, **detach)
print("launched", p.pid)
