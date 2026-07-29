#!/usr/bin/env bash
set -euo pipefail

echo "=== Freshdesk Scanner Validation ==="
echo ""

# Python
if ! command -v python3.11 &>/dev/null; then
  echo "FAIL: python3.11 not found. Install via brew install python@3.11"
  exit 1
fi
echo "OK: python3.11 $(python3.11 --version)"

# venv
if [ ! -d .venv ]; then
  echo "FAIL: .venv not found. Run: python3.11 -m venv .venv && source .venv/bin/activate && pip install -r requirements.txt"
  exit 1
fi
echo "OK: .venv exists"

# Flask installed
if ! .venv/bin/python -c "import flask" 2>/dev/null; then
  echo "FAIL: Flask not installed in venv. Run: pip install -r requirements.txt"
  exit 1
fi
echo "OK: Flask installed"

# Presidio + spacy
if ! .venv/bin/python -c "from presidio_analyzer import AnalyzerEngine" 2>/dev/null; then
  echo "FAIL: presidio-analyzer not installed. Run: pip install -r requirements.txt"
  exit 1
fi
echo "OK: presidio-analyzer installed"

if ! .venv/bin/python -m spacy validate 2>/dev/null | grep -q "en_core_web_lg"; then
  echo "FAIL: en_core_web_lg not found. Run: python -m spacy download en_core_web_lg"
  exit 1
fi
echo "OK: en_core_web_lg downloaded"

# API key
if [ ! -f ~/.config/furtouch/freshdesk_api_key ]; then
  echo "FAIL: Freshdesk API key not found at ~/.config/furtouch/freshdesk_api_key"
  echo "     Create it with: mkdir -p ~/.config/furtouch && nano ~/.config/furtouch/freshdesk_api_key && chmod 600 ~/.config/furtouch/freshdesk_api_key"
  exit 1
fi
echo "OK: API key file exists at ~/.config/furtouch/freshdesk_api_key"

# Permissions
perms=$(stat -f "%Lp" ~/.config/furtouch/freshdesk_api_key 2>/dev/null || stat -c "%a" ~/.config/furtouch/freshdesk_api_key 2>/dev/null || echo "unknown")
if [ "$perms" != "600" ]; then
  echo "WARN: API key permissions are $perms, expected 600. Fix with: chmod 600 ~/.config/furtouch/freshdesk_api_key"
else
  echo "OK: API key permissions are 600"
fi

echo ""
echo "=== All checks passed. To run the scanner: ==="
echo "  source .venv/bin/activate"
echo "  flask run --host 127.0.0.1 --port 5050"
echo ""
echo "Then open: http://127.0.0.1:5050/queue"
