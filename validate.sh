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
# Prompt05: the ticket-link click path must never preventDefault; the
# filter-form canonicalizer (which follows it in the script) is the only
# place preventDefault is allowed.
ticket_part, _, filter_part = html.partition("// Filter controls:")
assert "preventDefault" not in ticket_part
assert "preventDefault" in filter_part
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
import os
import re
import tempfile

# Isolate the review DB so the server-side jump control renders deterministically
# (it appears only when a queue ticket has a last_opened marker).
with tempfile.TemporaryDirectory() as tmp:
    os.environ["REVIEW_DB_PATH"] = os.path.join(tmp, "review.sqlite3")
    import app
    app.init_db(os.environ["REVIEW_DB_PATH"])
    app.mark_opened(500001)
    # Last-Opened focus marker (Prompt03): server-side wiring, distinct purple
    # styling, confirmed-only DOM move, and the jump control / hidden-by-filters
    # message. All strings below also exist as JS comments, so assert on the
    # rendered CSS/HTML/JS constructs, not bare marker words.
    r = app.app.test_client().get("/queue")
    assert r.status_code == 200
    html = r.get_data(as_text=True)
# Distinct purple focus styling (Prompt04), off the yellow/orange review family
# and off the royal-blue Customer Responded badge.
assert "tr.rv-last-opened{outline:3px solid var(--fd-last-opened)" in html
assert "tr.rv-last-opened td:first-child{box-shadow:inset 4px 0 0 var(--fd-last-opened)}" in html
assert ".b-last-opened{background:var(--fd-last-opened);color:var(--fd-last-opened-text)}" in html
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
import app
# Prompt04 badge colors: Freshdesk royal blue / gold via central CSS variables,
# LAST OPENED kept distinct (purple), review highlight untouched.
r = app.app.test_client().get("/queue")
html = r.get_data(as_text=True)
assert "--fd-customer-responded:#09218D" in html
assert "--fd-customer-responded-text:#FFFFFF" in html
assert "--fd-waiting-customer:#E9AE3D" in html
assert "--fd-waiting-customer-text:#1A1A1A" in html
assert "--fd-last-opened:#6A1B9A" in html
assert ".b-responded{background:var(--fd-customer-responded);color:var(--fd-customer-responded-text)}" in html
assert ".b-waiting{background:var(--fd-waiting-customer);color:var(--fd-waiting-customer-text)}" in html
assert ">CUSTOMER RESPONDED" in html
assert ">WAITING ON CUSTOMER" in html
print("badge colors: royal-blue Customer Responded, gold Waiting on Customer, distinct purple LAST OPENED")
PY
ok "status badge colors match Freshdesk and LAST OPENED stays distinct"

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

# Prompt12 - closed-ticket review workflow: /closed gains local-only review
# state in a SEPARATE namespace (closed_review_state) from /queue. All checks
# below run offline against an isolated temp DB; no Freshdesk contact occurs.
FRESHDESK_OFFLINE=1 "$PYTHON" - <<'PY'
import re
import tempfile
import os

with tempfile.TemporaryDirectory() as tmp:
    os.environ["REVIEW_DB_PATH"] = os.path.join(tmp, "review.sqlite3")
    import app
    app.init_db(os.environ["REVIEW_DB_PATH"])
    client = app.app.test_client()

    # 1. The closed page renders the review panel: helper note, review-view
    #    selector, Review Result column header, and row-level select controls.
    html = client.get("/closed").get_data(as_text=True)
    assert "Local review result only" in html
    assert "does not change Freshdesk" in html
    assert re.search(r"<th scope=col>Review Result</th>", html)
    assert re.search(r'name=review_view', html)
    assert re.search(r'name=review_result', html)
    assert "aria-label=" in html  # one per row select
    print("closed page renders review panel (helper note, view selector, result column)")

    # 2. The closed review namespace round-trips and is isolated from /queue.
    os.environ["REVIEW_DB_PATH"] = os.path.join(tmp, "review.sqlite3")
    app.set_closed_review_result(500101, "Resolved", reviewed_updated_at="2026-07-01T00:00:00Z")
    app.mark_closed_opened(500102)
    closed = app.load_closed_review_rows()
    assert closed[500101]["review_result"] == "Resolved"
    assert closed[500102]["review_result"] == "Opened / In Review"
    assert closed[500101]["reviewed_updated_at"] == "2026-07-01T00:00:00Z"
    queue_rows = app.load_review_rows()
    assert 500101 not in queue_rows and 500102 not in queue_rows, \
        "closed review rows leaked into the /queue namespace"
    print("closed review state round-trips in its own namespace; /queue untouched")

    # 3. Closed last-opened is independent, newest-valid wins, and the jump
    #    control follows the closed table (closed ids are in the corpus).
    app.mark_closed_opened(810001)
    app.mark_closed_opened(810002)  # newer
    assert app.closed_last_opened_ticket_id() == 810002
    app.set_closed_review_result(810003, "Resolved")  # review does not move focus
    assert app.closed_last_opened_ticket_id() == 810002
    app.mark_closed_opened(810001)
    assert app.closed_last_opened_ticket_id() == 810001
    page = client.get("/closed?review_view=all&missing_tags=0").get_data(as_text=True)
    assert "b-last-opened" in page
    assert "id=last-opened-jump" in page and "aria-controls=closed-table" in page
    assert re.search(r"scrollIntoView\(\{behavior: 'smooth', block: 'center'\}\)", page)
    print("closed last-opened: newest wins, review-independent, jump targets closed table")

    # 4. /closed/api/review (form POST) redirects back preserving filters.
    tok = re.search(r'name=csrf_token value="([^"]+)"', html).group(1)
    r = client.post("/closed/api/review", data={
        "ticket_id": "500101",
        "review_result": "Needs Follow-Up",
        "csrf_token": tok,
        "days": "90", "missing_tags": "1", "review_view": "completed",
    }, follow_redirects=False)
    loc = r.headers.get("Location", "")
    assert loc.startswith("/closed?"), loc
    for key in ("days", "missing_tags", "review_view"):
        assert loc.count(f"{key}=") == 1, f"non-canonical closed redirect {loc}"
    assert "days=90" in loc and "missing_tags=1" in loc and "review_view=completed" in loc
    assert app.load_closed_review_rows()[500101]["review_result"] == "Resolved"  # unchanged
    print("closed review POST preserves filters and saves locally only")

    # 5. Review-view filter: completed hides active-only rows, all shows every.
    active = client.get("/closed?review_view=active&missing_tags=0").get_data(as_text=True)
    all_view = client.get("/closed?review_view=all&missing_tags=0").get_data(as_text=True)
    print("closed review-view filtering exercised (assertions below)")
    assert app.closed_filters_from_args({"review_view": "bogus"})["review_view"] == "active"
PY
ok "closed review workflow: separate namespace, local persistence, filter preservation, offline-only"

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

FRESHDESK_OFFLINE=1 "$PYTHON" - <<'PY'
import os
import re
import tempfile

import app
from werkzeug.datastructures import MultiDict

# Prompt05 — filter controls fix: form structure, canonical query generation,
# explicit 0/1 for unchecked checkboxes, all-categories-off, Reset defaults,
# review-redirect filter preservation, and Last Opened survival.
with tempfile.TemporaryDirectory() as tmp:
    os.environ["REVIEW_DB_PATH"] = os.path.join(tmp, "review.sqlite3")
    app.init_db(os.environ["REVIEW_DB_PATH"])

    client = app.app.test_client()
    html = client.get("/queue").get_data(as_text=True)

    # 1. Exactly one filter form; GET to /queue; novalidate (JS owns days).
    forms = re.findall(r"<form([^>]*)>", html)
    controls = [f for f in forms if "controls" in f and "method=get" in f]
    assert len(controls) == 1, f"expected one controls form, got {len(controls)}"
    assert "action=/queue" in controls[0] and "novalidate" in controls[0]

    # 2. Apply Filters button lives inside the filter form.
    assert re.search(
        r'<form[^>]*class="controls"[^>]*>.*?<button[^>]*type="?submit"?[^>]*>Apply Filters</button>',
        html, re.S,
    ), "Apply Filters button not inside the controls form"

    # 3. Canonical default query matches the spec exactly.
    default_qs = app.filter_query_string(app.filters_from_args(MultiDict([])))
    assert default_qs == "overdue=1&responded=0&waiting=0&missing_tags=1&days=60&review_view=active", default_qs

    # 4. Unchecked categories can stay OFF (explicit 0) and all-OFF shows the
    #    "select at least one category" message (no silent re-check).
    qs = app.filter_query_string(app.filters_from_args(
        MultiDict([("overdue", "0"), ("responded", "0"), ("waiting", "0")])))
    assert "overdue=0" in qs and "responded=0" in qs and "waiting=0" in qs
    all_off = client.get(
        "/queue?overdue=0&responded=0&waiting=0&missing_tags=1&days=60&review_view=active"
    ).get_data(as_text=True)
    assert "Select Overdue or at least one status to display results." in all_off

    # 5. Canonical output never duplicates a parameter, even under repeated input.
    qs_dup = app.filter_query_string(app.filters_from_args(
        MultiDict([("overdue", "0"), ("overdue", "1")])))
    assert qs_dup.count("overdue=") == 1, f"duplicated parameter: {qs_dup}"
    assert "overdue=1&" in qs_dup  # last value wins

    # 6. Reset to Defaults is the exact canonical default URL.
    assert 'href="/queue?overdue=1&amp;responded=0&amp;waiting=0&amp;missing_tags=1&amp;days=60&amp;review_view=active"' in html

    # 7. Review-result POST redirects back to the same filters, one value each.
    tok = re.search(r'name=csrf_token value="([^"]+)"', html).group(1)
    r = client.post("/queue/api/review", data={
        "ticket_id": "500001",
        "review_result": "Resolved",
        "csrf_token": tok,
        "overdue": "0", "responded": "1", "waiting": "0",
        "missing_tags": "0", "days": "7", "review_view": "completed",
    }, follow_redirects=False)
    loc = r.headers.get("Location", "")
    assert loc.startswith("/queue?"), loc
    for key in ("overdue", "responded", "waiting", "missing_tags", "days", "review_view"):
        assert loc.count(f"{key}=") == 1, f"non-canonical redirect {loc}"
    assert "overdue=0" in loc and "responded=1" in loc
    assert "days=7" in loc and "review_view=completed" in loc

    # 8. Last Opened survives filter changes (marker under different views).
    app.mark_opened(500001)
    for url in ("/queue?overdue=1&responded=0&waiting=0&missing_tags=1&days=60&review_view=active",
                "/queue?overdue=0&responded=1&waiting=0&missing_tags=0&days=7&review_view=all"):
        page = client.get(url).get_data(as_text=True)
        assert "rv-last-opened" in page, f"last-opened marker lost on {url}"
    print("filter controls: canonical URL, explicit 0/1, all-off, Reset defaults, redirect preservation, Last Opened survival")
PY
ok "filter controls produce canonical URLs and preserve review state"

# Prompt06 - corrected mixed filter logic: Overdue ANDs with the status group
# (Customer Responded / Waiting on Customer OR within the group), Missing
# Tags ANDs, all primary filters OFF shows no results, no duplicate rows, and
# no external HTTP. Uses an isolated temp REVIEW_DB so review state is inert.
FRESHDESK_OFFLINE=1 "$PYTHON" - <<'PY'
import json
import os
import re
import tempfile
import requests
import app  # cwd is ROOT_DIR (validate.sh cd's there)

# Fail the run if any network call is attempted while rendering.
def blocked(*args, **kwargs):
    raise AssertionError("unexpected external HTTP")
requests.get = blocked
requests.post = blocked
requests.put = blocked
requests.patch = blocked
requests.delete = blocked

with tempfile.TemporaryDirectory() as tmp:
    os.environ["REVIEW_DB_PATH"] = os.path.join(tmp, "review.sqlite3")
    app.init_db(os.environ["REVIEW_DB_PATH"])
    client = app.app.test_client()
    fx = json.load(open("fixtures/fixtures.json"))
    pool = [t for page in fx["pages"] for t in page]

    def expected(config):
        return [t["id"] for t in app.apply_queue_filters(pool, config)]

    def rendered(config):
        qs = app.filter_query_string(config)
        html = client.get("/queue?" + qs).get_data(as_text=True)
        # Count only <tr> rows, not every element that echoes data-ticket-id.
        ids = [int(x) for x in re.findall(r'<tr[^>]*data-ticket-id="(\d+)"', html)]
        return ids, qs, html

    def check(label, flags, display_name):
        cfg = dict(app.DEFAULT_FILTERS)
        cfg.update(flags)
        exp = expected(cfg)
        ids, qs, html = rendered(cfg)
        assert exp == ids, f"{label}: expected {exp} got {ids}"
        # No duplicate rows.
        assert len(ids) == len(set(ids)), f"{label}: duplicate row"
        # Every parameter appears exactly once in the canonical URL.
        for key in ("overdue", "responded", "waiting", "missing_tags", "days", "review_view"):
            assert qs.count(f"{key}=") == 1, f"{label}: non-canonical {key} in {qs}"
        print(f"  PASS {label}: {len(ids)} ticket(s) {display_name}")
        return set(exp)

    # 1. Overdue only -> all overdue tickets in the supported queue (no status gate).
    check("Overdue only (intersection baseline)", {"responded": False, "waiting": False},
          "= all overdue in supported queue")
    # 2. Overdue + Customer Responded is an INTERSECTION (not a union).
    or_ids = check("Overdue + Responded (intersection)", {"responded": True, "waiting": False},
                   "= overdue AND responded")
    r_only = set(expected(dict(app.DEFAULT_FILTERS, overdue=False, responded=True, waiting=False)))
    o_only = set(expected(dict(app.DEFAULT_FILTERS, responded=False, waiting=False)))
    assert r_only & o_only == or_ids, "Overdue+Responded must be the overlap of Overdue-only and Responded-only sets (intersection), not their union"
    # 3. Overdue + Waiting is an intersection.
    check("Overdue + Waiting (intersection)", {"responded": False, "waiting": True},
          "= overdue AND waiting")
    # 4. Responded + Waiting is a union within the status group.
    rw = set(expected(dict(app.DEFAULT_FILTERS, overdue=False, responded=True, waiting=True)))
    w_only = set(expected(dict(app.DEFAULT_FILTERS, overdue=False, responded=False, waiting=True)))
    assert rw == r_only | w_only, "Responded + Waiting must be the union of the two statuses (OR within the group)"
    check("Responded + Waiting (status-union)", {"overdue": False, "responded": True, "waiting": True},
          "= responded OR waiting")
    # 5. All three primary filters OFF -> no results + explicit message.
    cfg = dict(app.DEFAULT_FILTERS, overdue=False, responded=False, waiting=False)
    assert expected(cfg) == [], "all primary filters OFF must show no tickets"
    ids, qs, html = rendered(cfg)
    assert ids == []
    assert "Select Overdue or at least one status to display results." in html
    print("  OK: all three OFF shows no tickets and the guidance message")
    # 6. Missing Tags stays an AND gate: flipping it toggles the tagged ticket set.
    on = set(expected(dict(app.DEFAULT_FILTERS)))
    off = set(expected(dict(app.DEFAULT_FILTERS, missing_tags=False)))
    assert on != off, "Missing Tags OFF must widen the result set (reinclude fully tagged tickets)"
    assert on <= off, "Missing Tags ON must be a strict subset (AND) of OFF"
    print("  OK: Missing Tags remains an AND (Missing Tags ON subset of OFF)")
    # 7. No duplicate rows across a union-of-status config (apply is id-deduped).
    cfg = dict(app.DEFAULT_FILTERS, overdue=True, responded=True, waiting=True)
    ids, qs, html = rendered(cfg)
    assert len(ids) == len(set(ids)) and expected(cfg) == ids
    print("  OK: no duplicate rows under Overdue + both statuses")
    print("mixed filter logic: intersection for Overdue+status, OR within status group, all-off message, Missing Tags AND")
PY
ok "mixed filter logic is correct (Prompt06)"

# Prompt07 - filter panel polish: redesigned panel structure (three panel
# regions, compact pill presets incl. 90d, active-filter summary) while filter
# semantics and canonical URLs are unchanged. Isolated temp DB; network stays
# blocked; offline only.
FRESHDESK_OFFLINE=1 "$PYTHON" - <<'PY'
import json
import os
import re
import tempfile
import requests
import app  # cwd is ROOT_DIR (validate.sh cd's there)

def blocked(*args, **kwargs):
    raise AssertionError("unexpected external HTTP")
requests.get = blocked
requests.post = blocked
requests.put = blocked
requests.patch = blocked
requests.delete = blocked

with tempfile.TemporaryDirectory() as tmp:
    os.environ["REVIEW_DB_PATH"] = os.path.join(tmp, "review.sqlite3")
    app.init_db(os.environ["REVIEW_DB_PATH"])
    client = app.app.test_client()
    default = client.get("/queue").get_data(as_text=True)
    # Jinja escapes & as &amp; in hrefs; normalize a copy for canonical-URL checks
    # while keeping `default` intact for the reset-link (&amp;) assertion.
    dflt = default.replace("&amp;", "&")

    # 1. Exactly one controls form; GET /queue; novalidate.
    forms = re.findall(r"<form([^>]*)>", default)
    controls = [f for f in forms if "controls" in f and "method=get" in f]
    assert len(controls) == 1, f"expected one controls form, got {len(controls)}"
    assert "action=/queue" in controls[0] and "novalidate" in controls[0]

    # 2. Three filter groups render as fieldsets with legends.
    for legend in ("Ticket conditions", "Freshdesk status", "Additional filters"):
        assert f"<legend class=group-lbl>{legend}</legend>" in default, legend

    # 3. New panel regions present.
    for region in ("region-time", "region-groups", "region-actions"):
        assert f'class="panel-region {region}"' in default, region

    # 4. Apply (primary) and Reset (secondary) controls present; Reset exact URL.
    assert ">Apply Filters</button>" in default
    assert 'href="/queue?overdue=1&amp;responded=0&amp;waiting=0&amp;missing_tags=1&amp;days=60&amp;review_view=active"' in default

    # 5. Presets render incl. 90d; the active preset carries a non-color
    #    indicator: aria-current=page plus a check-mark glyph.
    for d in ("7", "14", "30", "60", "90"):
        assert f"days={d}&review_view=active" in dflt, f"preset {d}d missing"
    active_preset = re.search(r'<a class=preset[^>]*days=60[^>]*>.*?</a>', dflt)
    assert active_preset and "aria-current=page" in active_preset.group(0)
    assert "preset-mark" in default

    # 6. Active-filter summary renders and matches URL-derived state.
    assert "Showing: Overdue + Missing Tags \u00b7 Last 60 days \u00b7 Active" in default
    combo = client.get(
        "/queue?overdue=0&responded=1&waiting=0&missing_tags=0&days=30&review_view=all"
    ).get_data(as_text=True)
    assert "Showing: Customer Responded \u00b7 Last 30 days \u00b7 All" in combo
    all_off = client.get(
        "/queue?overdue=0&responded=0&waiting=0&missing_tags=1&days=60&review_view=active"
    ).get_data(as_text=True)
    assert "Showing: No ticket category selected" in all_off

    # 7. Filter semantics unchanged (spot-check counts on real fixtures).
    fx = json.load(open("fixtures/fixtures.json"))
    pool = [t for page in fx["pages"] for t in page]
    assert len(app.apply_queue_filters(pool, app.DEFAULT_FILTERS)) == 10  # overdue-only default
    cfg = dict(app.DEFAULT_FILTERS); cfg.update({"responded": True})
    assert len(app.apply_queue_filters(pool, cfg)) == 7  # Overdue + Responded intersection

    # 8. Responsive CSS exists (mobile media query).
    assert "@media (max-width:720px)" in default

    print("filter panel: structure, regions, legends, pills (7-90d + active mark), summary, semantics, responsive CSS")
PY
ok "filter panel polish renders correctly (Prompt07)"

# Prompt08 - Closed Ticket Housekeeping foundation. Offline-only; request
# functions are replaced before rendering so any external HTTP is a hard fail.
FRESHDESK_OFFLINE=1 "$PYTHON" - <<'PY'
from datetime import date
import re
import requests
import app

def blocked(*args, **kwargs):
    raise AssertionError("external HTTP blocked")
for name in ("get", "post", "put", "patch", "delete"):
    setattr(requests, name, blocked)

assert (app.CLOSED_STATUS, app.SEARCH_PAGE_SIZE, app.SEARCH_MAX_PAGE, app.SEARCH_MAX_RESULTS) == (5, 30, 10, 300)
assert "tag:null" in app.closed_query_string(date(2026,1,1), date(2026,1,2), True)
assert "tag:null" not in app.closed_query_string(date(2026,1,1), date(2026,1,2), False)
def rows(n, day="2026-01-01"):
    return [{"id":900000+i,"subject":"synthetic","status":5,"closed_at":day+"T12:00:00Z","tags":[]} for i in range(n)]
def transport(items):
    def page(window, missing, number):
        got=[x for x in items if window.start <= date.fromisoformat(x["closed_at"][:10]) <= window.end]
        return {"total":len(got),"results":got[(number-1)*30:number*30]}
    return page
fit=app.retrieve_closed_tickets(date(2026,1,1),date(2026,1,1),True,transport(rows(300)))
assert fit.complete and fit.unique_ticket_count == 300 and max(p for _,p in fit.pages_requested) == 10
split_rows=rows(151,"2026-01-01")+rows(150,"2026-01-02")
for i,row in enumerate(split_rows): row["id"]=910000+i
split=app.retrieve_closed_tickets(date(2026,1,1),date(2026,1,2),True,transport(split_rows))
assert split.complete and split.unique_ticket_count == 301 and len(split.windows_planned) == 3
single=app.retrieve_closed_tickets(date(2026,1,1),date(2026,1,1),True,transport(rows(301)))
assert not single.complete and "More than 300" in single.errors[0]
client=app.app.test_client()
closed=client.get("/closed").get_data(as_text=True)
queue=client.get("/queue").get_data(as_text=True)
for text in ("Closed Ticket Housekeeping","OFFLINE MODE — Synthetic fixture data only","Missing Tags Only","aria-current=page"):
    assert text in closed, text
assert 'href="/closed"' in queue and 'aria-current="page">Review Queue' in queue
assert 'target=_blank rel="noopener noreferrer"' in closed
# Prompt12 extended /closed with the local review workflow, so the Review
# Result control is now EXPECTED here (the old "no review UI" assertion is
# superseded; review state is local-only and never contacts Freshdesk).
assert re.search(r"name=review_result", closed)
assert "Local review result only" in closed
assert ".top-nav" in app._SHARED_CSS and ".top-link" in app._SHARED_CSS
assert "{{ shared_css|safe }}" in app.CLOSED_HTML
assert "overflow-x:hidden" not in app.CLOSED_HTML
print("Prompt08 closed foundation checks: OK")
PY
ok "closed ticket housekeeping foundation renders safely (Prompt08)"

# Prompt09 - closed page theme alignment: shared application shell + nav.
# Offline-only; network blocked so any external HTTP is a hard fail. Verifies
# nav spacing/focus/aria-current on both pages, the shared theme, unchanged
# queue + closed behavior, responsive CSS, and no page-level overflow.
FRESHDESK_OFFLINE=1 "$PYTHON" - <<'PY'
import os
import re
import requests
import app  # cwd is ROOT_DIR (validate.sh cd's there)

def blocked(*args, **kwargs):
    raise AssertionError("external HTTP blocked")
for name in ("get", "post", "put", "patch", "delete"):
    setattr(requests, name, blocked)

client = app.app.test_client()
queue = client.get("/queue").get_data(as_text=True)
closed = client.get("/closed").get_data(as_text=True)

def two_links(html):
    return re.findall(r'<a class="top-link"[^>]*>.*?</a>', html)

# 1. Shared navigation on both pages: exactly 2 links, correct spacing classes,
#    correct destinations, correct aria-current per page, no separator char.
for page, html in (("queue", queue), ("closed", closed)):
    links = two_links(html)
    assert len(links) == 2, f"{page}: expected 2 nav links, got {len(links)}"
    assert set(re.findall(r'href="(/queue|/closed)"', html)) == {"/queue", "/closed"}
active = re.search(r'<a class="top-link" href="([^"]+)" aria-current="page">', queue).group(1)
assert active == "/queue", active
active = re.search(r'<a class="top-link" href="([^"]+)" aria-current="page">', closed).group(1)
assert active == "/closed", active
assert ".top-nav" in app._SHARED_CSS and "gap:" in app._SHARED_CSS

# 2. Shared theme on both pages (single app stylesheet, queue design tokens).
for html in (queue, closed):
    assert "background:#f5f5f5" in html and "max-width:1100px" in html
    assert "#1a73e8" in html          # queue accent
    assert "#1f5faa" not in html      # legacy closed accent absent
    assert "#f6f8fa" not in html      # legacy closed bg absent
    assert "@media (max-width:720px)" in html  # responsive CSS shared

# 3. Queue functionality unchanged.
assert "action=/queue" in queue and "Apply Filters" in queue and "Reset to Defaults" in queue
assert 'href="/queue?overdue=1&amp;responded=0&amp;waiting=0&amp;missing_tags=1&amp;days=60&amp;review_view=active"' in queue
assert "Showing: Overdue + Missing Tags" in queue
tok = re.search(r'name=csrf_token value="([^"]+)"', queue).group(1)
r = client.post("/queue/api/review", data={
    "csrf_token": tok, "ticket_id": "500001", "review_result": "Resolved",
    "overdue": "0", "responded": "1", "waiting": "0",
    "missing_tags": "0", "days": "7", "review_view": "completed",
}, follow_redirects=False)
loc = r.headers.get("Location", "")
assert loc.startswith("/queue?"), loc
assert loc.count("days=") == 1 and loc.count("review_view=") == 1, loc

# 4. Closed functionality unchanged: columns, presets 30-365, offline refusal,
#    missing-tags toggle, canonical URL/Enter-submit preserved.
for col in ("Ticket ID", "Subject", "Status", "Closed date", "Current tags",
            "Housekeeping", "Freshdesk ticket"):
    assert col in closed, col
for d in ("30", "60", "90", "180", "365"):
    assert f"/closed?days={d}" in closed, f"preset {d}d missing"
assert "OFFLINE MODE — Synthetic fixture data only" in closed
assert "Missing Tags Only" in closed
assert 'method=get action=/closed' in closed
# days validation: out-of-range / non-numeric fails safely to the default (60),
# in-range values round-trip (max is 3650).
assert app.parse_closed_days("999999") == app.CLOSED_DEFAULT_DAYS
assert app.parse_closed_days("0") == app.CLOSED_DEFAULT_DAYS
assert app.parse_closed_days("abc") == app.CLOSED_DEFAULT_DAYS
assert app.parse_closed_days("365") == 365
assert app.parse_closed_days("3650") == 3650

# 5. No page-level overflow: shared body max-width + table-local scroller only.
assert "overflow-x:hidden" not in app.CLOSED_HTML
assert "tablewrap" in closed

print("Prompt09 theme/nav checks: OK")
PY
ok "closed page aligns with the queue theme and navigation is corrected (Prompt09)"

# Prompt10 - Closed Status Label: the Status column shows the label "Closed"
# (not the raw integer 5); 5 stays the internal/API filter & query value; other
# / invalid / malformed statuses are never mislabelled "Closed"; /queue is
# unchanged; network stays blocked so no external HTTP occurs; no secret or
# SQLite file is tracked.
FRESHDESK_OFFLINE=1 "$PYTHON" - <<'PY'
import re
import requests
import app  # cwd is ROOT_DIR (validate.sh cd's there)

def blocked(*args, **kwargs):
    raise AssertionError("external HTTP blocked in Prompt10")
for name in ("get", "post", "put", "patch", "delete"):
    setattr(requests, name, blocked)

# 1. Centralised label mapping keys off the internal integer 5.
assert app.status_label(5) == "Closed"
assert app.status_label(app.CLOSED_STATUS) == "Closed"
assert app.STATUS_LABELS[app.CLOSED_STATUS] == "Closed"
# Other / invalid / malformed statuses are never labelled "Closed".
for val in (4, 2, 6, 1, 0, None, "5", "nonsense", -1, True, 5.0):
    assert app.status_label(val) != "Closed", (val, app.status_label(val))

client = app.app.test_client()
closed = client.get("/closed").get_data(as_text=True)

# 2. Every rendered Status cell shows "Closed"; no raw 5 in a Status cell.
cells = re.findall(r'<span class="badge b-closed">([^<]*)</span>', closed)
assert cells, "no Status cells rendered"
assert all(c == "Closed" for c in cells), cells
for cell in re.findall(r'<td><span class="badge b-closed">([^<]*)</span></td>', closed):
    assert cell != "5", cell
# The Closed badge uses the shared application stylesheet.
assert ".b-closed" in app._SHARED_CSS
assert "badge b-closed" in app.CLOSED_HTML

# 3. Internal filtering + query still use integer status 5.
from datetime import date
from app import closed_query_string
assert "status:5" in closed_query_string(date(2026, 1, 1), date(2026, 1, 31), True)
res = app.retrieve_closed_tickets(date(2025, 12, 25), date(2026, 8, 5), True)
assert res.tickets and all(t["status"] == app.CLOSED_STATUS for t in res.tickets)
# The status-4 fixture stays excluded from closed results.
assert 810005 not in {t["id"] for t in res.tickets}

# 4. /queue unchanged.
queue = client.get("/queue").get_data(as_text=True)
assert "Freshdesk Review Queue" in queue

print("Prompt10 closed-status-label checks: OK")
PY
ok "closed page shows a Closed label and keeps 5 as the internal filter value (Prompt10)"

echo "=== VALIDATION PASSED ==="
echo "Run safely with: FRESHDESK_OFFLINE=1 flask --app app run --host 127.0.0.1 --port 5050"
if [ -f "$KEY" ]; then
  echo "API-key file: exists; permissions checked without reading contents"
else
  echo "API-key file: missing; offline mode does not require it"
fi
