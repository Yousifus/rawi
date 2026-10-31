#!/bin/bash
# macOS double-click launcher for the daemon: the counterpart of start.bat.
# It runs inside Terminal, so Terminal is the app that needs Accessibility and Input Monitoring
# (System Settings -> Privacy & Security). Use the .venv from the install steps when it exists.
cd "$(dirname "$0")" || exit 1
PY=python3
[ -x .venv/bin/python ] && PY=.venv/bin/python
"$PY" rawi.py
echo
read -n 1 -s -r -p "rawi stopped. Press any key to close this window."
