@echo off
rem TheoryLabs local collector launcher (Windows, double-click).
rem Runs the canonical command `tftlab local-collect` from the repository folder
rem (where .env lives) and keeps the window open so the report can be read.
rem All collection logic is in the Python CLI; this file only locates it.
setlocal
cd /d "%~dp0.."
if not exist ".venv\Scripts\tftlab.exe" (
  echo TheoryLabs is not installed in %CD%\.venv yet.
  echo One-time setup: see README, "Collect data on your own computer".
  pause
  exit /b 1
)
".venv\Scripts\tftlab.exe" local-collect %*
set RESULT=%ERRORLEVEL%
echo.
pause
exit /b %RESULT%
