# Freshdesk Scanner — Project Context

## Goal
Read-only Freshdesk triage page that surfaces tickets needing attention. No write actions. No data leaves the machine.

## Stack
- Python 3.11+
- Flask (single route: `/queue`)
- pdfplumber, pandas, Pillow retained for `/upload` and color picker routes
- Presidio for PII redaction (if extended)
- Freshdesk list API only — search API is unreliable for this account

## Current Filter Logic
- Status: `2` (Customer responded) or `6` (Waiting on customer)
- Subject must match word-boundary regex: `\b(photo|photos|picture|pictures|pic|pics|video|videos|vid)\b` (case-insensitive)
- Tags: empty or missing only — tickets with any tags are excluded
- Overdue: status `2` only, using `due_by` timestamp in the past
- Closed (`5`) explicitly excluded
- Scope: subject only

## API & Auth
- Domain: `broadriverretail-help.freshdesk.com`
- Auth: HTTP Basic `{api_key}:X`
- API key location: `~/.config/furtouch/freshdesk_api_key` (chmod 600, **outside this repo**)
- Endpoint: `GET /api/v2/tickets` with `updated_since` + pagination (`per_page=100`)
- Rate limit: ~400/min; handle `429` with `Retry-After`

## Caching
- File: `cache/queue_cache.json`
- TTL: 30 minutes
- Manual "Refresh now" always available
- Cache invalidates on any `/queue` request after TTL

## Local Dev
```bash
python3.11 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
flask run --host 127.0.0.1 --port 5050
```
Then open http://127.0.0.1:5050/queue

## Constraints
- Bind to `127.0.0.1` only — do not expose Flask externally
- Port `5050` on macOS (port 5000 conflicts with AirPlay Receiver)
- No `.env` or secrets in this repo
- Stay read-only — no ticket mutations, no Freshdesk writes

## File Layout
```
app.py              — Flask app, single /queue route, cache, filters
SCANNER_PLAN.md     — original design doc
SCANNER_PLAN_REVIEW.md — AI coder review feedback
requirements.txt    — Python deps
```
