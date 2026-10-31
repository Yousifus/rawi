@echo off
cd /d "%~dp0"
rem the hub starts the daemon with its own interpreter, so launching it from .venv covers both
set "PYW=pythonw"
if exist ".venv\Scripts\pythonw.exe" set "PYW=.venv\Scripts\pythonw.exe"
start "" "%PYW%" rawi_app.py
