#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$ROOT_DIR"

# Safe local setup only. This script never prompts for, reads, or writes the
# Freshdesk API key. Live credentials remain an operator-managed file outside
# the repository. Use FRESHDESK_OFFLINE=1 for development.

echo "=== Freshdesk Scanner Setup (Mac) ==="

if ! command -v python3.11 >/dev/null 2>&1; then
  if command -v brew >/dev/null 2>&1; then
    echo "python3.11 not found; installing python@3.11 with Homebrew"
    brew install python@3.11
  else
    echo "FAIL: python3.11 not found and Homebrew is unavailable" >&2
    exit 1
  fi
else
  echo "OK: python3.11 already available"
fi

if [ ! -d .venv ]; then
  python3.11 -m venv .venv
else
  echo "OK: .venv already exists"
fi

.venv/bin/python -m pip install --upgrade pip
.venv/bin/python -m pip install -r requirements.txt

if [ -f "$HOME/.config/furtouch/freshdesk_api_key" ]; then
  perms="$(stat -f "%Lp" "$HOME/.config/furtouch/freshdesk_api_key" 2>/dev/null || stat -c "%a" "$HOME/.config/furtouch/freshdesk_api_key")"
  echo "API-key file exists; permissions: $perms (contents not read)"
else
  echo "API-key file not present; offline mode does not require it"
fi

echo ""
echo "=== Setup complete ==="
echo "Safe development run:"
echo "  FRESHDESK_OFFLINE=1 .venv/bin/flask --app app run --host 127.0.0.1 --port 5050"
echo "Then open: http://127.0.0.1:5050/queue"
