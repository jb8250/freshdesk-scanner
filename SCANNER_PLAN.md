# Freshdesk Ticket Scanner — Project Notes

## Goal
A standalone, read-only Freshdesk triage page that surfaces tickets needing attention: customer-responded tickets with photo/video keywords in the subject, waiting-on-customer tickets, and overdue items. Designed for mobile refresh awareness—prioritizes correctness and simplicity over completeness.

## Architecture
Single Python file, single route.
Served by the existing Flask app. No browser extension integration.

```
Mac/local:
  app.py  →  /queue  →  HTML table with ticket links
   |
   +-- cache/queue_cache.json (30-min TTL)
   +-- reads API key from ~/.config/furtouch/freshdesk_api_key (chmod 600)
```

Why local Flask: avoids CORS, keeps the API key server-side, works on phone/laptop without opening the account to the internet.

## Freshdesk API
- **Endpoint:** `GET /api/v2/tickets` with `updated_since` + page/per_page
- **Auth:** HTTP Basic Auth, `{api_key}:X`
- **Key location:** `~/.config/furtouch/freshdesk_api_key` — **outside the sync tree**
- **Rate limit:** 400/min works out to ~5–10 API calls per refresh for this profile
- **429 handling:** respect `Retry-After`, fail fast with a visible error rather than silent retries

## Filter logic (must match Chrome extension “Work - Highlight Tickets”)
Filter is applied in Python before Jinja renders the template.

| Rule | Implementation |
|------|----------------|
| Status in scope | 2 (Customer responded), 6 (Waiting on customer) |
| Photo/video keywords | `\b(photo|photos|picture|pictures|pic|pics|video|videos|vid)\b`, case-insensitive |
| Tag check | Only tickets with missing/empty `tags` array are flagged |
| Overdue | status 2 only; `due_by` must be in the past (resolution deadline) |
| Closed | status 5 explicitly excluded |
| Scope | **Subject only.** The Chrome extension remains the backstop for matches in the ticket body/description text |

Word-boundary regex is required. Plain substring matching causes false positives (`vid` in `vendor`, `avid`; `pic` in `topic`, `picnic`).

## Pagination
Loop pages 1..N with `per_page=100` on `updated_since`. Stop when returned page size is < 100. Compute `updated_since` at runtime as UTC now minus 60 days.

## Caching
- File: `cache/queue_cache.json`
- TTL: **30 minutes**
- Manual Refresh link always available
- Cache invalidates on any /queue request after TTL

## Output
HTML table with:
- Ticket ID linking to `https://broadriverretail-help.freshdesk.com/a/tickets/{id}`
- Subject
- Status label
- Priority label
- Due date / overdue badge
- Created date
- Tags
- Type

No write actions. No Freshdesk mutations. Stay read-only.

## Security
- Bind to `127.0.0.1`, not `0.0.0.0`
- Do not expose Flask externally
- No `.env` or secrets inside the synced folder
- `.gitignore` the local key file if this ever enters a repo

## Mac-specific
- Port **5050**, not 5000 (AirPlay Receiver conflict on Monterey+)
- Use arm64-native Python, not old Rosetta x86_64 Python
- Expect downtime when the laptop sleeps / lid closes; manual refresh is the recovery mechanism

## Ticket example matching documented filter logic
```json
{
  "id": 432782,
  "subject": "Customer has photos of the damage",
  "status": 2,
  "priority": 3,
  "due_by": "2026-05-22T17:00:00Z",
  "created_at": "2026-05-19T14:32:10Z",
  "tags": [],
  "type": "Complaint",
  "custom_fields": {
    "cf_ticket_reason": "Damage",
    "cf_product": "792-000",
    "cf_on_manifest": true
  }
}
```

## Status codes
| Code | Meaning |
|------|---------|
| 2 | Customer responded |
| 5 | Closed |
| 6 | Waiting on customer |
| 1 | Open |

## Priority codes
| Code | Meaning |
|------|---------|
| 1 | Low |
| 2 | Medium |
| 3 | High |
| 4 | Urgent |

## What the scanner does NOT do
- Search ticket body/description text. The list endpoint excludes description fields to preserve API performance. The Chrome extension remains the body-level safety net.
- Tag completeness checking. The scanner checks photo/video keywords in the subject only, because the list endpoint does not expose tag coverage reliably enough to build a correctness guarantee around.

## Key files
- `/home/ubuntu/notes/furtouch/app.py` — synced to Mac via Syncthing
- `/home/ubuntu/notes/FreshDesk/SCANNER_PLAN.md` — this file
- `/home/ubuntu/.config/furtouch/freshdesk_api_key` — API key, chmod 600, **not synced**
- `/home/ubuntu/notes/FreshDesk/Work - Highlight Tickets/content.js` — extension source of truth for keyword logic
