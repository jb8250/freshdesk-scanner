# Freshdesk Review Queue Scanner

Read-only Freshdesk triage page that surfaces tickets needing attention: customer-responded tickets with photo/video keywords in the subject, waiting-on-customer tickets, and overdue items.

## Milestone status

This branch is a safe local-development milestone. It supports an explicit offline mode backed by synthetic fixtures. Offline mode never reads the API key and never makes HTTP requests.

## What It Does

- Scans Freshdesk tickets from the last 60 days in live mode
- Uses only `GET /api/v2/tickets` in live mode
- Filters for photo/video keywords in the subject line using word boundaries
- Shows only untagged tickets
- Excludes closed tickets
- Flags overdue customer responses using `due_by`
- Includes waiting-on-customer tickets in the fetched pool; hidden by default and shown with `?waiting=1` (preserved existing behavior)
- Uses a 30-minute cache
- Has no ticket mutation actions
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
