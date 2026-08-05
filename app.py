"""Freshdesk Review Queue Scanner — read-only Flask triage page.

Single route: /queue.

Data sources:
  live    GET /api/v2/tickets on the Freshdesk account (read-only, list endpoint only)
  offline FRESHDESK_OFFLINE=1  -> local fixture pages, no network, no API key

Offline mode is fail-closed: it never calls the network and never reads the API
key file. If the fixture data is missing or malformed, /queue renders an error
page instead of falling back to live access.

The API key is never loaded at import time. Only load_api_key() touches the key
file, and only the live data path calls it.
"""
import json
import os
import re
from datetime import datetime, timedelta, timezone

import requests
from flask import Flask, render_template_string, request

app = Flask(__name__, static_folder=None)


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
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "fixtures", "fixtures.json"),
)

CACHE_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "cache")
os.makedirs(CACHE_DIR, exist_ok=True)
CACHE_FILE = os.path.join(CACHE_DIR, "queue_cache.json")
CACHE_TTL_SECONDS = 1800  # 30 minutes

# Only scan tickets updated in the last N days to avoid pulling all 8488.
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


def paginate_tickets():
    """Fetch all tickets across pages from the list endpoint. Live mode only."""
    page = 1
    per_page = 100
    since = (datetime.now(timezone.utc) - timedelta(days=UPDATED_SINCE_DAYS)).isoformat()
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


def passes_filters(t):
    status = t.get("status")
    if status not in SCAN_STATUSES:
        return False
    subject = t.get("subject") or ""
    if not KEYWORD_RE.search(subject):
        return False
    # Untagged check: match extension behavior — only flag tickets with missing/empty tags.
    tags = t.get("tags") or []
    if tags:
        return False
    # Overdue check for customer-responded tickets (status 2) using resolution deadline only.
    if status == 2:
        due = t.get("due_by")
        if due:
            try:
                dt = datetime.fromisoformat(due.replace("Z", "+00:00"))
                if dt >= datetime.now(timezone.utc):
                    return False  # not yet overdue
            except Exception:
                pass
    return True


def get_ticket_pool():
    """Return (filtered_tickets, cache_age_seconds) using the 30-min cache.

    Live mode fetches from Freshdesk; offline mode fetches from fixtures. The
    cache stores already-filtered tickets (existing behavior). Cache file
    corruption or read errors fall through to a fresh fetch.
    """
    now_ts = datetime.now(timezone.utc).timestamp()
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
    raw = [t for t in raw if passes_filters(t)]
    with open(CACHE_FILE, "w") as fh:
        json.dump({"fetched_at": now_ts, "tickets": raw}, fh)
    return raw, 0


QUEUE_HTML = """\
<!doctype html><html><head><meta charset=utf-8>
<meta name=viewport content="width=device-width,initial-scale=1">
<title>Freshdesk Review Queue</title>
<style>
 body{font-family:system-ui,Arial,sans-serif;max-width:960px;margin:auto;padding:16px;background:#f5f5f5;color:#222}
 h1{font-size:22px;margin:0 0 4px}
 .sub{color:#666;font-size:13px;margin-bottom:16px}
 .controls{margin-bottom:12px;display:flex;gap:8px;flex-wrap:wrap;align-items:center}
 .controls label{font-size:13px}
 .controls select,.controls button{font-size:13px;padding:5px 10px}
 table{width:100%;border-collapse:collapse;background:#fff;border-radius:8px;overflow:hidden;box-shadow:0 1px 3px rgba(0,0,0,0.08)}
 th,td{padding:9px 12px;border-bottom:1px solid #eee;text-align:left;font-size:14px}
 th{background:#fafafa;font-weight:600;font-size:12px;text-transform:uppercase;letter-spacing:0.03em;color:#555}
 tr:hover td{background:#f9f6f0}
 a.tid{color:#1a73e8;text-decoration:none;font-weight:600} a.tid:hover{text-decoration:underline}
 .badge{display:inline-block;padding:2px 8px;border-radius:12px;font-size:11px;font-weight:700;text-transform:uppercase}
 .badge-overdue{background:#fde8e8;color:#c00}
 .badge-ok{background:#e8f5e9;color:#2e7d32}
 .badge-waiting{background:#fff3e0;color:#e65100}
 .empty{background:#fff;border-radius:8px;padding:24px;text-align:center;color:#888;box-shadow:0 1px 3px rgba(0,0,0,0.06)}
 .error{background:#fde8e8;color:#c00;border-radius:8px;padding:16px;margin-bottom:12px}
 .offline-banner{background:#fff8e1;border:1px solid #f0c36d;color:#7a5c00;border-radius:8px;padding:10px 12px;margin-bottom:12px;font-weight:600}
 .refresh{float:right;font-size:12px;color:#666}
 .meta{font-size:12px;color:#888}
</style></head><body>
{% if offline %}
<div class=offline-banner>OFFLINE MODE — showing mock/offline fixture data. No Freshdesk connection is made.</div>
{% endif %}
<h1>Freshdesk Review Queue</h1>
<div class=sub>{{ total }} ticket{{ '' if total==1 else 's' }} matching your filters · <span class=refresh>{% if cache_age %}cached {{ cache_age }}s ago · {% endif %}<a href=/queue style=color:#666>Refresh</a></span></div>

{% if error %}
<div class=error>{{ error }}</div>
{% endif %}

{% if tickets %}
<table>
<tr>
  <th>Ticket</th>
  <th>Subject</th>
  <th>Status</th>
  <th>Priority</th>
  <th>Due</th>
  <th>Created</th>
  <th>Tags</th>
  <th>Type</th>
</tr>
{% for t in tickets %}
<tr>
  <td><a class=tid href="{{ t.url }}" target=_blank rel=noopener>#{{ t.id }}</a></td>
  <td>{{ t.subject }}</td>
  <td>{{ t.status_label }}</td>
  <td>{{ t.priority_label }}</td>
  <td class=meta>{{ t.due_display | safe }}</td>
  <td class=meta>{{ t.created_display }}</td>
  <td>{% if t.tags %}{{ t.tags|join(', ') }}{% else %}<em style=color:#bbb>none</em>{% endif %}</td>
  <td>{{ t.type or '—' }}</td>
</tr>
{% endfor %}
</table>
{% else %}
<div class=empty>No tickets match the current filter.</div>
{% endif %}

<script>
setTimeout(function(){ location.reload(); }, 300000); // auto-refresh every 5 min
</script>
</body></html>
"""


def ticket_url(ticket_id):
    return f"https://{FRESHDESK_DOMAIN}/a/tickets/{ticket_id}"


def fmt_due(due_str):
    if not due_str:
        return "—"
    try:
        dt = datetime.fromisoformat(due_str.replace("Z", "+00:00"))
        now = datetime.now(timezone.utc)
        delta = dt - now
        days = int(delta.total_seconds() // 86400)
        hours = int((delta.total_seconds() % 86400) // 3600)
        if delta.total_seconds() < 0:
            return f"<span style='color:red;font-weight:bold'>{abs(days)}d {abs(hours)}h OVERDUE</span>"
        return f"{days}d {hours}h left"
    except Exception:
        return due_str


STATUS_LABELS = {2: "Customer responded", 3: "Pending", 4: "Resolved", 5: "Closed", 6: "Waiting on customer", 1: "Open"}
PRIORITY_LABELS = {1: "Low", 2: "Medium", 3: "High", 4: "Urgent"}


@app.route("/queue")
def queue():
    # Read for URL compatibility. Overdue filtering happens at fetch time
    # (existing behavior: non-overdue status-2 tickets are excluded upstream).
    show_overdue = request.args.get("overdue", "1") != "0"
    include_waiting = request.args.get("waiting", "0") == "1"
    offline = is_offline()

    # Missing-key warning so the user notices before a blank page. Skipped in
    # offline mode — offline mode works without a key and never reads it.
    if not offline and not load_api_key():
        return render_template_string(
            QUEUE_HTML, tickets=[], total=0, offline=offline, cache_age=None,
            error="No Freshdesk API key found. Set FRESHDESK_API_KEY env var or write it to "
                  "~/.config/furtouch/freshdesk_api_key (chmod 600).",
        )

    try:
        raw, cache_age = get_ticket_pool()
    except OfflineDataError as e:
        # Fail closed: offline data problems never fall back to live access.
        return render_template_string(
            QUEUE_HTML, tickets=[], total=0, offline=offline, cache_age=None, error=str(e),
        )
    except requests.exceptions.HTTPError as e:
        resp = getattr(e, "response", None)
        detail = resp.status_code if resp is not None else str(e)
        return render_template_string(
            QUEUE_HTML, tickets=[], total=0, offline=offline, cache_age=None,
            error=f"Freshdesk API error: {detail} — check your API key and permissions.",
        )
    except Exception as e:
        return render_template_string(
            QUEUE_HTML, tickets=[], total=0, offline=offline, cache_age=None,
            error=f"Error fetching tickets: {e}",
        )

    # apply waiting toggle at render time so no extra API calls
    if not include_waiting:
        raw = [t for t in raw if t.get("status") != 6]

    tickets_out = []
    for t in raw:
        sid = t.get("status")
        pid = t.get("priority", 0)
        created = t.get("created_at", "")
        due = t.get("due_by") or t.get("fr_due_by")
        tags = t.get("tags") or []

        # Derive a clean due/overdue display.
        due_display = fmt_due(due)

        tickets_out.append({
            "id": t["id"],
            "url": ticket_url(t["id"]),
            "subject": t.get("subject", ""),
            "status_label": STATUS_LABELS.get(sid, f"Status {sid}"),
            "priority_label": PRIORITY_LABELS.get(pid, f"P{pid}"),
            "due_display": due_display,
            "created_display": created[:10] if created else "—",
            "tags": tags if tags else [],
            "type": t.get("type"),
        })

    return render_template_string(
        QUEUE_HTML, tickets=tickets_out, total=len(tickets_out), error=None,
        offline=offline, cache_age=cache_age,
    )


def resolve_bind_host(host):
    """Refuse unsafe external binds. The scanner must stay on the loopback
    interface. Raises SystemExit for 0.0.0.0 so misuse is loud and immediate.
    """
    if host == "0.0.0.0":
        raise SystemExit(
            "Refusing to bind to 0.0.0.0. Set HOST=127.0.0.1 or export PORT=5050."
        )
    return host


if __name__ == "__main__":
    host = os.environ.get("HOST", "127.0.0.1")
    port = int(os.environ.get("PORT", "5050"))
    app.run(host=resolve_bind_host(host), port=port, debug=False)
