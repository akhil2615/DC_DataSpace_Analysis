param(
    [switch]$SkipSfAuthCheck
)

$ErrorActionPreference = "Stop"

$repoRoot = Split-Path -Parent (Split-Path -Parent $MyInvocation.MyCommand.Path)
Set-Location $repoRoot

Write-Host "==> Running setup..." -ForegroundColor Cyan
$setupArgs = @()
if ($SkipSfAuthCheck) { $setupArgs += "-SkipSfAuthCheck" }
powershell -ExecutionPolicy Bypass -File ".\scripts\setup_windows.ps1" @setupArgs
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }

Write-Host "==> Starting launcher on http://127.0.0.1:8000" -ForegroundColor Cyan
& ".\.venv\Scripts\python.exe" -m uvicorn app:app --host 127.0.0.1 --port 8000 --reload
