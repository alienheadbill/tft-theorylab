@echo off
rem Stops the TheoryLabs Collector control panel (refuses while a collection is running).
setlocal
cd /d "%~dp0"
if not exist ".venv\Scripts\python.exe" (
  echo TheoryLabs is not installed in %CD%\.venv.
  pause
  exit /b 1
)
".venv\Scripts\python.exe" -m tftlab.collector_ui --stop
timeout /t 4 >nul
exit /b 0
