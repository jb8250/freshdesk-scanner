"""Freshdesk Review Queue Dashboard — read-only scanner + local review workflow.

Routes:
  GET  /queue               dashboard with filter controls and review views
  POST /queue/api/review    save a local review result (form POST, CSRF-protected)
  POST /queue/api/opened    record that a ticket link was opened (JSON, CSRF-protected)

Data sources:
  live    GET /api/v2/tickets on the Freshdesk account (read-only, list endpoint only)
  offline FRESHDESK_OFFLINE=1  -> local fixture pages, no network, no API key

Local review state (never sent to Freshdesk): SQLite at data/review_state.sqlite3
(override with REVIEW_DB_PATH). Contains per-ticket review result, first/last
opened timestamps, last review change, and the ticket updated_at snapshot taken
when a reviewed state was assigned (drives the Updated Since Review flag).

Offline mode is fail-closed: it never calls the network and never reads the API
key file. If the fixture data is missing or malformed, /queue renders an error
page instead of falling back to live access.

The API key is never loaded at import time. Only load_api_key() touches the key
file, and only the live data path calls it.
"""
import hmac
import json
import os
import re
import secrets
import sqlite3
from datetime import datetime, timedelta, timezone
from urllib.parse import urlencode

import requests
from flask import (Flask, jsonify, redirect, render_template_string, request,
                   session)

app = Flask(__name__, static_folder=None)
# Per-process random key: used only for the loopback CSRF token and flash
# messages. Changes every restart, which is correct for a local tool.
app.secret_key = secrets.token_hex(32)

BASE_DIR = os.path.dirname(os.path.abspath(__file__))


def is_offline() -> bool:
    """Explicit offline mode flag. Read from the environment on every call so
    tests (and operators) can flip it without a restart. There is no
    auto-detection: if FRESHDESK_OFFLINE is not set, the app is in live mode.
    """
    return os.environ.get("FRESHDESK_OFFLINE", "").strip().lower() in ("1", "true", "yes")


# Freshdesk queue scanner config
FRESHDESK_DOMAIN = "broadriverretail-help.freshdesk.com"
FRESHDESK_KEY_FILE = os.path.expanduser("~/.config/furtouch/freshdesk_api_key")

# Never populated at import time. Live mode only.
FRESHDESK_API_KEY = ""

# Offline fixtures: JSON file containing {"pages": [[ticket, ...], ...]}.
FIXTURES_FILE = os.environ.get(
    "FRESHDESK_FIXTURES",
    os.path.join(BASE_DIR, "fixtures", "fixtures.json"),
)

CACHE_DIR = os.path.join(BASE_DIR, "cache")
os.makedirs(CACHE_DIR, exist_ok=True)
CACHE_FILE = os.path.join(CACHE_DIR, "tickets.json")
CACHE_TTL_SECONDS = 30 * 60

UPDATED_SINCE_DAYS = 60  # ~2 months

# Scanner keyword set — matches Chrome extension logic (word-boundary regex).
KEYWORDS = [
    "photo", "photos", "picture", "pictures",
    "pic", "pics", "video", "videos", "vid",
]
KEYWORD_RE = re.compile(r"\b(" + "|".join(KEYWORDS) + r")\b", re.IGNORECASE)


class OfflineDataError(Exception):
    """Raised when offline mode cannot load valid fixture data. The app must
    fail closed on this — never fall back to the live API."""


def load_api_key() -> str:
    """Load the Freshdesk API key (live mode only). Reads FRESHDESK_API_KEY env
    var first, then falls back to the chmod-600 key file. The key file is never
    touched while offline mode is active. Returns "" when no key is available.
    """
    global FRESHDESK_API_KEY
    if FRESHDESK_API_KEY:
        return FRESHDESK_API_KEY
    key = os.environ.get("FRESHDESK_API_KEY", "").strip()
    if not key and not is_offline() and os.path.exists(FRESHDESK_KEY_FILE):
        with open(FRESHDESK_KEY_FILE, "r") as fh:
            key = fh.read().strip()
    FRESHDESK_API_KEY = key
    return key


def fd_auth():
    return (load_api_key(), "X")


def keyword_filter_hits(text):
    return bool(KEYWORD_RE.search(text or ""))


# Known status values on this account (from live ticket data):
#   2 = Customer responded  (needs review)
#   5 = Closed               (exclude)
#   6 = Waiting on customer  (optional include)
SCAN_STATUSES = [2, 6]  # Customer responded + Waiting on customer

STATUS_LABELS = {2: "Customer responded", 3: "Pending", 4: "Resolved", 5: "Closed", 6: "Waiting on customer", 1: "Open"}
PRIORITY_LABELS = {1: "Low", 2: "Medium", 3: "High", 4: "Urgent"}

# ---------------------------------------------------------------------------
# Dashboard filter configuration (URL-backed, documented defaults)
# ---------------------------------------------------------------------------

# Category selectors are OR'd together. Missing Tags is an AND gate.
# Days-back window is an AND gate (updated_at based).
DEFAULT_FILTERS = {
    "overdue": True,      # due_by is a valid timestamp earlier than now
    "responded": False,   # status == 2 (Customer responded)
    "waiting": False,     # status == 6 (Waiting on customer)
    "missing_tags": True, # AND: tags absent or empty
    "days": 60,           # tickets updated within the last N days (1-365)
    "review_view": "active",
}
DAYS_MIN, DAYS_MAX, DAYS_DEFAULT = 1, 365, 60
REVIEW_VIEWS = ("active", "completed", "all")

# Local review results (stored in SQLite only — never sent to Freshdesk).
REVIEW_STATES = [
    "Unreviewed",
    "Opened / In Review",
    "Resolved",
    "Not Applicable to Me",
    "No Action Needed",
    "Needs Follow-Up",
]
# States that snapshot the ticket's updated_at at review time. A later ticket
# update compared against that snapshot produces the "UPDATED SINCE REVIEW"
# flag, and such tickets are treated as Active again.
REVIEWED_STATES = {"Resolved", "Not Applicable to Me", "No Action Needed", "Needs Follow-Up"}
ACTIVE_STATES = {"Unreviewed", "Opened / In Review", "Needs Follow-Up"}
COMPLETED_STATES = {"Resolved", "Not Applicable to Me", "No Action Needed"}

# ---------------------------------------------------------------------------
# Time helpers (monkeypatchable in tests)
# ---------------------------------------------------------------------------


def now_utc():
    return datetime.now(timezone.utc)


def iso_now():
    return now_utc().isoformat()


def parse_dt(value):
    """Parse an ISO-8601 timestamp with optional Z suffix. None on failure."""
    if not value or not isinstance(value, str):
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except Exception:
        return None


# ---------------------------------------------------------------------------
# URL parameter parsing (safe fallbacks)
# ---------------------------------------------------------------------------


def _last_value(args, key):
    """Last occurrence wins for repeated query values."""
    vals = args.getlist(key)
    return vals[-1] if vals else None


def parse_bool(value, default=False):
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    low = str(value).strip().lower()
    if low in ("1", "true", "yes", "on"):
        return True
    if low in ("0", "false", "no", "off"):
        return False
    return default  # invalid -> documented default


def parse_days(value):
    if value is None:
        return DAYS_DEFAULT
    s = str(value).strip()
    if not s or not s.isdigit():
        return DAYS_DEFAULT
    n = int(s)
    if not (DAYS_MIN <= n <= DAYS_MAX):
        return DAYS_DEFAULT
    return n


def parse_review_view(value):
    if value in REVIEW_VIEWS:
        return value
    return DEFAULT_FILTERS["review_view"]  # invalid -> active


def filters_from_args(args):
    """Build a canonical filter config from raw query args. Every value is
    validated; invalid or missing values fall back to the documented default.
    Repeated query values: the last occurrence wins.
    """
    return {
        "overdue": parse_bool(_last_value(args, "overdue"), DEFAULT_FILTERS["overdue"]),
        "responded": parse_bool(_last_value(args, "responded"), DEFAULT_FILTERS["responded"]),
        "waiting": parse_bool(_last_value(args, "waiting"), DEFAULT_FILTERS["waiting"]),
        "missing_tags": parse_bool(_last_value(args, "missing_tags"), DEFAULT_FILTERS["missing_tags"]),
        "days": parse_days(_last_value(args, "days")),
        "review_view": parse_review_view(_last_value(args, "review_view")),
    }


def filter_query_string(config):
    """Canonical query string for a filter config (all five filter params +
    review_view). Used by the form action, preset links, and redirects."""
    return urlencode({
        "overdue": "1" if config["overdue"] else "0",
        "responded": "1" if config["responded"] else "0",
        "waiting": "1" if config["waiting"] else "0",
        "missing_tags": "1" if config["missing_tags"] else "0",
        "days": str(config["days"]),
        "review_view": config["review_view"],
    })


# ---------------------------------------------------------------------------
# Category logic (section 6 of the spec)
# ---------------------------------------------------------------------------


def is_overdue(t):
    """Overdue: due_by exists, is a valid timestamp, and is earlier than now.
    Missing or malformed due_by is NOT overdue (fail-closed)."""
    dt = parse_dt(t.get("due_by"))
    return dt is not None and dt < now_utc()


def is_customer_responded(t):
    return t.get("status") == 2


def is_waiting_on_customer(t):
    return t.get("status") == 6


def has_missing_tags(t):
    tags = t.get("tags")
    return not tags or not isinstance(tags, list) or len(tags) == 0


def category_matches(t, category):
    if category == "overdue":
        return is_overdue(t)
    if category == "responded":
        return is_customer_responded(t)
    if category == "waiting":
        return is_waiting_on_customer(t)
    return False


def matches_any_category(t, config):
    """OR across enabled category selectors. All three OFF -> no ticket can
    match (the page shows the 'select at least one category' message)."""
    return any(config[c] and category_matches(t, c) for c in ("overdue", "responded", "waiting"))


def matches_days_window(t, config):
    """AND gate: ticket must have a valid updated_at within the last N days.
    Missing or malformed updated_at fails closed (excluded) so the window is
    never silently widened. Ticket count across fixtures is unaffected by the
    real clock because tests pin now_utc(); live data always carries
    updated_at from the list endpoint."""
    dt = parse_dt(t.get("updated_at"))
    if dt is None:
        return False
    cutoff = now_utc() - timedelta(days=int(config["days"]))
    return dt >= cutoff


def passes_filters(t, config=None):
    """Business-rule filter used by the dashboard: status gate (the review
    queue only contains Customer Responded / Waiting on Customer tickets),
    keyword gate, category OR, Missing Tags AND gate. Days-back window is
    intentionally NOT part of this predicate (it is applied separately as an
    AND gate at render time).

    `config` is a filter dict from filters_from_args(); when omitted the
    documented defaults apply (Overdue ON, Customer Responded OFF, Waiting
    OFF, Missing Tags ON) — the original scanner's default behavior.
    """
    if t.get("status") not in SCAN_STATUSES:
        return False  # Closed/Resolved/Open tickets are never part of the review queue
    if not keyword_filter_hits(t.get("subject")):
        return False
    cfg = config or dict(DEFAULT_FILTERS)
    if not matches_any_category(t, cfg):
        return False
    if cfg["missing_tags"] and not has_missing_tags(t):
        return False
    return True


# ---------------------------------------------------------------------------
# Freshdesk fetch (live only)
# ---------------------------------------------------------------------------


def paginate_tickets():
    """Fetch all tickets across pages from the list endpoint. Live mode only."""
    page = 1
    per_page = 100
    since = (now_utc() - timedelta(days=UPDATED_SINCE_DAYS)).isoformat()
    while True:
        url = f"https://{FRESHDESK_DOMAIN}/api/v2/tickets"
        params = {"page": page, "per_page": per_page, "updated_since": since}
        r = requests.get(url, auth=fd_auth(), params=params, timeout=30)
        if r.status_code == 429:
            retry = r.headers.get("Retry-After")
            wait = int(retry) if retry and retry.isdigit() else 5
            raise requests.exceptions.HTTPError(
                f"429 rate-limited by Freshdesk. Retry after {wait}s."
            )
        r.raise_for_status()
        data = r.json()
        if not data:
            break
        yield from data
        if len(data) < per_page:
            break
        page += 1


def offline_paginate_tickets():
    """Yield tickets from local fixture pages. Offline mode only.

    Fail closed: a missing file, malformed JSON, or a fixture with the wrong
    shape raises OfflineDataError. This function never touches the network and
    never reads the API key file.
    """
    try:
        with open(FIXTURES_FILE, "r") as fh:
            data = json.load(fh)
    except FileNotFoundError:
        raise OfflineDataError(
            f"Offline mode: fixture file not found at {FIXTURES_FILE}. "
            "Run the scanner with live mode, or restore fixtures/fixtures.json."
        )
    except json.JSONDecodeError as e:
        raise OfflineDataError(
            f"Offline mode: fixture file {FIXTURES_FILE} is malformed JSON ({e})."
        )
    if not isinstance(data, dict) or not isinstance(data.get("pages"), list) or not data["pages"]:
        raise OfflineDataError(
            'Offline mode: fixture file must contain a non-empty "pages" list of ticket lists.'
        )
    for page in data["pages"]:
        if not isinstance(page, list):
            raise OfflineDataError(
                "Offline mode: each fixture page must be a list of tickets."
            )
        for t in page:
            if not isinstance(t, dict):
                raise OfflineDataError(
                    "Offline mode: each fixture ticket must be a JSON object."
                )
            yield t


def get_ticket_pool():
    """Return (raw_tickets, cache_age_seconds) using the 30-min cache.

    Live mode fetches from Freshdesk; offline mode fetches from fixtures. The
    cache stores the raw ticket list; dashboard filtering (categories, missing
    tags, days window) happens at render time so every URL-backed filter
    combination is evaluated against the full pool. Cache file corruption or
    read errors fall through to a fresh fetch.
    """
    now_ts = now_utc().timestamp()
    cached = None
    if os.path.exists(CACHE_FILE):
        try:
            with open(CACHE_FILE, "r") as fh:
                blob = json.load(fh)
            if now_ts - blob.get("fetched_at", 0) < CACHE_TTL_SECONDS:
                cached = blob
        except Exception:
            pass

    if cached:
        raw = cached["tickets"]
        cache_age = int(now_ts - cached.get("fetched_at", now_ts))
        return raw, cache_age

    if is_offline():
        raw = list(offline_paginate_tickets())
    else:
        raw = list(paginate_tickets())
    with open(CACHE_FILE, "w") as fh:
        json.dump({"fetched_at": now_ts, "tickets": raw}, fh)
    return raw, 0


def apply_queue_filters(tickets, config):
    """Full dashboard filter pipeline: keyword gate, category OR, Missing Tags
    AND gate, days-back AND gate. Dedupes by ticket id so a ticket can never
    render twice, regardless of source quirks."""
    seen = set()
    out = []
    for t in tickets:
        tid = t.get("id")
        if tid in seen:
            continue
        seen.add(tid)
        if passes_filters(t, config) and matches_days_window(t, config):
            out.append(t)
    return out


# ---------------------------------------------------------------------------
# Local review state (SQLite, never sent to Freshdesk)
# ---------------------------------------------------------------------------

SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS review_state (
    ticket_id           INTEGER PRIMARY KEY,
    review_result       TEXT    NOT NULL DEFAULT 'Unreviewed',
    first_opened_at     TEXT,
    last_opened_at      TEXT,
    last_review_change_at TEXT,
    reviewed_updated_at TEXT,
    note                TEXT    NOT NULL DEFAULT '',
    created_at          TEXT    NOT NULL,
    modified_at         TEXT    NOT NULL
);
"""


def get_db_path():
    return os.environ.get("REVIEW_DB_PATH") or os.path.join(BASE_DIR, "data", "review_state.sqlite3")


def init_db(path=None):
    """Create the review-state database (and parent dir) if missing. Used by
    tests and validate.sh with a temporary path; callers never need a live
    database for offline development."""
    db_path = path or get_db_path()
    parent = os.path.dirname(db_path)
    if parent:
        os.makedirs(parent, exist_ok=True)
    conn = sqlite3.connect(db_path)
    try:
        conn.execute(SCHEMA_SQL)
        conn.commit()
    finally:
        conn.close()
    return db_path


def _db_conn():
    db_path = get_db_path()
    parent = os.path.dirname(db_path)
    if parent:
        os.makedirs(parent, exist_ok=True)
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.execute(SCHEMA_SQL)
    conn.commit()
    return conn


def load_review_rows():
    """Return {ticket_id: row-dict} for every stored review state."""
    conn = _db_conn()
    try:
        rows = conn.execute("SELECT * FROM review_state").fetchall()
        return {r["ticket_id"]: dict(r) for r in rows}
    finally:
        conn.close()


def _state_row(ticket_id):
    conn = _db_conn()
    try:
        return conn.execute("SELECT * FROM review_state WHERE ticket_id = ?", (ticket_id,)).fetchone()
    finally:
        conn.close()


def mark_opened(ticket_id):
    """Record that a ticket link was opened. Pure local state — no Freshdesk
    interaction.

    Review-result rule (dashboard spec §8): an Unreviewed ticket becomes
    "Opened / In Review"; an already-opened ticket stays opened; deliberate
    states (Resolved, Not Applicable to Me, No Action Needed, Needs Follow-Up)
    are PRESERVED — re-opening a link must never silently erase a deliberate
    review result. first_opened_at is set once, last_opened_at always updates,
    and no duplicate record is ever created.

    Returns the effective review_result for the ticket after marking.
    """
    now = iso_now()
    conn = _db_conn()
    try:
        row = conn.execute("SELECT * FROM review_state WHERE ticket_id = ?", (ticket_id,)).fetchone()
        if row is None:
            conn.execute(
                "INSERT INTO review_state (ticket_id, review_result, first_opened_at, last_opened_at,"
                " last_review_change_at, reviewed_updated_at, note, created_at, modified_at)"
                " VALUES (?,?,?,?,?,?,?,?,?)",
                (ticket_id, "Opened / In Review", now, now, now, None, "", now, now),
            )
            result = "Opened / In Review"
        else:
            current = row["review_result"]
            first = row["first_opened_at"] or now
            if not current or current == "Unreviewed":
                result = "Opened / In Review"
            else:
                result = current  # preserve deliberate/completed states
            changed = result != current
            conn.execute(
                "UPDATE review_state SET review_result = ?, first_opened_at = ?,"
                " last_opened_at = ?, last_review_change_at = ?, modified_at = ? WHERE ticket_id = ?",
                (result, first, now, now if changed else row["last_review_change_at"], now, ticket_id),
            )
        conn.commit()
        return result
    finally:
        conn.close()


def set_review_result(ticket_id, result, reviewed_updated_at=None):
    """Save a local review result. `reviewed_updated_at` snapshots the ticket's
    updated_at at review time for the Reviewed states; for Unreviewed / Opened
    the snapshot is cleared so no stale flag can linger."""
    if result not in REVIEW_STATES:
        raise ValueError(f"unknown review result: {result!r}")
    now = iso_now()
    conn = _db_conn()
    try:
        row = conn.execute("SELECT * FROM review_state WHERE ticket_id = ?", (ticket_id,)).fetchone()
        if row is None:
            conn.execute(
                "INSERT INTO review_state (ticket_id, review_result, first_opened_at, last_opened_at,"
                " last_review_change_at, reviewed_updated_at, note, created_at, modified_at)"
                " VALUES (?,?,?,?,?,?,?,?,?)",
                (ticket_id, result, None, None, now,
                 reviewed_updated_at if result in REVIEWED_STATES else None, "", now, now),
            )
        else:
            conn.execute(
                "UPDATE review_state SET review_result = ?, last_review_change_at = ?,"
                " reviewed_updated_at = ?, modified_at = ? WHERE ticket_id = ?",
                (result, now,
                 reviewed_updated_at if result in REVIEWED_STATES else None,
                 now, ticket_id),
            )
        conn.commit()
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Review view logic (Active / Completed / All)
# ---------------------------------------------------------------------------


def updated_since_review(t, state_row):
    """True when the ticket's current updated_at is strictly newer than the
    updated_at snapshot taken when a Reviewed state was assigned. Fail-safe:
    malformed or missing timestamps never produce the flag."""
    if state_row is None:
        return False
    snapshot = state_row.get("reviewed_updated_at")
    if not snapshot:
        return False
    ticket_dt = parse_dt(t.get("updated_at"))
    snapshot_dt = parse_dt(snapshot)
    if ticket_dt is None or snapshot_dt is None:
        return False
    return ticket_dt > snapshot_dt


def review_view_includes(state_row, updated_flag, view):
    """Which tickets belong to the requested review view.

    active:    Unreviewed, Opened / In Review, Needs Follow-Up, or any ticket
               flagged UPDATED SINCE REVIEW (even a previously Completed one).
    completed: Resolved, Not Applicable to Me, No Action Needed — but only if
               NOT flagged, because an updated ticket returns to Active.
    all:       everything.
    """
    if view == "all":
        return True
    result = state_row.get("review_result", "Unreviewed") if state_row else "Unreviewed"
    if updated_flag:
        return view == "active"
    if view == "active":
        return result in ACTIVE_STATES
    return result in COMPLETED_STATES


# ---------------------------------------------------------------------------
# Badges
# ---------------------------------------------------------------------------

REVIEW_CLASS = {
    "Unreviewed": "rv-unreviewed",
    "Opened / In Review": "rv-opened",
    "Resolved": "rv-resolved",
    "Not Applicable to Me": "rv-na",
    "No Action Needed": "rv-none",
    "Needs Follow-Up": "rv-followup",
}


def sla_unavailable(t):
    """Customer-responded ticket whose due_by is missing or malformed: the SLA
    date simply is not available, so the dashboard says so instead of guessing."""
    if t.get("status") != 2:
        return False
    due = t.get("due_by")
    if not due:
        return True
    return parse_dt(due) is None


def ticket_badges(t, state_row, updated_flag):
    """All badges for one ticket row. Text + CSS class pair; never color alone
    (screen readers and color-blind users get the text)."""
    badges = []
    result = state_row.get("review_result", "Unreviewed") if state_row else "Unreviewed"
    # Display text: the Opened state renders as the all-caps "OPENED / IN REVIEW"
    # badge (spec §3.5/§7) while the select option value stays sentence case.
    badge_text = "OPENED / IN REVIEW" if result == "Opened / In Review" else result
    badges.append(("review", badge_text, "b-review " + REVIEW_CLASS.get(result, "rv-unreviewed")))
    if is_overdue(t):
        badges.append(("attr", "OVERDUE", "b-overdue"))
    if is_customer_responded(t):
        badges.append(("attr", "CUSTOMER RESPONDED", "b-responded"))
    if is_waiting_on_customer(t):
        badges.append(("attr", "WAITING ON CUSTOMER", "b-waiting"))
    if has_missing_tags(t):
        badges.append(("attr", "MISSING TAGS", "b-missing"))
    if sla_unavailable(t):
        badges.append(("attr", "SLA DATE UNAVAILABLE", "b-sla"))
    if updated_flag:
        badges.append(("attr", "UPDATED SINCE REVIEW", "b-updated"))
    return badges


# ---------------------------------------------------------------------------
# CSRF protection (loopback-appropriate)
# ---------------------------------------------------------------------------


def get_csrf_token():
    token = session.get("csrf_token")
    if not token:
        token = secrets.token_urlsafe(32)
        session["csrf_token"] = token
    return token


def csrf_valid(token):
    expected = session.get("csrf_token")
    return bool(expected) and bool(token) and hmac.compare_digest(expected, token)


# ---------------------------------------------------------------------------
# Freshdesk ticket URLs (never followed in tests)
# ---------------------------------------------------------------------------


def ticket_url(ticket_id):
    return f"https://{FRESHDESK_DOMAIN}/a/tickets/{ticket_id}"


def fmt_due(due_str):
    if not due_str:
        return "—"
    try:
        dt = datetime.fromisoformat(due_str.replace("Z", "+00:00"))
        now = now_utc()
        delta = dt - now
        days = int(delta.total_seconds() // 86400)
        hours = int((delta.total_seconds() % 86400) // 3600)
        if delta.total_seconds() < 0:
            return f"<span style='color:red;font-weight:bold'>{abs(days)}d {abs(hours)}h OVERDUE</span>"
        return f"{days}d {hours}h left"
    except Exception:
        return due_str


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------


def _queue_error_page(message, offline):
    return _queue_render(
        tickets=[], total=0, offline=offline, cache_age=None,
        error=message, config=dict(DEFAULT_FILTERS), all_categories_off=False,
    )


@app.route("/queue")
def queue():
    config = filters_from_args(request.args)
    offline = is_offline()

    # Missing-key warning so the user notices before a blank page. Skipped in
    # offline mode — offline mode works without a key and never reads it.
    if not offline and not load_api_key():
        return _queue_error_page(
            "No Freshdesk API key found. Set FRESHDESK_API_KEY env var or write it to "
            "~/.config/furtouch/freshdesk_api_key (chmod 600).",
            offline,
        )

    try:
        raw, cache_age = get_ticket_pool()
    except OfflineDataError as e:
        return _queue_error_page(str(e), offline)
    except requests.exceptions.HTTPError as e:
        resp = getattr(e, "response", None)
        detail = resp.status_code if resp is not None else str(e)
        return _queue_error_page(
            f"Freshdesk API error: {detail} — check your API key and permissions.", offline,
        )
    except Exception as e:
        return _queue_error_page(f"Error fetching tickets: {e}", offline)

    all_categories_off = not (config["overdue"] or config["responded"] or config["waiting"])
    if all_categories_off:
        tickets = []
    else:
        tickets = apply_queue_filters(raw, config)

    state_rows = load_review_rows()

    # Per-ticket view decision + row data (single row per ticket, sorted by id).
    rows = []
    for t in sorted(tickets, key=lambda x: (x.get("id") is None, x.get("id") or 0)):
        tid = t["id"]
        state_row = state_rows.get(tid)
        updated_flag = updated_since_review(t, state_row)
        if not review_view_includes(state_row, updated_flag, config["review_view"]):
            continue
        sid = t.get("status")
        pid = t.get("priority", 0)
        due = t.get("due_by") or t.get("fr_due_by")
        updated_at = t.get("updated_at")
        rows.append({
            "id": tid,
            "url": ticket_url(tid),
            "subject": t.get("subject", ""),
            "status_label": STATUS_LABELS.get(sid, f"Status {sid}"),
            "priority_label": PRIORITY_LABELS.get(pid, f"P{pid}"),
            "due_display": fmt_due(due),
            "updated_display": (updated_at or "")[:16].replace("T", " ") or "—",
            "created_display": (t.get("created_at") or "")[:10] or "—",
            "tags": (t.get("tags") or []) if isinstance(t.get("tags"), list) else [],
            "type": t.get("type"),
            "result": state_row.get("review_result", "Unreviewed") if state_row else "Unreviewed",
            "row_class": REVIEW_CLASS.get(
                state_row.get("review_result", "Unreviewed") if state_row else "Unreviewed",
                "rv-unreviewed",
            ),
            "badges": ticket_badges(t, state_row, updated_flag),
        })

    flash_msg = session.pop("flash", None)

    return _queue_render(
        tickets=rows, total=len(rows), error=None,
        offline=offline, cache_age=cache_age, config=config,
        csrf_token=get_csrf_token(), flash=flash_msg,
        all_categories_off=all_categories_off,
    )


@app.route("/queue/api/review", methods=["POST"])
def review_api():
    """Save a local review result (form POST). Redirects back to the exact
    queue view the user was on, preserving every filter parameter."""
    config = filters_from_args(request.form)

    def back_to_queue(msg, ok):
        session["flash"] = ("ok" if ok else "err", msg)
        return redirect(f"/queue?{filter_query_string(config)}", code=303)

    if not csrf_valid(request.form.get("csrf_token")):
        return back_to_queue("Review not saved: invalid security token. Reload the page and try again.", False)

    raw_ticket_id = request.form.get("ticket_id")
    if raw_ticket_id is None or str(raw_ticket_id).strip() == "":
        return back_to_queue("Review not saved: missing ticket ID.", False)
    try:
        ticket_id = int(raw_ticket_id)
    except (TypeError, ValueError):
        return back_to_queue("Review not saved: invalid ticket ID.", False)

    result = request.form.get("review_result")
    if result not in REVIEW_STATES:
        return back_to_queue("Review not saved: unknown review result.", False)

    # Snapshot the ticket's current updated_at for Reviewed states. The lookup
    # reads the same pool the dashboard uses (cache-first; offline = fixtures,
    # so no HTTP is ever triggered by a review update).
    reviewed_updated_at = None
    try:
        pool, _ = get_ticket_pool()
        match = next((t for t in pool if t.get("id") == ticket_id), None)
    except Exception:
        match = None
    if match is None:
        return back_to_queue(f"Review not saved: unknown ticket #{ticket_id}.", False)
    if result in REVIEWED_STATES:
        reviewed_updated_at = match.get("updated_at")

    try:
        set_review_result(ticket_id, result, reviewed_updated_at)
    except Exception as e:
        return back_to_queue(f"Review not saved: database error ({e}).", False)

    return back_to_queue(f"Review saved for #{ticket_id}: {result}.", True)


@app.route("/queue/api/opened", methods=["POST"])
def opened_api():
    """Record that a ticket link was opened (JSON). The click handler calls
    this without preventing the new tab, so the Freshdesk ticket always opens
    even if recording fails. Success/failure is reported honestly: the UI only
    claims the mark was saved when this endpoint returns ok."""
    token = request.headers.get("X-CSRF-Token") or (request.get_json(silent=True) or {}).get("csrf_token")
    if not csrf_valid(token):
        return jsonify({"ok": False, "error": "invalid security token"}), 403

    body = request.get_json(silent=True) or {}
    raw_ticket_id = body.get("ticket_id")
    if raw_ticket_id is None:
        return jsonify({"ok": False, "error": "missing ticket_id"}), 400
    try:
        ticket_id = int(raw_ticket_id)
    except (TypeError, ValueError):
        return jsonify({"ok": False, "error": "invalid ticket_id"}), 400

    try:
        pool, _ = get_ticket_pool()
        known = any(t.get("id") == ticket_id for t in pool)
    except Exception:
        known = False
    if not known:
        return jsonify({"ok": False, "error": "unknown ticket id"}), 404

    try:
        result = mark_opened(ticket_id)
    except Exception as e:
        return jsonify({"ok": False, "error": f"database error: {e}"}), 500

    return jsonify({"ok": True, "review_result": result})


def resolve_bind_host(host):
    """Refuse unsafe external binds. The scanner must stay on the loopback
    interface. Raises SystemExit for 0.0.0.0 so misuse is loud and immediate.
    """
    if host == "0.0.0.0":
        raise SystemExit(
            "Refusing to bind to 0.0.0.0. Set HOST=127.0.0.1 or export PORT=5050."
        )
    return host


# ---------------------------------------------------------------------------
# Dashboard template
# ---------------------------------------------------------------------------

QUEUE_HTML = """\
<!doctype html><html lang=en><head><meta charset=utf-8>
<meta name=viewport content="width=device-width,initial-scale=1">
<title>Freshdesk Review Queue</title>
<style>
 body{font-family:system-ui,Arial,sans-serif;max-width:1100px;margin:auto;padding:16px;background:#f5f5f5;color:#222}
 h1{font-size:22px;margin:0 0 4px}
 .sub{color:#666;font-size:13px;margin-bottom:16px}
 .banner{background:#fff3cd;border:1px solid #e0c060;padding:8px 12px;border-radius:6px;font-size:13px;margin-bottom:14px}
 .banner.err{background:#fdecea;border-color:#d66;color:#8a1f1f}
 .banner.ok{background:#e8f5e9;border-color:#6a9;color:#1e4d2b}
 .controls{background:#fff;border:1px solid #ddd;border-radius:8px;padding:12px 14px;margin-bottom:14px}
 .controls .row{display:flex;gap:16px;flex-wrap:wrap;align-items:center;margin-bottom:8px}
 .controls .row:last-child{margin-bottom:0}
 .controls label{font-size:13px;display:inline-flex;align-items:center;gap:5px;cursor:pointer}
 .controls input[type=number]{width:70px;padding:5px 8px;font-size:13px;border:1px solid #bbb;border-radius:4px}
 .controls select{padding:5px 8px;font-size:13px;border:1px solid #bbb;border-radius:4px}
 .controls button{padding:6px 14px;font-size:13px;border-radius:4px;border:1px solid #388e3c;background:#388e3c;color:#fff;cursor:pointer}
 .controls button.reset{background:#fff;color:#666;border-color:#bbb}
 .controls a.preset{font-size:12px;color:#1565c0;margin-left:8px}
 .field{display:flex;align-items:center;gap:6px}
 .field .lbl{font-size:13px;color:#444;white-space:nowrap}
 .count{font-size:13px;color:#555;margin-bottom:8px}
 .tablewrap{overflow-x:auto;background:#fff;border:1px solid #ddd;border-radius:8px}
 table{border-collapse:collapse;width:100%;font-size:13px;min-width:960px}
 th,td{text-align:left;padding:8px 10px;border-bottom:1px solid #eee;vertical-align:top}
 th{background:#fafafa;font-size:12px;color:#666;white-space:nowrap}
 tr.rv-unreviewed{background:#fff}
 tr.rv-opened{background:#fff8e1}
 tr.rv-opened td:first-child{box-shadow:inset 4px 0 0 #f9a825}
 tr.rv-resolved{background:#e8f5e9}
 tr.rv-na{background:#eeeeee}
 tr.rv-none{background:#e3f2fd}
 tr.rv-followup{background:#fff3e0}
 a.tid{font-weight:bold;color:#1565c0;text-decoration:none}
 a.tid:hover{text-decoration:underline}
 a.sbj{color:#222;text-decoration:none}
 a.sbj:hover{text-decoration:underline}
 .badges{display:flex;flex-wrap:wrap;gap:4px}
 .badge{font-size:11px;font-weight:bold;padding:2px 6px;border-radius:4px;white-space:nowrap;letter-spacing:.02em}
 .b-review{border:1px solid #bbb;color:#333;background:#fff}
 .b-review.rv-opened{background:#fff8e1;border-color:#f9a825;color:#5d4037}
 .b-overdue{background:#d32f2f;color:#fff}
 .b-responded{background:#f9a825;color:#222}
 .b-waiting{background:#7b1fa2;color:#fff}
 .b-missing{background:#757575;color:#fff}
 .b-sla{background:#e65100;color:#fff}
 .b-updated{background:#00838f;color:#fff}
 .toast{position:fixed;right:16px;bottom:16px;max-width:340px;background:#fdecea;border:1px solid #d66;color:#8a1f1f;padding:10px 14px;border-radius:6px;font-size:13px;box-shadow:0 2px 8px rgba(0,0,0,.15);z-index:99}
 .toast.hidden{display:none}
 .meta{color:#666;white-space:nowrap}
 .empty{color:#666;padding:24px;text-align:center;font-size:14px}
 .rvform{margin:0}
 .rvform select{padding:4px 6px;font-size:12px;border:1px solid #bbb;border-radius:4px;max-width:150px}
 .foot{color:#999;font-size:11px;margin-top:10px}
</style></head><body>
<h1>Freshdesk Review Queue</h1>
<div class=sub>{% if offline %}<strong>OFFLINE MODE</strong> — using mock/offline fixture data. No network access.{% else %}Live mode — read-only ticket list.{% endif %}
{% if cache_age is not none %} · cache {{ cache_age }}s old{% endif %}</div>

{% if flash %}
<div class="banner {{ 'ok' if flash[0] == 'ok' else 'err' }}" role=status>{{ flash[1] }}</div>
{% endif %}
{% if error %}
<div class="banner err" role=alert>{{ error }}</div>
{% endif %}

<form class=controls method=get action=/queue>
  <div class=row>
    <span class=field><span class=lbl>Tickets updated in the last</span>
      <input type=number name=days min=1 max=365 value={{ config.days }} aria-label="Days back">
      <span class=lbl>days</span>
      {% for d in [7, 14, 30, 60] %}<a class=preset href="/queue?{{ preset_urls[d] }}">{{ d }}d</a>{% endfor %}
    </span>
    <span class=field><label><input type=checkbox name=overdue value=1 {{ 'checked' if config.overdue }}> Overdue</label></span>
    <span class=field><label><input type=checkbox name=responded value=1 {{ 'checked' if config.responded }}> Customer Responded</label></span>
    <span class=field><label><input type=checkbox name=waiting value=1 {{ 'checked' if config.waiting }}> Waiting on Customer</label></span>
    <span class=field><label><input type=checkbox name=missing_tags value=1 {{ 'checked' if config.missing_tags }}> Missing Tags</label></span>
  </div>
  <div class=row>
    <span class=field><label for=review_view>Review view</label>
      <select id=review_view name=review_view>
        {% for v in ['active','completed','all'] %}<option value={{ v }} {{ 'selected' if config.review_view == v }}>{% if v == 'active' %}Active{% elif v == 'completed' %}Completed{% else %}All{% endif %}</option>{% endfor %}
      </select></span>
    <button type=submit>Apply Filters</button>
    <a class="controls button reset" href=/queue role=button aria-label="Reset filters to defaults">Reset to Defaults</a>
  </div>
</form>

<p class=count>{{ total }} tickets matching your filters</p>
{% if all_categories_off %}
<div class=empty>Select at least one ticket category to display results.</div>
{% elif tickets %}
<div class=tablewrap>
<table>
<caption class=visually-hidden>Freshdesk review queue</caption>
<tr>
  <th scope=col>Ticket</th><th scope=col>Subject</th><th scope=col>Status</th>
  <th scope=col>Badges</th><th scope=col>Review</th><th scope=col>Priority</th>
  <th scope=col>Due / SLA</th><th scope=col>Updated</th><th scope=col>Created</th>
  <th scope=col>Tags</th><th scope=col>Type</th>
</tr>
{% for t in tickets %}
<tr class="{{ t.row_class }}">
  <td><a class="tid fd-link" href="{{ t.url }}" target=_blank rel="noopener noreferrer" data-ticket-id="{{ t.id }}" aria-label="Open ticket #{{ t.id }} in Freshdesk (new tab)">#{{ t.id }}</a></td>
  <td><a class="sbj fd-link" href="{{ t.url }}" target=_blank rel="noopener noreferrer" data-ticket-id="{{ t.id }}" aria-label="Open subject of ticket #{{ t.id }} in Freshdesk (new tab)">{{ t.subject }}</a></td>
  <td>{{ t.status_label }}</td>
  <td><div class=badges>{% for kind, text, cls in t.badges %}<span class="badge {{ cls }}">{{ text }}</span>{% endfor %}</div></td>
  <td>
    <form class=rvform method=post action=/queue/api/review>
      <input type=hidden name=csrf_token value="{{ csrf_token }}">
      <input type=hidden name=ticket_id value="{{ t.id }}">
      <input type=hidden name=overdue value="{{ '1' if config.overdue else '0' }}">
      <input type=hidden name=responded value="{{ '1' if config.responded else '0' }}">
      <input type=hidden name=waiting value="{{ '1' if config.waiting else '0' }}">
      <input type=hidden name=missing_tags value="{{ '1' if config.missing_tags else '0' }}">
      <input type=hidden name=days value="{{ config.days }}">
      <input type=hidden name=review_view value="{{ config.review_view }}">
      <select name=review_result aria-label="Review result for ticket {{ t.id }}" onchange="this.form.submit()">
        {% for s in review_states %}<option value="{{ s }}" {{ 'selected' if t.result == s }}>{{ s }}</option>{% endfor %}
      </select>
    </form>
  </td>
  <td>{{ t.priority_label }}</td>
  <td class=meta>{{ t.due_display | safe }}</td>
  <td class=meta>{{ t.updated_display }}</td>
  <td class=meta>{{ t.created_display }}</td>
  <td>{% if t.tags %}{{ t.tags|join(', ') }}{% else %}<em style=color:#bbb>none</em>{% endif %}</td>
  <td>{{ t.type or '—' }}</td>
</tr>
{% endfor %}
</table>
</div>
{% else %}
<div class=empty>No tickets match the current filter.</div>
{% endif %}

<div class=foot>Review results are stored locally only (SQLite) and are never sent to Freshdesk. Ticket links open Freshdesk in a new tab; opening a ticket marks it as Opened / In Review locally.</div>

<script>
var CSRF_TOKEN = {{ csrf_token_json | safe }};
var REVIEW_CLASS = {
  'Unreviewed': 'rv-unreviewed',
  'Opened / In Review': 'rv-opened',
  'Resolved': 'rv-resolved',
  'Not Applicable to Me': 'rv-na',
  'No Action Needed': 'rv-none',
  'Needs Follow-Up': 'rv-followup'
};
function badgeText(result) { return result === 'Opened / In Review' ? 'OPENED / IN REVIEW' : result; }
var toastEl = null;
function showError(msg) {
  if (!toastEl) {
    toastEl = document.createElement('div');
    toastEl.className = 'toast hidden';
    toastEl.setAttribute('role', 'status');
    document.body.appendChild(toastEl);
  }
  toastEl.textContent = msg;
  toastEl.classList.remove('hidden');
  clearTimeout(showError._t);
  showError._t = setTimeout(function () { toastEl.classList.add('hidden'); }, 6000);
}
// Anchor on the reliable data-ticket-id identifier (spec section 4). Covers
// both the ticket-number and subject links. The default navigation is never
// prevented: the Freshdesk ticket always opens in a new tab synchronously,
// and the local state request runs in the background (spec section 5).
document.querySelectorAll('a[data-ticket-id]').forEach(function (a) {
  a.addEventListener('click', function () {
    var tid = this.getAttribute('data-ticket-id');
    if (!tid) { return; }
    var row = this.closest('tr');
    fetch('/queue/api/opened', {
      method: 'POST',
      headers: {'Content-Type': 'application/json', 'X-CSRF-Token': CSRF_TOKEN},
      body: JSON.stringify({ticket_id: tid})
    }).then(function (r) { return r.json(); }).then(function (d) {
      if (d && d.ok && d.review_result) {
        // Confirmed saved: highlight the row and update badge + selector.
        var cls = REVIEW_CLASS[d.review_result] || 'rv-unreviewed';
        if (row) {
          var extra = [];
          row.classList.forEach(function (c) { if (c.indexOf('rv-') !== 0) { extra.push(c); } });
          row.className = extra.concat([cls]).join(' ');
        }
        var badge = row ? row.querySelector('.b-review') : null;
        if (badge) { badge.className = 'badge b-review ' + cls; badge.textContent = badgeText(d.review_result); }
        var sel = row ? row.querySelector('select[name=review_result]') : null;
        if (sel && sel.querySelector('option[value="' + d.review_result + '"]')) { sel.value = d.review_result; }
      } else {
        showError('Could not save Opened / In Review state for #' + tid + ' (not saved).');
      }
    }).catch(function () {
      showError('Could not save Opened / In Review state for #' + tid + ' (not saved).');
    });
  });
});
setTimeout(function(){ location.reload(); }, 300000); // auto-refresh every 5 min
</script>
</body></html>
"""


def _queue_render(**kwargs):
    """Render QUEUE_HTML with the shared context merged in."""
    ctx = dict(kwargs)
    cfg = ctx.get("config") or dict(DEFAULT_FILTERS)
    ctx.setdefault("config", cfg)
    ctx.setdefault("csrf_token", get_csrf_token())
    ctx.setdefault("flash", None)
    token = ctx["csrf_token"]
    ctx.setdefault("csrf_token_json", json.dumps(token))
    ctx.setdefault("review_states", REVIEW_STATES)
    ctx.setdefault("preset_urls", {d: filter_query_string(dict(cfg, days=d)) for d in (7, 14, 30, 60)})
    return render_template_string(QUEUE_HTML, **ctx)


if __name__ == "__main__":
    host = os.environ.get("HOST", "127.0.0.1")
    port = int(os.environ.get("PORT", "5050"))
    app.run(host=resolve_bind_host(host), port=port, debug=False)
