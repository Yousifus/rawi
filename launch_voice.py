"""Start the optional local voice server (voice_out.py) detached, logging to voiceout.log."""
from rawi_core import paths
from rawi_core.system import current

plat = current()
p = plat.spawn([plat.background_python(), paths.VOICE_SCRIPT], log_path=paths.VOICE_LOG,
               cwd=paths.ROOT, detached=True)
print("launched", p.pid)
