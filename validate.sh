#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$ROOT_DIR"
PYTHON=".venv/bin/python"
KEY="$HOME/.config/furtouch/freshdesk_api_key"

fail() { echo "FAIL: $*" >&2; exit 1; }
ok() { echo "OK: $*"; }

echo "=== Freshdesk Scanner Offline Validation ==="
command -v python3.11 >/dev/null || fail "python3.11 not found"
PY311_VERSION="$(python3.11 -c 'import sys; print(sys.version_info[:2])')"
[ "$PY311_VERSION" = "(3, 11)" ] || fail "python3.11 is not Python 3.11: $PY311_VERSION"
ok "python3.11 available"

[ -x "$PYTHON" ] || fail ".venv/bin/python not found"
ok "virtual environment exists"

"$PYTHON" - <<'PY'
import flask, requests, pytest
print("required imports: Flask, requests, pytest")
PY
ok "required scanner dependencies import"

"$PYTHON" - <<'PY'
import app
assert "/queue" in {rule.rule for rule in app.app.url_map.iter_rules()}
print("app.py import and /queue registration succeeded")
PY
ok "app.py imports and /queue is registered"

if [ -f "$KEY" ]; then
  perms="$(stat -f "%Lp" "$KEY" 2>/dev/null || stat -c "%a" "$KEY")"
  [ "$perms" = "600" ] || fail "API-key file exists but permissions are $perms, expected 600"
  ok "API-key file exists and permissions are 600 (contents not read)"
else
  echo "INFO: API-key file missing (contents not read; offline mode does not require it)"
fi

FRESHDESK_OFFLINE=1 "$PYTHON" - <<'PY'
import app
assert app.is_offline()
assert {rule.rule for rule in app.app.url_map.iter_rules()} == {"/queue"}
response = app.app.test_client().get("/queue")
assert response.status_code == 200
text = response.get_data(as_text=True)
assert "OFFLINE MODE" in text
assert "mock/offline fixture data" in text
print("offline mode renders /queue")
PY
ok "offline mode is available and /queue renders"

FRESHDESK_OFFLINE=1 "$PYTHON" - <<'PY'
import requests
import app

def blocked(*args, **kwargs):
    raise AssertionError("unexpected network request")
requests.get = blocked
requests.post = blocked
requests.put = blocked
requests.patch = blocked
requests.delete = blocked
response = app.app.test_client().get("/queue")
assert response.status_code == 200
assert "OFFLINE MODE" in response.get_data(as_text=True)
print("offline /queue made no HTTP requests")
PY
ok "offline mode does not make network requests"

"$PYTHON" - <<'PY'
import app
try:
    app.resolve_bind_host("0.0.0.0")
except SystemExit:
    print("unsafe external bind refused")
else:
    raise SystemExit("external bind was not refused")
PY
ok "unsafe external bind is refused"

"$PYTHON" -m pytest
ok "automated tests passed"

if git ls-files | grep -E '(^|/)(\.env|freshdesk_api_key|.*\.key)$' >/dev/null; then
  fail "secret-like file is tracked"
fi
if git ls-files | grep -E '(^|/)cache/|(^|/).*cache.*\.json$' >/dev/null; then
  fail "cache file is tracked"
fi
if git ls-files | grep -E '(^|/).*\.env($|\.)' >/dev/null; then
  fail ".env file is tracked"
fi
ok "no API key, cache, or .env file is tracked"

if git status --short | grep -E '(^| )\.env|freshdesk_api_key|cache/' >/dev/null; then
  fail "secret/cache artifact appears in working tree"
fi
ok "no secret or cache artifact appears in git status"

echo "=== VALIDATION PASSED ==="
echo "Run safely with: FRESHDESK_OFFLINE=1 flask --app app run --host 127.0.0.1 --port 5050"
if [ -f "$KEY" ]; then
  echo "API-key file: exists; permissions checked without reading contents"
else
  echo "API-key file: missing"
fi
