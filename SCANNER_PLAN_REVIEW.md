# SCANNER_PLAN.md — Review (2026-07-22)

## Fixes from previous review — status

| # | Finding | What the coder did | Verdict |
|---|---------|-------------------|---------|
| 1 | Cache TTL contradiction (15-min stated, 30–60 preferred) | Settled on **30 min** (line 46) | ✅ Resolved |
| 2 | — | — | — |
| 3 | `vid`/`pic` false positives | Word-boundary regex `\b(...)\b` (line 33, 39) | ✅ Resolved |
| 4 | Scanner subject-only vs extension subject+body | Explicitly documented as subject-only by design, extension is backstop (lines 37, 110) | ✅ Resolved (decision documented) |
| 5 | `updated_since=60 days` not valid API value | "Compute `updated_since` at runtime as UTC now minus 60 days" (line 42) | ✅ Resolved |
| 6 | Pagination mechanism unspecified | "Stop when returned page size is < 100" (line 42) | ✅ Resolved |
| 7 | `.env` synced by Syncthing leaks secrets | Key moved to `~/.config/furtouch/freshdesk_api_key`, explicitly outside sync tree (lines 15, 23, 66, 116) | ✅ Resolved |
| 8 | Current code state unclear | `app.py` path stated (line 114), architecture shows existing Flask app (line 8) | ⚠️ Partially resolved — see below |
| 9 | "Why Waiting on customer" filler | Trimmed entirely | ✅ Resolved |
| 10 | Schema example showed excluded status 5 | Changed to status 2 with matching subject (lines 76–90) | ✅ Resolved |

**7 of 8 prior findings resolved.** Item #8 (does `app.py` already exist with Jinja-side filtering?) is still implied rather than stated — line 114 gives the path but doesn't say "exists, currently filters in Jinja, needs refactor" vs "to be created." Minor.

---

## New issues in this revision

### N1 — Tag check silently dropped (concerning)

The original plan required `tags` empty or missing as a filter condition. The coder **removed the tag check entirely** (line 34: "NOT applied"), citing that "the list endpoint does not return tag completeness reliably enough."

This is a **material scope change**, not a cleanup:
- The original goal (line 7 of old plan): "catches tickets needing review regardless of tags" — but the filter was *also* `AND tags empty`.
- Removing the tag filter means the scanner now surfaces **every** open ticket with photo/video keywords, including well-tagged ones already triaged.

The rationale (API doesn't return tags reliably) is plausible, but **unverified**. The ticket schema (line 83) shows `"tags": []` — so the list endpoint *does* return a tags array. Is the coder's claim that it's *unreliable* based on testing, or an assumption?

**Action needed:** Either (a) verify the claim by hitting the API and checking whether `tags` is consistently populated, or (b) if it really is unreliable, add `fr_due_by` to the overdue check as a compensating signal so the scanner doesn't flood with already-handled tickets.

### N2 — `fr_due_by` added to overdue but logic is imprecise

Line 35: "`due_by` or `fr_due_by` must be in the past" — using OR means a ticket is overdue if *either* deadline passed. Freshdesk considers a ticket overdue based on `due_by` (resolution deadline), not `fr_due_by` (first-response deadline). `fr_due_by` past = "first response breach," which is a different SLA condition.

Decide: does the scanner flag first-response breaches too? If yes, label them separately ("FR overdue" vs "Resolution overdue"). If no, drop `fr_due_by` and use `due_by` only.

### N3 — Rate limit math doesn't close

Line 24: "400/min works out to ~20 API calls per refresh." 

20 API calls × 100 tickets/page = 2,000 tickets in a 60-day window. Is that the expected volume? The math itself is fine (20 calls << 400/min), but the claim "works out to ~20" should derive from the expected ticket volume, not the rate limit. State the assumed volume so the estimate is verifiable.

### N4 — Key file path inconsistency

- Line 15: `~/.config/furtouch/freshdesk_api_key`
- Line 23: same ✓
- Line 114: `/home/ubuntu/notes/furtouch/app.py` (synced)
- Line 116: `/home/ubuntu/.config/furtouch/freshdesk_api_key`

The note dir is `notes/furtouch/` (line 114) but the plan's own path on line 115 is `notes/FreshDesk/`. Pick one casing — Syncthing on case-insensitive filesystems (macOS APFS) will treat `furtouch` and `FreshDesk` as different on Linux but the same directory on Mac, causing silent sync path mismatches.

---

## What's good

- Structure is cleaner — goal up front, tables for codes, clear "what it does NOT do" section.
- Architecture diagram is concise and accurate.
- Security section is tight: loopback bind, no secrets in sync tree, gitignore note.
- Word-boundary regex requirement explicitly justified with examples (line 39).
- Schema example now matches filter logic (status 2, photo keyword in subject, has tags/type).

---

## Verdict

**Build-ready after N1 and N2 are resolved.** The tag-check removal (N1) is the only item that changes what the scanner *does* vs what was originally asked for — confirm that's intentional or get evidence the API is unreliable for tags. N2 (overdue `OR` logic) needs a one-line decision. N3 and N4 are doc-accuracy cleanups.
