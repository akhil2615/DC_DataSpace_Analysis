#!/usr/bin/env bash
set -euo pipefail

repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$repo_root"

step() {
  echo
  echo "==> $1"
}

missing=()
command -v python3 >/dev/null 2>&1 || missing+=("python3")
command -v git >/dev/null 2>&1 || missing+=("git")
command -v sf >/dev/null 2>&1 || missing+=("sf")

step "Checking required system tools"
if [ "${#missing[@]}" -gt 0 ]; then
  echo "Missing required tools: ${missing[*]}"
  echo
  echo "Install guidance:"
  [[ " ${missing[*]} " == *" python3 "* ]] && echo "  Python 3.10+: https://www.python.org/downloads/"
  [[ " ${missing[*]} " == *" git "* ]] && echo "  Git: https://git-scm.com/downloads"
  [[ " ${missing[*]} " == *" sf "* ]] && echo "  Salesforce CLI: https://developer.salesforce.com/tools/salesforcecli"
  echo
  echo "After installing, rerun this script."
  exit 1
fi

step "Checking Python version (3.10+ required)"
py_ver="$(python3 -c 'import sys; print(f"{sys.version_info.major}.{sys.version_info.minor}")')"
py_major="${py_ver%%.*}"
py_minor="${py_ver##*.}"
if [ "$py_major" -lt 3 ] || { [ "$py_major" -eq 3 ] && [ "$py_minor" -lt 10 ]; }; then
  echo "Detected Python $py_ver. Python 3.10 or newer is required."
  exit 1
fi

step "Creating virtual environment (.venv) if needed"
if [ ! -d ".venv" ]; then
  python3 -m venv .venv
fi

step "Installing Python dependencies"
.venv/bin/python -m pip install --upgrade pip
.venv/bin/python -m pip install -r requirements.txt

step "Preparing cache directory"
mkdir -p .data-space-analysis-cache

step "Checking Salesforce CLI authentication"
if ! sf org display --json >/dev/null 2>&1; then
  echo "Salesforce CLI is installed, but no active org login was found."
  echo "Run: sf org login web --alias my-org"
  echo "Then set target org if needed: sf config set target-org my-org"
else
  echo "Salesforce org context is available."
fi

step "Setup complete"
echo "Start launcher with:"
echo "  .venv/bin/python -m uvicorn app:app --host 127.0.0.1 --port 8000 --reload"
echo "Then open: http://127.0.0.1:8000"
