# Freshdesk Review Queue Scanner

Read-only Freshdesk triage page that surfaces tickets needing attention: customer-responded tickets with photo/video keywords in the subject, waiting-on-customer tickets, and overdue items.

## Milestone status

This branch is a safe local-development milestone. It supports an explicit offline mode backed by synthetic fixtures. Offline mode never reads the API key and never makes HTTP requests.

## Refresh and reconcile controls

### Normal Refresh

**Refresh Tickets** is the standard day-to-day operation. When a valid successful cursor exists, it retrieves new or changed tickets incrementally from the prior successful attempt start minus the automatic 120-second overlap. With a fresh, legacy, or unusable cursor, it safely bootstraps from a 60-day baseline. Users do not need to choose a Days value for normal Refresh.

### Reconcile Range

**Reconcile Range** is an explicit historical re-check. Choose 7, 14, 30, 60, 90 days, or Custom. The selected window is merged into the existing cache; it does not replace the cache or erase local review history. After a successful Reconcile, its attempt start becomes the cursor for the next normal incremental Refresh.

### Local-only controls

Opening `/queue`, loading the page, applying filters, selecting a Reconcile preset, entering Custom days, switching workflow tabs, **Show All Cached Tickets**, and **Reset to Default Review Scope** are local-only actions. They do not retrieve from Freshdesk.

Successful **Refresh Tickets** applies a rolling 60-day retention policy after reconciliation and before its atomic cache commit. Retention measures ticket `updated_at` against the refresh attempt start time, keeping the exact 60-day cutoff inclusively. Tickets in `Opened / In Review`, `Needs Follow-Up`, or `Needs Supervisor Review` remain cached beyond the window; expired non-active ticket objects may be pruned. Closed tickets follow those same normal age and active-state rules—there is no immediate Closed pruning. Cache retention never deletes or modifies SQLite local review-state rows. If retention validation fails, the existing cache and successful-refresh cursor remain unchanged.

## Local review-state backup protection

The SQLite review database is automatically protected with verified, SQLite-consistent backups in `~/FreshdeskScannerBackups/review_state` by default. Set `REVIEW_BACKUP_DIR` to override the location and `REVIEW_BACKUP_KEEP` to configure retention (default: 200 automatic generations; malformed values use the safe default). A startup baseline and successful queue review, acknowledge-update, automatic review advancement, opened-state, and closed-review mutation create backups. Each backup uses SQLite's backup API, integrity verification, SHA-256 metadata, a temporary file, and atomic finalization. Manual/recovery files are never pruned; only the feature's automatic filename pattern is rotated. If backup work fails, the local database mutation remains committed and the failure is logged (with a concise warning where practical). Tests always use temporary `REVIEW_DB_PATH` and `REVIEW_BACKUP_DIR`; backup protection makes no Freshdesk requests.

## Portland / No Coverage — provisional preview

`/queue?queue_scope=portland` reuses the normal cached-ticket filters, review
workflow tabs, review identity, and XLSX export. **By default this is only a
classification preview.** Tentative candidates remain in their original Main
Queue or Needs Triage; the separately labelled preview count overlaps those
working queues and must not be added to active membership totals. No ticket is
confirmed or automatically excluded by installing this feature.

The initial **provisional** grammar `ascii-10-comma-v1` accepts only the complete
string at `ticket["custom_fields"]["cf_order"]`: exactly ten ASCII digits per
reference, optionally separated by commas, with ASCII spaces/tabs around the
field and commas. It preserves identifiers as strings, including leading zeros,
and never alters the source field. All parsed references must start with `7` to
appear as a Portland-only candidate. Mixed-prefix, missing, malformed, partially
supported, or wrong-type values stay in their original working queue. Other
delimiters, labels, non-ASCII digits, and other lengths are currently unsupported;
**ten digits is not an approved universal sales-order rule**. No numbers are
extracted from subjects, descriptions, phone fields, or other text. Store/home
store fields have no verified mapping and are not used as proof. Raw order values
are not added to UI diagnostics or exports.

### Activation boundary — do not activate before business approval

The only routing switch is the process environment variable
`FRESHDESK_PORTLAND_APPROVED_GRAMMAR`. It is disabled when absent or when its value
differs from the exact supported grammar identifier. URL/form values cannot
activate it. Existing installations therefore default to preview.

Final activation requires **separate authorization**:

1. Confirm that `cf_order` is authoritative and approve the complete grammar,
   including lengths, comma-separated multi-order semantics, and the prefix-7
   mapping. Decide how mixed-origin and conflicting-location tickets are handled.
2. If that contract differs from `ascii-10-comma-v1`, revise the parser, grammar
   identifier, and synthetic tests first; do not activate an approximation.
3. Review preview results privately without publishing customer records or raw
   orders. Run the isolated offline tests; obtain rollout approval.
4. Only then configure the scanner process with
   `FRESHDESK_PORTLAND_APPROVED_GRAMMAR=ascii-10-comma-v1` and restart it through the
   separately approved operational procedure. Do not put this setting in a
   public credential/environment file. No database migration, cache conversion,
   or Freshdesk request is required for classification.
5. Approved routing evaluates Portland **before** the unchanged Main/Triage
   rules. Three active scopes are disjoint; mixed/unknown tickets remain under
   existing Main/Triage rules. Unset the variable and restart to return to
   non-excluding preview without rewriting tickets or reviews.

Scope changes never save/reset review results, notes, timestamps, or update
snapshots. Explicit review/open/acknowledge actions retain their existing local
behavior. Normal/Closed mode switching preserves the selected normal scope;
Closed mode itself and the separate legacy `/closed` cache remain unchanged.
Successful manual refresh, Reset, and Show All retain their existing return-to-
Main behavior. Filters and workflow can still hide a preview/Portland ticket;
this is not an inventory of every historical Portland order. Existing retention
and scheduling are unchanged.

For offline tests use synthetic tickets and temporary database/cache/backup
paths. `FRESHDESK_OFFLINE=1` alone does **not** isolate the review database.
Ordinary old fixtures keep their legacy queue behavior while routing is off;
strict cache-preview tests must use temporary synthetic caches. Activated
offline tests use the real three-way partition. Do not use production caches or
manual live probes for validation.

## What It Does

- Retrieves Freshdesk tickets only when **Refresh Tickets** is explicitly clicked
- Normal **Refresh Tickets** uses the previous successful refresh start as a persistent incremental cursor after initialization; the first/no-cursor normal refresh safely uses a 60-day baseline. The 7/14/30/60/90 and Custom 1-365 Days controls belong only to the explicit **Reconcile Range** action.
- Uses a deliberate two-minute overlap before the prior successful refresh start, then reconciles each complete result into the existing queue cache rather than replacing it. Reconcile Range intentionally reads the selected historical window, still merges and retains, and on success establishes the current attempt start as the next incremental cursor; failed or cancelled refreshes never advance the cursor
- Retention pruning is not active: cached tickets are never deleted merely because of age
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
- The queue is organized by local workflow tabs: To Review, Supervisor Review, Follow-Up, Resolved, and No Action. The `Needs Supervisor Review` state is local-only; workflow tabs and review actions never call Freshdesk. Actual Freshdesk `Closed` tickets are excluded from normal workflow tabs and appear only through the explicit Show All Cached Tickets view.
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
