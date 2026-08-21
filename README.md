# Freshdesk Review Queue Scanner

Read-only Freshdesk triage page that surfaces tickets needing attention: customer-responded tickets with photo/video keywords in the subject, waiting-on-customer tickets, and overdue items.

## Milestone status

This branch is a safe local-development milestone. It supports an explicit offline mode backed by synthetic fixtures. Offline mode never reads the API key and never makes HTTP requests.

## Local review-state backup protection

The SQLite review database is automatically protected with verified, SQLite-consistent backups in `~/FreshdeskScannerBackups/review_state` by default. Set `REVIEW_BACKUP_DIR` to override the location and `REVIEW_BACKUP_KEEP` to configure retention (default: 200 automatic generations; malformed values use the safe default). A startup baseline and successful queue review, acknowledge-update, automatic review advancement, opened-state, and closed-review mutation create backups. Each backup uses SQLite's backup API, integrity verification, SHA-256 metadata, a temporary file, and atomic finalization. Manual/recovery files are never pruned; only the feature's automatic filename pattern is rotated. If backup work fails, the local database mutation remains committed and the failure is logged (with a concise warning where practical). Tests always use temporary `REVIEW_DB_PATH` and `REVIEW_BACKUP_DIR`; backup protection makes no Freshdesk requests.

## What It Does

- Retrieves Freshdesk tickets only when **Refresh Tickets** is explicitly clicked
- Lets the operator choose the live retrieval window (1-365 rolling days); **Days** controls Freshdesk retrieval only, it never re-filters the already-cached rows
- Reconciles each complete successful Days-based retrieval into the existing queue cache rather than replacing it, preserving cached tickets omitted from the requested window; persistent cursor-based incremental retrieval and retention pruning are not active yet
- Uses only `GET /api/v2/tickets` for queue retrieval, plus targeted conversation `GET /api/v2/tickets/{id}/conversations` checks only for locally reviewed tickets whose `updated_at` became newer than their review snapshot
- Queue cache uses a single atomic JSON envelope (schema version 2) containing tickets and refresh metadata together. Schema-less legacy queue caches remain readable without being rewritten.
- Version-2 queue-cache metadata records canonical UTC refresh start/finish timestamps, the requested Days value, and the future rolling-retention setting (60 days). `fetched_at` remains a compatible completion-time Unix timestamp for the existing freshness display.
- Phase 3A does **not** enable incremental retrieval, merge behavior, or retention pruning: each successful Refresh Tickets operation still replaces the queue cache.
- Paces ticket and conversation requests through the same conservative rate limiter and reports finite refresh progress
- Conversation-aware **UPDATED SINCE REVIEW** handling suppresses only explained private-agent-note-only activity; customer activity, public agent replies, ambiguous metadata, meaningful ticket-field changes, unexplained timestamp tails, lookup failures, and incomplete pagination fail safe and remain flagged
- **Acknowledge Update** is a local-only action for meaningful reviewed updates. It advances the local snapshot to the current cached `updated_at`, preserves the existing reviewer badge, makes no Freshdesk request or write, and preserves the current queue filters
- No conversation lookup is performed for unreviewed, unchanged, or older reviewed tickets


- Keeps **Apply Filters** completely local to the current cache
- **Default Review Scope** (visible in the UI, defaults ON): the default queue shows photo/video subjects only, and hides tickets carrying any reviewed/closed Freshdesk tag (Parts needed, Exchange, No Service Needed, Closed, Schedule Service, Delivery special needed)
- Photo/video matching is case-insensitive and word-boundary aware on the ticket **subject** only (Photo, Photos, Picture, Pictures, Pic, Pics, Video, Videos, Vid, Vids)
- Tag comparison for the reviewed/closed exclusions is case-insensitive and leading/trailing-whitespace-insensitive for comparison only; stored Freshdesk tags are never modified
- Optional manual local filters stay independent and remain opt-in: Overdue, Customer Responded, Waiting on Customer, and Missing Tags (all default OFF)
- The queue is organized by local workflow tabs: Main Queue, Supervisor Review, Follow-Up, Resolved, and No Action. The `Needs Supervisor Review` state is local-only; workflow tabs and review actions never call Freshdesk. Actual Freshdesk `Closed` tickets are excluded from normal workflow tabs and appear only through the explicit Show All Cached Tickets view.
- `workflow_tab` is the canonical bookmarkable queue URL parameter; older `review_view` links remain accepted for compatibility. Changing tabs, filters, presets, or review state is local-only; only Refresh Tickets retrieves data.
- **Reset to Default Review Scope** restores the scoped default queue; **Show All Cached Tickets** turns the scope controls and every manual filter off to display the complete current cache (deduped only) — both are local-only and never contact Freshdesk
- A successful data refresh reloads the queue back into the default Review Scope view
- Has no Freshdesk ticket mutation actions
- Binds to `127.0.0.1` by default and refuses `0.0.0.0`

## Safe offline development

```bash
python3.11 -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade pip
pip install -r requirements.txt

FRESHDESK_OFFLINE=1 flask --app app run --host 127.0.0.1 --port 5050
```

Open http://127.0.0.1:5050/queue.

The page displays an `OFFLINE MODE` banner and synthetic fixture data. If fixtures are missing or malformed, the app fails closed with an error; it does not fall back to Freshdesk.

The fixture file is `fixtures/fixtures.json`. It contains fake data only and must not be replaced with production data.

## Closed Ticket Housekeeping

`/closed` is a separate Closed-ticket housekeeping dashboard. It supports safe offline fixtures and an explicit, read-only live refresh into its own local cache. Open `http://127.0.0.1:5050/closed`.

- Defaults: last 60 days and Missing Tags Only enabled (`/closed?days=60&missing_tags=1`).
- Opening or reloading `/closed` reads `cache/closed_tickets.json`; it does not automatically contact Freshdesk.
- Only **Refresh from Freshdesk** starts live retrieval. The retrieval is GET-only, uses 100-ticket sequential pagination, deduplicates ticket IDs, applies conservative rate limiting, and replaces the cache only after a complete successful run.
- The Closed cache is separate from the queue cache at `cache/tickets.json`.
- Local review actions remain local; there is no Freshdesk tag editing or other write operation.

Operational details and safe recovery are in `docs/CLOSED_DASHBOARD_RUNBOOK.md`. The historical public API contract used by the original foundation is recorded in `docs/closed_housekeeping_api_contract.md`.

## Tests and validation

```bash
.venv/bin/python -m pytest
bash validate.sh
```

The test suite blocks all `requests` HTTP methods and verifies that offline `/queue` rendering succeeds without a network request or API key.

## Live mode (not used by this milestone)

The eventual live read-only run requires an API key outside this repository:

```bash
mkdir -p ~/.config/furtouch
chmod 700 ~/.config/furtouch
# Put the key in ~/.config/furtouch/freshdesk_api_key, then:
chmod 600 ~/.config/furtouch/freshdesk_api_key
flask --app app run --host 127.0.0.1 --port 5050
```

The live code reads the key only when offline mode is not enabled. It only calls the Freshdesk list endpoint with pagination. No live account test is part of this milestone.

## Project files

- `app.py` — scanner-only Flask app and filtering/cache logic
- `fixtures/fixtures.json` — synthetic multi-page offline fixture data
- `tests/test_app.py` — offline, filtering, pagination, cache, rendering, and safety tests
- `conftest.py` — autouse external-network blocker
- `validate.sh` — Python, import, route, offline, safety, test, and Git tracking checks
- `PROJECT.md` — project context and preserved business rules
- `SCANNER_PLAN.md` — original design notes
- `SCANNER_PLAN_REVIEW.md` — prior review notes

## Security constraints

- API key remains outside the repository at `~/.config/furtouch/freshdesk_api_key`
- No `.env`, key, source export, or cache files belong in Git
- `cache/` is ignored
- Use `127.0.0.1`; do not expose Flask externally
- No Freshdesk data mutations
- Do not run the app in live mode until a separately approved, controlled read-only test procedure exists

## Known open business decisions

See the final milestone report for recommendations on waiting-ticket visibility, refresh/cache semantics, subject-only scope, malformed `due_by`, and the first live read-only test.
