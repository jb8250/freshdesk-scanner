# Freshdesk Scanner — Project Context

## Goal
Read-only Freshdesk triage page that surfaces tickets needing attention. No write actions. No data leaves the machine in offline mode.

## Stack
- Python 3.11+
- Flask dashboard routes: `/queue` and offline-only `/closed`, each with local review endpoints
- `requests` for the eventual read-only Freshdesk list API call
- pytest for offline tests
- Local JSON fixtures for guaranteed offline development

## Current Filter Logic (preserved from the repository)
- Status: `2` (Customer responded) or `6` (Waiting on customer)
- Subject must match word-boundary regex: `\b(photo|photos|picture|pictures|pic|pics|video|videos|vid)\b` (case-insensitive)
- Tags: empty or missing only — tickets with any tags are excluded
- Overdue: status `2` only, using `due_by` timestamp in the past
- Closed (`5`) explicitly excluded
- Scope: subject only
- Waiting-on-customer tickets are included in the fetched/cache pool but hidden by default; `/queue?waiting=1` shows them
- Missing or malformed `due_by` values are retained for status-2 tickets because the original code treated those cases as not safely excludable

## API & Auth
- Domain: `broadriverretail-help.freshdesk.com`
- Auth: HTTP Basic `{api_key}:X`
- API key location: `~/.config/furtouch/freshdesk_api_key` (chmod 600, outside this repo)
- Endpoint: `GET /api/v2/tickets` with `updated_since` + pagination (`per_page=100`)
- No live API request is made by this milestone.

## Offline Development
Set `FRESHDESK_OFFLINE=1`. The application then reads only `fixtures/fixtures.json`, displays an offline banner, never reads the API-key file, and never calls HTTP. Missing or malformed fixture data fails closed and cannot fall back to live mode.

## Caching
- File: `cache/tickets.json`
- TTL: 30 minutes
- Plain Refresh link reloads `/queue`; it does not bypass the cache (preserved behavior)

## Local Dev
```bash
python3.11 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
FRESHDESK_OFFLINE=1 flask --app app run --host 127.0.0.1 --port 5050
```
Then open `http://127.0.0.1:5050/queue`.

## Constraints
- Bind to `127.0.0.1` only — external `0.0.0.0` bind is refused
- Port `5050` on macOS
- No `.env` or secrets in this repo
- Stay read-only — no ticket mutations or writes
- The former Mohawk Blend Finder routes were removed; the repository history contains no `blend.py`, and the app could not import before cleanup.

## File Layout
```
app.py                 — scanner-only Flask app, /queue, filters, cache
fixtures/fixtures.json — synthetic multi-page offline fixture data
tests/test_app.py      — offline and safety test suite
conftest.py            — autouse network blocker
validate.sh            — offline validation gate
setup.sh               — safe dependency setup; never prompts for key
```

## Historical documentation
`SCANNER_PLAN.md` and `SCANNER_PLAN_REVIEW.md` are retained as project history. Their older statements about tag checking, the Mohawk routes, and `/home/ubuntu` paths should not override the executable code or this context document.
