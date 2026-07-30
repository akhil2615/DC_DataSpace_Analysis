#!/usr/bin/env bash
set -euo pipefail

repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$repo_root"

step() {
  echo
  echo "==> $1"
}

missing=()
command -v git >/dev/null 2>&1 || missing+=("git")
command -v sf >/dev/null 2>&1 || missing+=("sf")

step "Checking required system tools"
if [ "${#missing[@]}" -gt 0 ]; then
  echo "Missing required tools: ${missing[*]}"
  echo
  echo "Install guidance:"
  [[ " ${missing[*]} " == *" git "* ]] && echo "  Git: https://git-scm.com/downloads"
  [[ " ${missing[*]} " == *" sf "* ]] && echo "  Salesforce CLI: https://developer.salesforce.com/tools/salesforcecli"
  echo
  echo "After installing, rerun this script."
  exit 1
fi

step "Selecting Python interpreter (3.10+ required)"
PYTHON_BIN=""
for candidate in python3.12 python3.11 python3.10 python3; do
  if command -v "$candidate" >/dev/null 2>&1; then
    py_ver="$("$candidate" -c 'import sys; print(f"{sys.version_info.major}.{sys.version_info.minor}")')"
    py_major="${py_ver%%.*}"
    py_minor="${py_ver##*.}"
    if [ "$py_major" -gt 3 ] || { [ "$py_major" -eq 3 ] && [ "$py_minor" -ge 10 ]; }; then
      PYTHON_BIN="$candidate"
      break
    fi
  fi
done

if [ -z "$PYTHON_BIN" ]; then
  echo "No supported Python found. Install Python 3.10+ and ensure it is available as python3.10, python3.11, python3.12, or python3."
  echo "Install: https://www.python.org/downloads/"
  exit 1
fi
echo "Using $PYTHON_BIN"

step "Creating virtual environment (.venv) if needed"
if [ ! -d ".venv" ]; then
  "$PYTHON_BIN" -m venv .venv
fi

step "Installing Python dependencies"
.venv/bin/python -m pip install --upgrade pip
.venv/bin/python -m pip install -r requirements.txt

step "Preparing cache directory"
mkdir -p .data-space-analysis-cache

step "Checking Salesforce CLI authentication"
if ! sf org display --json >/dev/null 2>&1; then
  echo "Salesforce CLI is installed, but no active org login was found."
  echo "Run these commands now:"
  echo "  sf org login web --alias my-org"
  echo "  sf config set target-org my-org --global"
else
  echo "Salesforce org context is available."
fi

step "Setup complete"
echo "Start launcher with:"
echo "  .venv/bin/python -m uvicorn app:app --host 127.0.0.1 --port 8000 --reload"
echo "Then open: http://127.0.0.1:8000"
