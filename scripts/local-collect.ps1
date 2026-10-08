# TheoryLabs local collector launcher (Windows PowerShell).
#
# Finds this repository's virtual environment and runs the canonical command,
# `tftlab local-collect`, from the repository folder (where .env lives). All
# collection logic is in the Python CLI; this file only locates it.
# Run:  .\scripts\local-collect.ps1
# If Windows blocks scripts, use:  powershell -ExecutionPolicy Bypass -File .\scripts\local-collect.ps1
# (or double-click scripts\local-collect.cmd). Extra arguments are passed through.
$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent $PSScriptRoot
Set-Location $Root
$Tftlab = Join-Path $Root ".venv\Scripts\tftlab.exe"
if (-not (Test-Path $Tftlab)) {
    Write-Host "TheoryLabs is not installed in $Root\.venv yet." -ForegroundColor Red
    Write-Host "One-time setup (see README, 'Collect data on your own computer'):"
    Write-Host "  py -3 -m venv .venv"
    Write-Host "  .\.venv\Scripts\pip install -e ."
    exit 1
}
& $Tftlab local-collect @args
exit $LASTEXITCODE
