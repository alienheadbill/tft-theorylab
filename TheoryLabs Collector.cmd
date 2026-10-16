@echo off
rem TheoryLabs Collector -- double-click to open the local control panel.
rem Starts the collector app in the background (no console window stays open),
rem using the .venv in this folder, and opens it in your browser at http://127.0.0.1:8765/.
rem If it is already running, it just opens the browser again.
rem Stop it with the "Stop the collector app" button, or "Stop TheoryLabs Collector.cmd".
setlocal
cd /d "%~dp0"
if not exist ".venv\Scripts\pythonw.exe" (
  echo TheoryLabs is not installed in %CD%\.venv yet.
  echo One-time setup: see docs\local-collector.md, "One-time setup".
  pause
  exit /b 1
)
start "" ".venv\Scripts\pythonw.exe" -m tftlab.collector_ui
exit /b 0
