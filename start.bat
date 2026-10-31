@echo off
cd /d "%~dp0"
rem use the .venv from the install steps when it exists, otherwise the python on PATH
set "PY=python"
if exist ".venv\Scripts\python.exe" set "PY=.venv\Scripts\python.exe"
"%PY%" rawi.py
pause
