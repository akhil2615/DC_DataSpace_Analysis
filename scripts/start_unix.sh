#!/usr/bin/env bash
set -euo pipefail

repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$repo_root"

echo "==> Running setup..."
./scripts/setup_unix.sh

echo "==> Starting launcher on http://127.0.0.1:8000"
.venv/bin/python -m uvicorn app:app --host 127.0.0.1 --port 8000 --reload
