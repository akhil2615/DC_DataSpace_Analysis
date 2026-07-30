param(
    [switch]$SkipSfAuthCheck
)

$ErrorActionPreference = "Stop"

function Write-Step($msg) {
    Write-Host ""
    Write-Host "==> $msg" -ForegroundColor Cyan
}

function Test-Command($name) {
    return [bool](Get-Command $name -ErrorAction SilentlyContinue)
}

$repoRoot = Split-Path -Parent (Split-Path -Parent $MyInvocation.MyCommand.Path)
Set-Location $repoRoot

Write-Step "Checking required system tools"
$missing = @()
if (-not (Test-Command "python")) { $missing += "python" }
if (-not (Test-Command "git")) { $missing += "git" }
if (-not (Test-Command "sf")) { $missing += "sf" }

if ($missing.Count -gt 0) {
    Write-Host "Missing required tools: $($missing -join ', ')" -ForegroundColor Red
    Write-Host ""
    Write-Host "Install guidance:"
    if ($missing -contains "python") {
        Write-Host "  Python 3.10+: https://www.python.org/downloads/"
        Write-Host "  Optional (winget): winget install Python.Python.3.12"
    }
    if ($missing -contains "git") {
        Write-Host "  Git: https://git-scm.com/downloads"
        Write-Host "  Optional (winget): winget install Git.Git"
    }
    if ($missing -contains "sf") {
        Write-Host "  Salesforce CLI: https://developer.salesforce.com/tools/salesforcecli"
        Write-Host "  Optional (winget): winget install Salesforce.CLI"
    }
    Write-Host ""
    Write-Host "After installing, restart PowerShell and rerun this script." -ForegroundColor Yellow
    exit 1
}

Write-Step "Checking Python version (3.10+ required)"
$pyVersion = & python -c "import sys; print(f'{sys.version_info.major}.{sys.version_info.minor}')"
$parts = $pyVersion.Trim().Split(".")
$major = [int]$parts[0]
$minor = [int]$parts[1]
if ($major -lt 3 -or ($major -eq 3 -and $minor -lt 10)) {
    Write-Host "Detected Python $pyVersion. Python 3.10 or newer is required." -ForegroundColor Red
    exit 1
}

Write-Step "Creating virtual environment (.venv) if needed"
if (-not (Test-Path ".venv")) {
    python -m venv .venv
}

Write-Step "Installing Python dependencies"
& ".\.venv\Scripts\python.exe" -m pip install --upgrade pip
& ".\.venv\Scripts\python.exe" -m pip install -r requirements.txt

Write-Step "Preparing cache directory"
if (-not (Test-Path ".data-space-analysis-cache")) {
    New-Item -ItemType Directory -Path ".data-space-analysis-cache" | Out-Null
}

if (-not $SkipSfAuthCheck) {
    Write-Step "Checking Salesforce CLI authentication"
    sf org display --json | Out-Null
    if ($LASTEXITCODE -ne 0) {
        Write-Host "Salesforce CLI is installed, but no active org login was found." -ForegroundColor Yellow
        Write-Host "Run: sf org login web --alias my-org" -ForegroundColor Yellow
        Write-Host "Then set target org if needed: sf config set target-org my-org" -ForegroundColor Yellow
    } else {
        Write-Host "Salesforce org context is available." -ForegroundColor Green
    }
}

Write-Step "Setup complete"
Write-Host "Start launcher with:"
Write-Host "  .\.venv\Scripts\python.exe -m uvicorn app:app --host 127.0.0.1 --port 8000 --reload"
Write-Host "Then open: http://127.0.0.1:8000"
