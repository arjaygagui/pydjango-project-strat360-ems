# Launch the Strat360 municipal EMS (Django).
# Voters: cvl_national (read-only). Machinery + audit: generic_360_db (shared with
# CVL-NATIONAL). Logins/sessions: local strat360.db.
# Usage:  .\run.ps1
$ErrorActionPreference = 'Stop'
$here = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $here
$py = Join-Path $here 'venv\Scripts\python.exe'

Write-Host "==> Applying migrations (local SQLite only)..." -ForegroundColor Cyan
& $py manage.py migrate --noinput

Write-Host "==> Starting server at http://127.0.0.1:8000/" -ForegroundColor Green
& $py manage.py runserver
