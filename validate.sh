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

FRESHDESK_OFFLINE=1 "$PYTHON" - <<'PY'
import re
import app
# Last-Opened focus marker (Prompt03): server-side wiring, distinct dark-blue
# styling, confirmed-only DOM move, and the jump control / hidden-by-filters
# message. All strings below also exist as JS comments, so assert on the
# rendered CSS/HTML/JS constructs, not bare marker words.
r = app.app.test_client().get("/queue")
assert r.status_code == 200
html = r.get_data(as_text=True)
# Distinct dark-blue focus styling, off the yellow/orange review family.
assert "tr.rv-last-opened{outline:3px solid #0d47a1" in html
assert "tr.rv-last-opened td:first-child{box-shadow:inset 4px 0 0 #0d47a1}" in html
assert ".b-last-opened{background:#0d47a1;color:#fff}" in html
# Rows carry a semantic data-ticket-id focus anchor.
assert re.search(r'data-ticket-id="5000\d\d"', html)
# JS: marker moves only on a confirmed save, stripping the old row first.
assert "function moveLastOpened(newId)" in html
assert "moveLastOpened(d.last_opened_id);" in html
assert "target.classList.add('rv-last-opened')" in html
# Jump control targets the table and never navigates/uses the network.
assert "id=last-opened-jump" in html and "aria-controls=queue-table" in html
assert re.search(r"scrollIntoView\(\{behavior: 'smooth', block: 'center'\}\)", html)
print("last-opened focus markup, distinct CSS, jump control, and confirmed-only JS wiring present")
PY
ok "last-opened focus markup/CSS/JS wiring is present"

FRESHDESK_OFFLINE=1 "$PYTHON" - <<'PY'
import tempfile, os
with tempfile.TemporaryDirectory() as tmp:
    # Set REVIEW_DB_PATH BEFORE importing app so the Flask app (and its
    # test client) resolves the isolated DB for the render checks below.
    os.environ["REVIEW_DB_PATH"] = os.path.join(tmp, "review.sqlite3")
    import app
    app.init_db(os.environ["REVIEW_DB_PATH"])
    app.mark_opened(500001)
    app.mark_opened(500002)   # newer last_opened_at -> 500002 wins
    assert app.last_opened_ticket_id() == 500002
    # Reviewing another ticket does not move the focus.
    app.set_review_result(500003, "Resolved")
    assert app.last_opened_ticket_id() == 500002
    # Move the marker onto 500001 (a ticket rendered by the default/overdue
    # view) so the jump control is actually emitted for the render checks.
    app.mark_opened(500001)
    assert app.last_opened_ticket_id() == 500001
    # Render checks against this isolated DB: 500001 is Opened (active view only).
    c = app.app.test_client()
    active = c.get("/queue").get_data(as_text=True)
    assert '<tr class="rv-opened rv-last-opened" data-ticket-id="500001">' in active
    assert "id=last-opened-jump" in active
    assert "id=last-opened-hidden" not in active
    completed = c.get("/queue?review_view=completed").get_data(as_text=True)
    assert "id=last-opened-jump" not in completed
    assert "id=last-opened-hidden" in completed
    # Invalid timestamps fail safe (skipped, never crash).
    conn = app._db_conn()
    conn.execute("UPDATE review_state SET last_opened_at = 'garbage' WHERE ticket_id = 500002")
    conn.commit(); conn.close()
    assert app.last_opened_ticket_id() == 500001
    print("last_opened selection: newest valid wins, review-separated, fail-safe")
PY
ok "last-opened selection is newest-valid, review-independent, and fail-safe"

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
