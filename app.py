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
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from math import ceil
from typing import Callable, Optional
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

# Freshdesk Filter Tickets API v2 contract, documented in
# docs/closed_housekeeping_api_contract.md. This closed-housekeeping foundation
# deliberately has no live HTTP adapter: all retrieval is fake/injectable.
CLOSED_STATUS = 5
SEARCH_PAGE_SIZE = 30
SEARCH_MAX_PAGE = 10
SEARCH_MAX_RESULTS = SEARCH_PAGE_SIZE * SEARCH_MAX_PAGE
CLOSED_MAX_SPLIT_DEPTH = 20
CLOSED_MAX_WINDOWS = 512
CLOSED_DEFAULT_DAYS = 60
CLOSED_MIN_DAYS = 1
CLOSED_MAX_DAYS = 3650

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


_VIEW_LABEL = {"active": "Active", "completed": "Completed", "all": "All"}


def filter_summary_text(config):
    """Human-readable active-filter summary derived solely from the canonical
    filter config (which itself comes from the parsed URL state). Rendered as
    the dashboard's filter-summary bar so it always matches the filters the
    page is actually using."""
    prim = []
    if config.get("overdue"):
        prim.append("Overdue")
    if config.get("responded"):
        prim.append("Customer Responded")
    if config.get("waiting"):
        prim.append("Waiting on Customer")
    if not prim:
        return "Showing: No ticket category selected"
    label = _VIEW_LABEL.get(config.get("review_view"), _VIEW_LABEL["active"])
    segments = list(prim)
    if config.get("missing_tags"):
        segments.append("Missing Tags")
    return f"Showing: {' + '.join(segments)} \u00b7 Last {config.get('days', 60)} days \u00b7 {label}"



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


def has_primary_filter(config):
    """At least one of Overdue / Customer Responded / Waiting on Customer must
    be selected. When all three are OFF no category restriction is in effect,
    so no tickets are shown and the 'select a filter' message is displayed."""
    return config["overdue"] or config["responded"] or config["waiting"]


def matches_status_group(t, config):
    """OR within the status group (Customer Responded / Waiting on Customer).
    The two are mutually exclusive Freshdesk statuses: selecting exactly one
    shows only that status, selecting both shows either. When neither is
    selected the status group imposes no restriction (this is what allows
    Overdue-only filtering)."""
    responded_on = config.get("responded") and is_customer_responded(t)
    waiting_on = config.get("waiting") and is_waiting_on_customer(t)
    if not (config.get("responded") or config.get("waiting")):
        return True  # no status restriction
    return responded_on or waiting_on


def matches_overdue(t, config):
    """Overdue is a separate AND condition combined with the selected status
    group. When Overdue is OFF it imposes no restriction."""
    return (not config.get("overdue")) or is_overdue(t)


def matches_missing_tags(t, config):
    """Missing Tags is a separate AND condition. When OFF it imposes no
    restriction."""
    return (not config.get("missing_tags")) or has_missing_tags(t)


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
    keyword gate, then the mixed filter model — at least one primary filter
    selected (Overdue / Customer Responded / Waiting on Customer), the status
    group ORs the two statuses, and Overdue and Missing Tags each AND in as
    separate dimensions. Days-back window is intentionally NOT part of this
    predicate (it is applied separately as an AND gate at render time).

    `config` is a filter dict from filters_from_args(); when omitted the
    documented defaults apply (Overdue ON, Customer Responded OFF, Waiting
    OFF, Missing Tags ON) — the original scanner's default behavior.
    """
    if t.get("status") not in SCAN_STATUSES:
        return False  # Closed/Resolved/Open tickets are never part of the review queue
    if not keyword_filter_hits(t.get("subject")):
        return False
    cfg = config or dict(DEFAULT_FILTERS)
    if not has_primary_filter(cfg):
        return False
    if not matches_status_group(t, cfg):
        return False
    if not matches_overdue(t, cfg):
        return False
    if not matches_missing_tags(t, cfg):
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


def last_opened_ticket_id():
    """Ticket id of the single most recently opened ticket, or None.

    The Last Opened focus marker is derived from the newest valid
    `last_opened_at` across review_state rows (spec §3/§6) — never from the
    review result, so a ticket can be e.g. "Resolved + Last Opened" at the
    same time. Selection is deterministic:

    - every stored last_opened_at is parsed as UTC; missing, empty, or
      malformed values are skipped (fail safe);
    - the chosen ticket is the maximum (last_opened_at, ticket_id), so two
      records with identical timestamps resolve to the higher ticket id
      instead of leaving the marker ambiguous;
    - None is returned when no ticket has ever been opened, in which case
      the dashboard renders neither marker nor jump control.
    """
    conn = _db_conn()
    try:
        rows = conn.execute(
            "SELECT ticket_id, last_opened_at FROM review_state"
        ).fetchall()
    finally:
        conn.close()
    best = None  # (parsed UTC datetime, ticket_id)
    for r in rows:
        ts = parse_dt(r["last_opened_at"])
        if ts is None:
            continue  # missing / malformed timestamp fails safe
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=timezone.utc)  # naive stored value == UTC
        key = (ts, r["ticket_id"])
        if best is None or key > best:
            best = key
    return best[1] if best else None


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
# Closed-ticket housekeeping: offline-only search simulation
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class ClosedWindow:
    start: date
    end: date
    depth: int = 0


@dataclass
class ClosedRetrieval:
    tickets: list = field(default_factory=list)
    complete: bool = True
    warnings: list = field(default_factory=list)
    errors: list = field(default_factory=list)
    windows_planned: list = field(default_factory=list)
    windows_completed: list = field(default_factory=list)
    pages_requested: list = field(default_factory=list)
    reported_total_sum: int = 0
    unique_ticket_count: int = 0
    duplicate_count: int = 0
    date_range: tuple = None
    missing_tags_only: bool = True
    source_mode: str = "offline-synthetic"


def parse_closed_days(value) -> int:
    """Canonical positive integer day count; invalid values fail safely to 60."""
    try:
        if isinstance(value, bool) or not re.fullmatch(r"[0-9]+", str(value or "")):
            raise ValueError
        days = int(value)
        if not CLOSED_MIN_DAYS <= days <= CLOSED_MAX_DAYS:
            raise ValueError
        return days
    except (TypeError, ValueError):
        return CLOSED_DEFAULT_DAYS


def closed_filters_from_args(args):
    # A hidden form field supplies 0 when the checkbox is unchecked; use the
    # final value so checked 1 wins when Werkzeug exposes both values.
    values = args.getlist("missing_tags") if hasattr(args, "getlist") else []
    raw_missing = values[-1] if values else args.get("missing_tags", "1")
    return {
        "days": parse_closed_days(args.get("days", CLOSED_DEFAULT_DAYS)),
        "missing_tags": parse_bool(raw_missing),
    }


def closed_query_string(start: date, end: date, missing_tags_only: bool) -> str:
    """Build a fixed, quoted Freshdesk search query from validated dates only."""
    if not isinstance(start, date) or not isinstance(end, date) or start > end:
        raise ValueError("Closed date range is invalid.")
    clauses = ["status:5"]
    if missing_tags_only:
        clauses.append("tag:null")
    clauses += [f"closed_at:>'{start.isoformat()}'", f"closed_at:<'{end.isoformat()}'"]
    return '"' + " AND ".join(clauses) + '"'


def closed_search_url_params(start: date, end: date, missing_tags_only: bool, page: int):
    if not isinstance(page, int) or not 1 <= page <= SEARCH_MAX_PAGE:
        raise ValueError("Search page must be between 1 and 10.")
    return urlencode({"query": closed_query_string(start, end, missing_tags_only), "page": page})


def split_closed_window(window: ClosedWindow):
    """Calendar midpoint split with one inclusive boundary overlap."""
    if window.start >= window.end:
        return None
    midpoint = window.start + timedelta(days=(window.end - window.start).days // 2)
    # Calendar ranges are partitioned, not overlapped, to guarantee a >300
    # two-day range can reduce. Deduplication still protects against an API
    # returning boundary duplicates in a future live transport.
    return (ClosedWindow(window.start, midpoint, window.depth + 1),
            ClosedWindow(midpoint + timedelta(days=1), window.end, window.depth + 1))


def _closed_at_date(ticket):
    raw = ticket.get("closed_at")
    if not isinstance(raw, str):
        return None
    try:
        return datetime.fromisoformat(raw.replace("Z", "+00:00")).date()
    except ValueError:
        return None


def _synthetic_closed_tickets():
    """Synthetic-only fixture corpus. Large cases are generated deterministically."""
    base = [
        {"id": 810001, "subject": "Synthetic closed untagged", "status": 5, "closed_at": "2026-08-04T09:00:00Z", "tags": []},
        {"id": 810002, "subject": "Synthetic closed tagged", "status": 5, "closed_at": "2026-08-03T10:00:00Z", "tags": ["parts"]},
        {"id": 810003, "subject": "Synthetic closed same timestamp low", "status": 5, "closed_at": "2026-08-02T10:00:00Z", "tags": []},
        {"id": 810004, "subject": "Synthetic closed same timestamp high", "status": 5, "closed_at": "2026-08-02T10:00:00Z", "tags": []},
        {"id": 810005, "subject": "Synthetic resolved excluded", "status": 4, "closed_at": "2026-08-01T10:00:00Z", "tags": []},
        {"id": 810006, "subject": "Synthetic missing date", "status": 5, "tags": []},
        {"id": 810007, "subject": "Synthetic malformed date", "status": 5, "closed_at": "not-a-date", "tags": []},
        {"id": 810008, "subject": "Synthetic outside range", "status": 5, "closed_at": "2025-01-01T10:00:00Z", "tags": []},
    ]
    # 301 tickets across two dates proves planner splitting without a 300-row file.
    for i in range(301):
        # Spread across 301 calendar dates so a >300 range splits cleanly;
        # custom fake transports cover the unsplittable single-day case.
        day = date(2025, 10, 8) + timedelta(days=i)
        base.append({"id": 820000 + i, "subject": f"Synthetic split ticket {i}", "status": 5,
                     "closed_at": f"{day.isoformat()}T12:00:00Z", "tags": []})
    return base


def synthetic_closed_search_page(window: ClosedWindow, missing_tags_only: bool, page: int):
    """Injectable GET-like page reader. It has no network or credential path."""
    if page > SEARCH_MAX_PAGE:
        raise ValueError("Synthetic transport refuses page 11.")
    matches = []
    for ticket in _synthetic_closed_tickets():
        closed = _closed_at_date(ticket)
        if ticket.get("status") != CLOSED_STATUS or closed is None:
            continue
        if missing_tags_only and ticket.get("tags"):
            continue
        if window.start <= closed <= window.end:
            matches.append(ticket)
    # Deliberate duplicate across pages/windows to exercise deduplication.
    matches.sort(key=lambda t: t["id"])
    start = (page - 1) * SEARCH_PAGE_SIZE
    return {"total": len(matches), "results": matches[start:start + SEARCH_PAGE_SIZE]}


def retrieve_closed_tickets(start: date, end: date, missing_tags_only: bool,
                             search_page: Callable = synthetic_closed_search_page):
    """Deterministic search planning. The transport returns dict(total, results)."""
    result = ClosedRetrieval(date_range=(start, end), missing_tags_only=missing_tags_only)
    pending = [ClosedWindow(start, end)]
    seen = {}
    while pending:
        if len(result.windows_planned) >= CLOSED_MAX_WINDOWS:
            result.complete = False; result.errors.append("Maximum date-window count reached."); break
        window = pending.pop(0); result.windows_planned.append(window)
        try:
            first = search_page(window, missing_tags_only, 1)
            if not isinstance(first, dict) or not isinstance(first.get("total"), int) or not isinstance(first.get("results"), list):
                raise ValueError("Malformed search response: expected integer total and results list.")
            total = first["total"]
            if total < 0: raise ValueError("Malformed search response: negative total.")
            result.reported_total_sum += total
            if total > SEARCH_MAX_RESULTS:
                children = split_closed_window(window)
                if children is None:
                    result.complete = False; result.errors.append("More than 300 matching tickets were closed on one date. This range cannot be fully retrieved through the Search Tickets page limit.")
                    continue
                if window.depth >= CLOSED_MAX_SPLIT_DEPTH:
                    result.complete = False; result.errors.append("Maximum date-window split depth reached."); continue
                pending[0:0] = children
                continue
            pages = ceil(total / SEARCH_PAGE_SIZE)
            responses = [first]
            result.pages_requested.append((window, 1))
            for page in range(2, pages + 1):
                result.pages_requested.append((window, page))
                response = search_page(window, missing_tags_only, page)
                if not isinstance(response, dict) or response.get("total") != total or not isinstance(response.get("results"), list):
                    raise ValueError("Malformed or inconsistent search page response.")
                responses.append(response)
            received = [t for response in responses for t in response["results"]]
            if len(received) < total:
                raise ValueError("Search page ended before the reported total was retrieved.")
            for ticket in received:
                if not isinstance(ticket, dict) or not isinstance(ticket.get("id"), int):
                    raise ValueError("Malformed ticket in search response.")
                ticket_date = _closed_at_date(ticket)
                if ticket.get("status") != CLOSED_STATUS or ticket_date is None or not window.start <= ticket_date <= window.end:
                    result.complete = False; result.warnings.append("Ignored malformed or out-of-contract ticket from synthetic response."); continue
                if missing_tags_only and ticket.get("tags"):
                    continue
                if ticket["id"] in seen: result.duplicate_count += 1
                seen[ticket["id"]] = ticket
            result.windows_completed.append(window)
        except Exception as exc:
            result.complete = False
            result.errors.append(str(exc))
    result.tickets = sorted(seen.values(), key=lambda t: (_closed_at_date(t), t["id"]), reverse=True)
    result.unique_ticket_count = len(result.tickets)
    return result


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------


def _queue_error_page(message, offline):
    return _queue_render(
        tickets=[], total=0, offline=offline, cache_age=None,
        error=message, config=dict(DEFAULT_FILTERS), all_categories_off=False,
    )


@app.route("/closed")
def closed_housekeeping():
    """Offline-only closed-ticket page. It cannot use the queue's live path."""
    if not is_offline():
        return _closed_render(error="Closed Ticket Housekeeping is offline-only in this milestone.", result=None,
                              config=closed_filters_from_args(request.args)), 503
    config = closed_filters_from_args(request.args)
    # Local date is intentionally used for display range. Search predicates use
    # explicit YYYY-MM-DD calendar dates; see the UTC caveat in the contract doc.
    end = now_utc().date()
    start = end - timedelta(days=config["days"] - 1)
    try:
        result = retrieve_closed_tickets(start, end, config["missing_tags"])
        return _closed_render(result=result, config=config, error=None)
    except Exception:
        return _closed_render(result=None, config=config,
                              error="Closed synthetic retrieval failed safely. No live fallback was attempted."), 500


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
    last_opened_id = last_opened_ticket_id()  # focus state, independent of filters

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
        row_class = REVIEW_CLASS.get(
            state_row.get("review_result", "Unreviewed") if state_row else "Unreviewed",
            "rv-unreviewed",
        )
        is_last_opened = last_opened_id is not None and tid == last_opened_id
        if is_last_opened:
            row_class += " rv-last-opened"  # layered on top of the review class
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
            "row_class": row_class,
            "last_opened": is_last_opened,
            "badges": ticket_badges(t, state_row, updated_flag),
        })

    flash_msg = session.pop("flash", None)

    return _queue_render(
        tickets=rows, total=len(rows), error=None,
        offline=offline, cache_age=cache_age, config=config,
        csrf_token=get_csrf_token(), flash=flash_msg,
        all_categories_off=all_categories_off,
        last_opened_id=last_opened_id,
        last_opened_rendered=any(r["last_opened"] for r in rows),
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

    return jsonify({
        "ok": True,
        "review_result": result,
        "last_opened_id": last_opened_ticket_id(),
    })


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

# Shared application theme (design source of truth = the /queue theme).
# Both QUEUE_HTML and CLOSED_HTML reference this single stylesheet so the two
# pages share body background, content width, typography, navigation, panels,
# buttons, form controls, tables, badges, links, focus-visible and responsive
# breakpoints. Navigation is rendered by _nav_html(current).
_SHARED_CSS = """
 :root{--fd-customer-responded:#09218D;--fd-customer-responded-text:#FFFFFF;--fd-waiting-customer:#E9AE3D;--fd-waiting-customer-text:#1A1A1A;--fd-last-opened:#6A1B9A;--fd-last-opened-text:#FFFFFF}
 body{font-family:system-ui,Arial,sans-serif;max-width:1100px;margin:auto;padding:16px;background:#f5f5f5;color:#222}
 h1{font-size:22px;margin:0 0 4px}
 .sub{color:#666;font-size:13px;margin-bottom:16px}
 .banner{background:#fff3cd;border:1px solid #e0c060;padding:8px 12px;border-radius:6px;font-size:13px;margin-bottom:14px}
 .banner.err{background:#fdecea;border-color:#d66;color:#8a1f1f}
 .banner.ok{background:#e8f5e9;border-color:#6a9;color:#1e4d2b}
 .controls{background:#fff;border:1px solid #e0e0e0;border-radius:10px;padding:16px 18px;margin-bottom:10px;box-shadow:0 1px 2px rgba(0,0,0,.04)}
 .controls .panel-region{display:flex;flex-wrap:wrap;align-items:center;gap:16px;padding-bottom:14px;margin-bottom:14px;border-bottom:1px solid #f0f0f0}
 .controls .panel-region:last-child{padding-bottom:0;margin-bottom:0;border-bottom:0}
 .region-time{justify-content:space-between}
 .days-field{display:inline-flex;align-items:center;gap:7px;flex-wrap:wrap}
 .days-field .lbl{font-size:13px;color:#444;white-space:nowrap}
 .controls input[type=number]{width:76px;padding:6px 9px;font-size:14px;border:1px solid #bdbdbd;border-radius:6px;background:#fff}
 .controls input[type=number]:focus-visible{outline:2px solid #1a73e8;outline-offset:1px}
 .preset-group{display:inline-flex;flex-wrap:wrap;gap:4px;align-items:center;background:#f3f4f6;border:1px solid #e5e7eb;border-radius:8px;padding:4px}
 .preset{font-size:12px;font-weight:600;color:#444;text-decoration:none;padding:5px 11px;border-radius:6px;line-height:1;letter-spacing:.02em}
 .preset:hover{background:#e5e7eb;color:#111}
 .preset[aria-current=page]{background:#1a73e8;color:#fff;box-shadow:inset 0 0 0 1.5px #175cd3}
 .preset[aria-current=page] .preset-mark{font-weight:700}
 .preset:focus-visible{outline:2px solid #1a73e8;outline-offset:2px}
 .region-groups{display:flex;flex-wrap:wrap;gap:12px}
 .filter-group{border:1px solid #e4e6eb;border-radius:8px;padding:10px 12px 11px;margin:0;min-width:150px;flex:1 1 200px;display:flex;flex-direction:column;gap:7px;background:#fcfcfd}
 .filter-group .group-lbl{font-size:11px;font-weight:700;color:#5f6368;text-transform:uppercase;letter-spacing:.5px;padding:0 2px}
 .field{display:inline-flex;align-items:center;gap:5px}
 .field .lbl{font-size:13px;color:#444;white-space:nowrap}
 .filter-group .field label{display:inline-flex;align-items:center;gap:6px;cursor:pointer;font-size:13px;color:#222;text-transform:none;letter-spacing:0;font-weight:400}
 .filter-group .field input[type=checkbox]{width:16px;height:16px;accent-color:#1a73e8;cursor:pointer}
 .filter-group .field input[type=checkbox]:focus-visible{outline:2px solid #1a73e8;outline-offset:2px}
 .filter-group .field-hint{font-size:11px;color:#8a8f98;line-height:1.4;margin:0}
 .region-actions{display:flex;flex-wrap:wrap;align-items:center;gap:14px;justify-content:space-between}
 .view-field{display:inline-flex;align-items:center;gap:8px}
 .view-field label{font-size:13px;color:#444;white-space:nowrap}
 .controls select{padding:7px 10px;font-size:13px;border:1px solid #bdbdbd;border-radius:6px;background:#fff}
 .controls select:focus-visible{outline:2px solid #1a73e8;outline-offset:2px}
 .action-buttons{display:inline-flex;gap:10px;flex-wrap:wrap;align-items:center}
 .controls button[type=submit]{padding:8px 18px;font-size:13px;font-weight:600;border:1px solid #1565c0;background:#1a73e8;color:#fff;border-radius:6px;cursor:pointer}
 .controls button[type=submit]:hover{background:#1664d0}
 .controls button[type=submit]:focus-visible{outline:2px solid #0d47a1;outline-offset:2px}
 .controls a.reset{display:inline-block;padding:7px 15px;font-size:13px;font-weight:500;color:#5f6368;background:#fff;border:1px solid #c6c9cf;border-radius:6px;text-decoration:none}
 .controls a.reset:hover{border-color:#9aa0a6;color:#202124}
 .controls a.reset:focus-visible{outline:2px solid #1a73e8;outline-offset:2px}
 .filter-summary{font-size:13px;color:#3c4043;background:#f1f3f4;border:1px solid #e0e0e0;border-radius:8px;padding:7px 12px;margin:8px 0 12px}
 @media (max-width:720px){.controls{padding:14px}.region-time{flex-direction:column;align-items:flex-start;gap:10px}.region-groups{flex-direction:column}.filter-group{flex:1 1 auto;min-width:0}.region-actions{flex-direction:column;align-items:flex-start;gap:12px}.action-buttons{width:100%;justify-content:space-between}.controls button[type=submit]{flex:1 1 auto}.controls a.reset{flex:1 1 auto;text-align:center}}
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
 tr.rv-last-opened{outline:3px solid var(--fd-last-opened);outline-offset:-3px}
 tr.rv-last-opened td:first-child{box-shadow:inset 4px 0 0 var(--fd-last-opened)}
 .b-last-opened{background:var(--fd-last-opened);color:var(--fd-last-opened-text)}
 a.tid{font-weight:bold;color:#1565c0;text-decoration:none}
 a.tid:hover{text-decoration:underline}
 a.sbj{color:#222;text-decoration:none}
 a.sbj:hover{text-decoration:underline}
 .badges{display:flex;flex-wrap:wrap;gap:4px}
 .badge{font-size:11px;font-weight:bold;padding:2px 6px;border-radius:4px;white-space:nowrap;letter-spacing:.02em}
 .b-review{border:1px solid #bbb;color:#333;background:#fff}
 .b-review.rv-opened{background:#fff8e1;border-color:#f9a825;color:#5d4037}
 .b-overdue{background:#d32f2f;color:#fff}
 .b-responded{background:var(--fd-customer-responded);color:var(--fd-customer-responded-text)}
 .b-waiting{background:var(--fd-waiting-customer);color:var(--fd-waiting-customer-text)}
 .b-missing{background:#757575;color:#fff}
 .b-sla{background:#e65100;color:#fff}
 .b-updated{background:#00838f;color:#fff}
 .b-closed{background:#00838f;color:#fff}
 .toast{position:fixed;right:16px;bottom:16px;max-width:340px;background:#fdecea;border:1px solid #d66;color:#8a1f1f;padding:10px 14px;border-radius:6px;font-size:13px;box-shadow:0 2px 8px rgba(0,0,0,.15);z-index:99}
 .toast.hidden{display:none}
 .meta{color:#666;white-space:nowrap}
 .empty{color:#666;padding:24px;text-align:center;font-size:14px}
 .rvform{margin:0}
 .rvform select{padding:4px 6px;font-size:12px;border:1px solid #bbb;border-radius:4px;max-width:150px}
 .foot{color:#999;font-size:11px;margin-top:10px}
 .top-nav{display:flex;flex-wrap:wrap;gap:6px;align-items:center;margin:0 0 18px;padding:0 0 12px;border-bottom:1px solid #d0d7de}
 .top-link{display:inline-block;padding:7px 14px;font-size:13px;font-weight:600;color:#3c4043;background:#fff;border:1px solid #c6c9cf;border-radius:999px;text-decoration:none;white-space:nowrap}
 .top-link:hover{border-color:#9aa0a6;background:#f1f3f4;color:#202124}
 .top-link[aria-current=page]{background:#1a73e8;border-color:#1565c0;color:#fff}
 .top-link:focus-visible{outline:2px solid #1a73e8;outline-offset:2px}
 @media (max-width:500px){.top-nav{gap:6px}.top-link{white-space:normal;text-align:center;flex:1 1 auto}}
"""


def _nav_html(current):
    """Shared top navigation (pill/tab style matching the queue theme).

    Renders the two dashboard links with the active page marked via
    aria-current=page. The .top-nav/.top-link rules live in _SHARED_CSS, so the
    two links are visibly spaced (flex + gap) instead of running together.
    """
    def link(href, label, active):
        attr = ' aria-current="page"' if active else ""
        return '<a class="top-link" href="%s"%s>%s</a>' % (href, attr, label)
    return (
        '<nav class="top-nav" aria-label="Dashboard pages">'
        + link("/queue", "Review Queue", current == "queue")
        + link("/closed", "Closed Ticket Housekeeping", current == "closed")
        + "</nav>"
    )


QUEUE_HTML = """\
<!doctype html><html lang=en><head><meta charset=utf-8>
<meta name=viewport content="width=device-width,initial-scale=1">
<title>Freshdesk Review Queue</title>
<style>{{ shared_css|safe }}</style></head><body>
{{ nav|safe }}
<h1>Freshdesk Review Queue</h1>

<div class=sub>{% if offline %}<strong>OFFLINE MODE</strong> — using mock/offline fixture data. No network access.{% else %}Live mode — read-only ticket list.{% endif %}
{% if cache_age is not none %} · cache {{ cache_age }}s old{% endif %}</div>

{% if flash %}
<div class="banner {{ 'ok' if flash[0] == 'ok' else 'err' }}" role=status>{{ flash[1] }}</div>
{% endif %}
{% if error %}
<div class="banner err" role=alert>{{ error }}</div>
{% endif %}

<form class="controls" method=get action=/queue novalidate>
  <div class="panel-region region-time">
    <span class="days-field field"><span class=lbl>Tickets updated in the last</span>
      <input type=number name=days min=1 max=365 value={{ config.days }} aria-label="Days back">
      <span class=lbl>days</span>
    </span>
    <div class=preset-group role=group aria-label="Quick time presets">
      {% for d in [7, 14, 30, 60, 90] %}<a class=preset href="/queue?{{ preset_urls[d] }}" {% if config.days == d %}aria-current=page{% endif %}>{% if config.days == d %}<span class=preset-mark aria-hidden=true>&#10003;</span> {% endif %}{{ d }}d</a>{% endfor %}
    </div>
  </div>
  <div class="panel-region region-groups">
    <fieldset class=filter-group>
      <legend class=group-lbl>Ticket conditions</legend>
      <div class=field><label for=filter-overdue><input type=checkbox id=filter-overdue name=overdue value=1 {{ 'checked' if config.overdue }}> Overdue</label></div>
      <p class=field-hint>Works together with the selected status.</p>
    </fieldset>
    <fieldset class=filter-group>
      <legend class=group-lbl>Freshdesk status</legend>
      <div class=field><label for=filter-responded><input type=checkbox id=filter-responded name=responded value=1 {{ 'checked' if config.responded }}> Customer Responded</label></div>
      <div class=field><label for=filter-waiting><input type=checkbox id=filter-waiting name=waiting value=1 {{ 'checked' if config.waiting }}> Waiting on Customer</label></div>
      <p class=field-hint>Select one or both statuses.</p>
    </fieldset>
    <fieldset class=filter-group>
      <legend class=group-lbl>Additional filters</legend>
      <div class=field><label for=filter-missing><input type=checkbox id=filter-missing name=missing_tags value=1 {{ 'checked' if config.missing_tags }}> Missing Tags</label></div>
    </fieldset>
  </div>
  <div class="panel-region region-actions">
    <span class=view-field><label for=review_view>Review view</label>
      <select id=review_view name=review_view>
        {% for v in ['active','completed','all'] %}<option value={{ v }} {{ 'selected' if config.review_view == v }}>{% if v == 'active' %}Active{% elif v == 'completed' %}Completed{% else %}All{% endif %}</option>{% endfor %}
      </select></span>
    <div class=action-buttons>
      <button type=submit class=apply>Apply Filters</button>
      <a class=reset href="/queue?overdue=1&amp;responded=0&amp;waiting=0&amp;missing_tags=1&amp;days=60&amp;review_view=active" role=button aria-label="Reset filters to defaults">Reset to Defaults</a>
    </div>
  </div>
</form>
<p class=filter-summary role=status>{{ active_summary }}</p>

<p class=count>{{ total }} tickets matching your filters</p>
{% if last_opened_id is not none %}
  {% if last_opened_rendered %}
<p class=last-opened-bar><button type=button id=last-opened-jump aria-controls=queue-table>Jump to Last Opened</button></p>
  {% else %}
<div class="banner" id=last-opened-hidden role=status>Last opened ticket is hidden by the current filters.</div>
  {% endif %}
{% endif %}
{% if all_categories_off %}
<div class=empty>Select Overdue or at least one status to display results.</div>
{% elif tickets %}
<div class=tablewrap>
<table id=queue-table>
<caption class=visually-hidden>Freshdesk review queue</caption>
<tr>
  <th scope=col>Ticket</th><th scope=col>Subject</th><th scope=col>Status</th>
  <th scope=col>Badges</th><th scope=col>Review</th><th scope=col>Priority</th>
  <th scope=col>Due / SLA</th><th scope=col>Updated</th><th scope=col>Created</th>
  <th scope=col>Tags</th><th scope=col>Type</th>
</tr>
{% for t in tickets %}
<tr class="{{ t.row_class }}" data-ticket-id="{{ t.id }}">
  <td><a class="tid fd-link" href="{{ t.url }}" target=_blank rel="noopener noreferrer" data-ticket-id="{{ t.id }}" aria-label="Open ticket #{{ t.id }} in Freshdesk (new tab)">#{{ t.id }}</a></td>
  <td><a class="sbj fd-link" href="{{ t.url }}" target=_blank rel="noopener noreferrer" data-ticket-id="{{ t.id }}" aria-label="Open subject of ticket #{{ t.id }} in Freshdesk (new tab)">{{ t.subject }}</a></td>
  <td>{{ t.status_label }}</td>
  <td><div class=badges>{% if t.last_opened %}<span class="badge b-last-opened">LAST OPENED</span>{% endif %}{% for kind, text, cls in t.badges %}<span class="badge {{ cls }}">{{ text }}</span>{% endfor %}</div></td>
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
// The Last Opened focus marker is derived server-side from the newest valid
// last_opened_at (spec section 3/6) and reported back by the opened endpoint
// as last_opened_id. On a *confirmed* save we move the marker in the DOM
// without reloading: strip the marker class/badge from every row (idempotent,
// so repeat clicks never duplicate the badge), then apply it to the target row
// (if rendered) and keep the jump control / hidden-message in sync.
function moveLastOpened(newId) {
  if (typeof newId === 'undefined' || newId === null) { return; }
  ensureLastOpenedChrome();
  document.querySelectorAll('.b-last-opened').forEach(function (b) {
    b.parentNode.removeChild(b);
  });
  document.querySelectorAll('tr.rv-last-opened').forEach(function (r) {
    r.classList.remove('rv-last-opened');
  });
  var target = document.querySelector('tr[data-ticket-id="' + newId + '"]');
  var bar = document.querySelector('.last-opened-bar');
  var hidden = document.getElementById('last-opened-hidden');
  if (target) {
    target.classList.add('rv-last-opened');
    var badges = target.querySelector('.badges');
    if (badges) {
      var span = document.createElement('span');
      span.className = 'badge b-last-opened';
      span.textContent = 'LAST OPENED';
      badges.appendChild(span);
    }
    if (bar) { bar.style.display = ''; }
    if (hidden) { hidden.style.display = 'none'; }
  } else {
    if (bar) { bar.style.display = 'none'; }
    if (hidden) { hidden.style.display = ''; }
  }
}
// Jump to Last Opened: smooth-scroll to the marked row and set temporary
// keyboard focus. Never opens the Freshdesk link and never makes a network
// request (spec section 5).
function jumpToLastOpened() {
  var row = document.querySelector('tr.rv-last-opened');
  if (!row) { return; }
  row.scrollIntoView({behavior: 'smooth', block: 'center'});
  row.setAttribute('tabindex', '-1');
  row.focus({preventScroll: true});
  setTimeout(function () { row.removeAttribute('tabindex'); }, 1500);
}
// The jump bar and the hidden-by-filters notice are server-rendered only when
// a last-opened marker already existed at page load. When the *first* click
// creates the marker client-side, build the same chrome here so the jump
// control appears without a reload.
function ensureLastOpenedChrome() {
  if (!document.querySelector('.last-opened-bar')) {
    var bar = document.createElement('p');
    bar.className = 'last-opened-bar';
    var btn = document.createElement('button');
    btn.type = 'button';
    btn.id = 'last-opened-jump';
    btn.setAttribute('aria-controls', 'queue-table');
    btn.textContent = 'Jump to Last Opened';
    bar.appendChild(btn);
    var anchor = document.querySelector('.tablewrap') || document.body;
    anchor.parentNode.insertBefore(bar, anchor);
  }
  if (!document.getElementById('last-opened-hidden')) {
    var div = document.createElement('div');
    div.className = 'banner';
    div.id = 'last-opened-hidden';
    div.setAttribute('role', 'status');
    div.textContent = 'Last opened ticket is hidden by the current filters.';
    var divAnchor = document.querySelector('.tablewrap') || document.body;
    divAnchor.parentNode.insertBefore(div, divAnchor);
  }
}
// Delegated listener: works whether the button was server-rendered at load or
// created later by ensureLastOpenedChrome() after the first marker click.
document.addEventListener('click', function (e) {
  if (e.target && e.target.id === 'last-opened-jump') { jumpToLastOpened(); }
});
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
        // Marker only moves on a confirmed save (spec: save failure must not
        // falsely move the Last Opened marker).
        moveLastOpened(d.last_opened_id);
      } else {
        showError('Could not save Opened / In Review state for #' + tid + ' (not saved).');
      }
    }).catch(function () {
      showError('Could not save Opened / In Review state for #' + tid + ' (not saved).');
    });
  });
});
// Filter controls: canonicalize on submit (Prompt05). Native HTML checkbox
// GET forms omit *unchecked* fields entirely, so turning OFF a default-ON
// category (Overdue, Missing Tags) submitted no parameter and the backend's
// documented default re-checked it. On submit we prevent default navigation
// and rebuild one canonical query string from the live control state so every
// parameter (overdue, responded, waiting, missing_tags, days, review_view)
// appears exactly once with an explicit 0/1 / validated value. This fires for
// mouse click, keyboard activation, and Enter in the days field alike.
(function () {
  var form = document.querySelector('form.controls');
  if (!form) { return; }
  function normDays(raw) {
    var v = String(raw == null ? '' : raw).trim();
    if (!/^[0-9]+$/.test(v)) { return 60; }
    var n = parseInt(v, 10);
    return (n >= 1 && n <= 365) ? n : 60;
  }
  function normView(v) { return (v === 'completed' || v === 'all') ? v : 'active'; }
  form.addEventListener('submit', function (e) {
    try {
      var params = {};
      ['overdue', 'responded', 'waiting', 'missing_tags'].forEach(function (n) {
        var el = form.querySelector('input[name="' + n + '"]');
        params[n] = el && el.checked ? '1' : '0';
      });
      var daysEl = form.querySelector('input[name=days]');
      params['days'] = String(normDays(daysEl ? daysEl.value : null));
      var viewEl = form.querySelector('select[name=review_view]');
      params['review_view'] = normView(viewEl ? viewEl.value : 'active');
      var parts = [];
      ['overdue', 'responded', 'waiting', 'missing_tags', 'days', 'review_view'].forEach(function (k) {
        parts.push(encodeURIComponent(k) + '=' + encodeURIComponent(params[k]));
      });
      e.preventDefault();
      window.location.href = '/queue?' + parts.join('&');
    } catch (err) {
      // On any unexpected error, fall back to native submission.
    }
  });
  // Browser back/forward (and bfcache restore) may re-apply stale values to
  // the controls even though the server re-rendered from the current URL
  // (e.g. the days box showing its previous typed value while the URL says
  // 60). Re-derive every control from the canonical URL on every pageshow so
  // the rendered controls always reflect the address-bar state.
  function syncControlsFromURL() {
    var q = new URLSearchParams(window.location.search);
    ['overdue', 'responded', 'waiting', 'missing_tags'].forEach(function (n) {
      var el = form.querySelector('input[name="' + n + '"]');
      if (el) { el.checked = (q.get(n) === '1'); }
    });
    var daysEl = form.querySelector('input[name=days]');
    if (daysEl) { daysEl.value = String(normDays(q.get('days'))); }
    var viewEl = form.querySelector('select[name=review_view]');
    if (viewEl) { viewEl.value = normView(q.get('review_view')); }
  }
  window.addEventListener('pageshow', function () { syncControlsFromURL(); });
})();
setTimeout(function(){ location.reload(); }, 300000); // auto-refresh every 5 min
</script>
</body></html>
"""


CLOSED_HTML = """\
<!doctype html><html lang=en><head><meta charset=utf-8>
<meta name=viewport content="width=device-width,initial-scale=1">
<title>Closed Ticket Housekeeping</title>
<style>{{ shared_css|safe }}
 .complete{color:#176b35;font-weight:700}
 .incomplete{color:#8a1f1f;background:#fdecea;border:1px solid #d66;border-radius:6px;padding:8px 12px;font-size:13px;margin:8px 0}
 .closed-empty{color:#666;padding:24px 0;text-align:center;font-size:14px}
</style></head><body>
{{ nav|safe }}
<h1>Closed Ticket Housekeeping</h1>
<p class=sub>Find closed tickets that may need housekeeping, such as tickets with no tags.</p>
<div class="banner" role=status><strong>OFFLINE MODE — Synthetic fixture data only</strong></div>
<form class="controls" method=get action=/closed novalidate>
  <div class="panel-region region-time">
    <span class="days-field field"><span class=lbl>Closed in the last</span>
      <input type=number name=days min=1 max=3650 value="{{ config.days }}" aria-label="Closed in last days">
      <span class=lbl>days</span>
    </span>
    <div class=preset-group role=group aria-label="Closed date presets">
      {% for d in [30, 60, 90, 180, 365] %}<a class=preset href="/closed?days={{d}}&amp;missing_tags={{ 1 if config.missing_tags else 0 }}" {% if config.days == d %}aria-current=page{% endif %}>{{d}}d</a>{% endfor %}
    </div>
  </div>
  <div class="panel-region region-groups">
    <fieldset class=filter-group>
      <legend class=group-lbl>Tags</legend>
      <div class=field><label for=closed-missing><input type=hidden name=missing_tags value=0><input type=checkbox id=closed-missing name=missing_tags value=1 {{ 'checked' if config.missing_tags }}> Missing Tags Only</label></div>
      <p class=field-hint>Show only closed tickets that have no tags.</p>
    </fieldset>
  </div>
  <div class="panel-region region-actions">
    <div class=action-buttons>
      <button type=submit class=apply>Apply Filters</button>
      <a class=reset href="/closed?days=60&amp;missing_tags=1" role=button aria-label="Reset to defaults">Reset to Defaults</a>
    </div>
  </div>
</form>
{% if error %}<div class="banner err" role=alert>{{ error }}</div>{% elif result %}
<p class="filter-summary" role=status>{{ result.unique_ticket_count }} unique closed tickets found · {% if result.missing_tags_only %}Missing Tags Only{% else %}All tag states{% endif %} · {{ result.date_range[0] }} to {{ result.date_range[1] }} · {{ result.windows_planned|length }} date windows · {{ result.pages_requested|length }} pages · {% if result.complete %}<span class=complete>Complete</span>{% else %}<span class=incomplete>Results incomplete</span>{% endif %}</p>
{% for text in result.warnings %}<div class=incomplete role=status>{{ text }}</div>{% endfor %}{% for text in result.errors %}<div class=incomplete role=alert>{{ text }}</div>{% endfor %}
{% if result.tickets %}<div class=tablewrap><table id=closed-table><caption>Closed ticket search results</caption><thead><tr><th scope=col>Ticket ID</th><th scope=col>Subject</th><th scope=col>Status</th><th scope=col>Closed date</th><th scope=col>Current tags</th><th scope=col>Housekeeping</th><th scope=col>Freshdesk ticket</th></tr></thead><tbody>{% for t in result.tickets %}<tr><td><a class=tid href="https://broadriverretail-help.freshdesk.com/a/tickets/{{t.id}}" target=_blank rel="noopener noreferrer" aria-label="Open ticket #{{t.id}} in Freshdesk (new tab)">#{{t.id}}</a></td><td><a class=sbj href="https://broadriverretail-help.freshdesk.com/a/tickets/{{t.id}}" target=_blank rel="noopener noreferrer" aria-label="Open subject of ticket #{{t.id}} in Freshdesk (new tab)">{{t.subject}}</a></td><td>{% if t.status %}<span class="badge b-closed">{{t.status}}</span>{% else %}<span class="badge b-closed">Closed</span>{% endif %}</td><td>{{t.closed_at}}</td><td>{% if t.tags %}{{t.tags|join(', ')}}{% else %}<span class=meta>No tags</span>{% endif %}</td><td>{% if not t.tags %}<span class="badge b-missing">Missing Tags</span>{% else %}<span class=meta>—</span>{% endif %}</td><td><a href="https://broadriverretail-help.freshdesk.com/a/tickets/{{t.id}}" target=_blank rel="noopener noreferrer">Open ticket</a></td></tr>{% endfor %}</tbody></table></div>{% else %}<p class=closed-empty>No matching closed tickets were found.</p>{% endif %}
{% endif %}</body></html>
"""


def _closed_render(**kwargs):
    return render_template_string(
        CLOSED_HTML, offline=True, shared_css=_SHARED_CSS,
        nav=_nav_html("closed"), **kwargs)


def _queue_render(**kwargs):
    """Render QUEUE_HTML with the shared context merged in."""
    ctx = dict(kwargs)
    cfg = ctx.get("config") or dict(DEFAULT_FILTERS)
    ctx.setdefault("config", cfg)
    ctx.setdefault("shared_css", _SHARED_CSS)
    ctx.setdefault("nav", _nav_html("queue"))
    ctx.setdefault("csrf_token", get_csrf_token())
    ctx.setdefault("flash", None)
    token = ctx["csrf_token"]
    ctx.setdefault("csrf_token_json", json.dumps(token))
    ctx.setdefault("review_states", REVIEW_STATES)
    ctx.setdefault("preset_urls", {d: filter_query_string(dict(cfg, days=d)) for d in (7, 14, 30, 60, 90)})
    ctx.setdefault("active_summary", filter_summary_text(cfg))
    return render_template_string(QUEUE_HTML, **ctx)


if __name__ == "__main__":
    host = os.environ.get("HOST", "127.0.0.1")
    port = int(os.environ.get("PORT", "5050"))
    app.run(host=resolve_bind_host(host), port=port, debug=False)
