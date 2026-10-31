#!/bin/bash
# macOS double-click launcher for the hub: the counterpart of rawi-app.bat.
# The hub starts the daemon with its own interpreter, so launching it from .venv covers both.
cd "$(dirname "$0")" || exit 1
PY=python3
[ -x .venv/bin/python ] && PY=.venv/bin/python
"$PY" rawi_app.py
