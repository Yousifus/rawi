"""Start the optional local voice server (voice_out.py) detached, logging beside it."""
import os
import subprocess
import sys

root = os.path.dirname(os.path.abspath(__file__))
log = open(os.path.join(root, "voiceout.log"), "ab", buffering=0)
p = subprocess.Popen([sys.executable, os.path.join(root, "voice_out.py")], cwd=root,
                     stdout=log, stderr=log, creationflags=0x00000008, close_fds=True)
print("launched", p.pid)
