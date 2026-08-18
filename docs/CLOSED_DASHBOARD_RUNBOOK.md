# Closed Dashboard Runbook

## Known-good state

- Prompt 24 implementation branch: `dev/closed-live-dashboard`
- Prompt 24 implementation commit: `3002f83f894940485ec4807bf18ebe976c4b143a`

Prompt 25 redo creates a child documentation/readiness commit from that verified
implementation commit. The Prompt 24 and Prompt 25 hashes are expected to
differ: this is normal Git lineage, not a contradiction.

## Normal startup

```bash
cd /Users/joshua/Projects/freshdesk-scanner
source .venv/bin/activate
flask --app app run --host 127.0.0.1 --port 5050
```

Open @url:`http://127.0.0.1:5050/closed`.

## Offline mode

Start with `FRESHDESK_OFFLINE=1` when using fixtures only:

```bash
FRESHDESK_OFFLINE=1 flask --app app run --host 127.0.0.1 --port 5050
```

Offline mode uses local synthetic fixtures, never reads the Freshdesk API key,
and never makes an HTTP request to Freshdesk. It fails closed if fixtures are
missing or malformed.

## Live cache mode

- Opening or reloading `/closed` reads the local Closed cache; it does not
  automatically call Freshdesk.
- Only **Refresh from Freshdesk** starts live retrieval.
- Freshdesk integration is GET-only.
- The cache is replaced only after a complete, successful refresh.

## Cache locations

Keep these cache namespaces separate:

```text
Queue:  cache/tickets.json
Closed: cache/closed_tickets.json
```

The Closed dashboard must not repurpose the queue cache, and `/queue` must not
use Closed rows.

## Local review database

`data/review_state.sqlite3` stores local review-state actions. Those actions do
not modify Freshdesk. Do not delete this database as a routine recovery step.

## Refresh behavior

An explicit Closed refresh retrieves 100 tickets per page using sequential
pagination and normal/default ordering (no `order_by=status`). It deduplicates
by ticket ID, applies a conservative rate limiter, runs in the background,
reports progress, supports cancellation, and atomically replaces the
last-known-good Closed cache only after a complete successful result.

The validated rate-limit policy is:

```text
remaining > 50       -> at least 2 seconds
20 < remaining <= 50 -> at least 5 seconds
remaining <= 20      -> at least 60 seconds
```

Retries are bounded for HTTP `429`, `5xx`, and network timeout/connection
errors. A cancelled, failed, or incomplete refresh does not overwrite the
existing cache.

## Safe recovery

- **Port 5050 already in use:** stop the other local process using port 5050,
  or choose an unused local port and open the matching URL. Do not expose the
  app on `0.0.0.0`.
- **Flask restarts:** restart with the normal command. The saved Closed cache
  and local review database remain local; opening `/closed` still does not
  auto-refresh.
- **Closed cache missing:** `/closed` shows its safe no-cache state. Do not
  manufacture a cache; use the explicit refresh control only when a live
  retrieval is authorized and credentials are available.
- **Refresh cancelled or failed:** retain and use the last-known-good cache;
  inspect the displayed progress/error and retry later only through the
  explicit refresh control.
- **Freshdesk returns 429:** allow the conservative limiter and bounded retry
  policy to back off. Do not repeatedly click Refresh.
- **Credentials unavailable:** remain in offline mode or use existing cache.
  The credential belongs at `~/.config/furtouch/freshdesk_api_key`, outside
  the repository; do not place it in project files.
- **Coverage stale or too narrow:** use the coverage metadata to decide whether
  an explicitly authorized refresh is needed. Do not treat incomplete coverage
  as complete.
- **`/closed` opens with no rows:** confirm the visible cache/coverage state,
  filters, and whether a cache exists. Do not delete
  `data/review_state.sqlite3`; use the safe no-cache state or an explicitly
  authorized refresh as appropriate.

## Safety guarantees

Freshdesk integration is read-only. There are no Freshdesk
POST/PUT/PATCH/DELETE operations. Local review-state POSTs remain local.
Credentials remain outside the repository.
