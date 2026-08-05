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
expected = {"/queue", "/queue/api/review", "/queue/api/opened"}
rules = {rule.rule for rule in app.app.url_map.iter_rules()}
assert expected <= rules, f"missing routes: {expected - rules}"
print("app.py import and route registration succeeded")
PY
ok "app.py imports; /queue and local review endpoints are registered"

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
response = app.app.test_client().get("/queue")
assert response.status_code == 200
text = response.get_data(as_text=True)
assert "OFFLINE MODE" in text
assert "mock/offline fixture data" in text
assert "matching your filters" in text
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
import tempfile, os
import app
with tempfile.TemporaryDirectory() as tmp:
    db_path = os.path.join(tmp, "sub", "review.sqlite3")
    app.init_db(db_path)
    assert os.path.exists(db_path), "database file was not created"
    # Save and read a review result in the temporary database.
    os.environ["REVIEW_DB_PATH"] = db_path
    app.set_review_result(500001, "Resolved", reviewed_updated_at="2026-07-01T00:00:00Z")
    app.mark_opened(500002)
    rows = app.load_review_rows()
    assert rows[500001]["review_result"] == "Resolved"
    assert rows[500002]["review_result"] == "Opened / In Review"
    assert rows[500001]["reviewed_updated_at"] == "2026-07-01T00:00:00Z"
    print("SQLite review state saved and read in a temporary database")
PY
ok "SQLite database initializes and review state round-trips in a temp location"

FRESHDESK_OFFLINE=1 "$PYTHON" - <<'PY'
import app

html = app.app.test_client().get("/queue").get_data(as_text=True)
# Root-cause regression (Prompt02): the unquoted `class=tid fd-link` was
# parsed by the browser as class="tid" plus an empty attribute `fd-link`, so
# the old `a.fd-link` JS selector matched nothing and clicks never sent
# /queue/api/opened. The class must now be quoted so `fd-link` is a real class.
assert 'class="tid fd-link"' in html, "ticket-number link class must be quoted"
assert 'class="sbj fd-link"' in html, "subject link class must be quoted"
# The click handler anchors on the reliable data-ticket-id identifier and
# never blocks the native new-tab navigation (spec sections 4-5).
assert "querySelectorAll('a[data-ticket-id]')" in html
assert "preventDefault" not in html
# Visible highlight marker + OPENED / IN REVIEW badge + save-failure toast
# (spec sections 6-7).
assert "tr.rv-opened td:first-child{box-shadow:inset 4px 0 0 #f9a825}" in html
assert "OPENED / IN REVIEW" in html
assert "showError" in html
print("click-highlight markup, CSS marker, and JS wiring present")
PY
ok "click-highlight markup/CSS/JS wiring is present"

FRESHDESK_OFFLINE=1 "$PYTHON" - <<'PY'
import tempfile, os
import app
with tempfile.TemporaryDirectory() as tmp:
    db_path = os.path.join(tmp, "review.sqlite3")
    os.environ["REVIEW_DB_PATH"] = db_path
    app.init_db(db_path)
    # Fresh open -> Opened / In Review, with timestamps recorded.
    assert app.mark_opened(500001) == "Opened / In Review"
    # A deliberate state is preserved on re-open, while last-opened updates
    # and no duplicate record is created (spec section 8).
    app.set_review_result(500002, "Resolved")
    assert app.mark_opened(500002) == "Resolved"
    rows = app.load_review_rows()
    assert rows[500002]["review_result"] == "Resolved"
    assert rows[500002]["last_opened_at"] is not None
    assert len(rows) == 2
    print("mark_opened: fresh open -> Opened / In Review; deliberate states preserved")
PY
ok "clicked-ticket review state is saved locally and deliberate states are preserved"

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
if git ls-files | grep -E '(^|/)(data/|.*\.sqlite3?$)' >/dev/null; then
  fail "SQLite database file is tracked"
fi
ok "no API key, cache, .env, or SQLite database is tracked"

if git status --short | grep -E '(^| )\.env|freshdesk_api_key|cache/|(^| )data/' >/dev/null; then
  fail "secret/cache/database artifact appears in working tree"
fi
ok "no secret, cache, or database artifact appears in git status"

echo "=== VALIDATION PASSED ==="
echo "Run safely with: FRESHDESK_OFFLINE=1 flask --app app run --host 127.0.0.1 --port 5050"
if [ -f "$KEY" ]; then
  echo "API-key file: exists; permissions checked without reading contents"
else
  echo "API-key file: missing; offline mode does not require it"
fi
