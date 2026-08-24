# Freshdesk Scanner Shared Access

## Current status

Shared private access is configured through Tailscale Serve. Funnel is off and the scanner is not publicly exposed.

- **Mac local queue URL:** `http://127.0.0.1:5050/queue`
- **Private Tailscale queue URL:** `https://joshuas-macbook-air.tailbf3c8d.ts.net/queue`

## Architecture

The Mac hosts the one Freshdesk Scanner backend. It is the authority for:

- review database: `data/review_state.sqlite3`
- live queue cache: `cache/queue_live_tickets.json`
- closed cache: `cache/closed_tickets.json`
- one scanner process and its one 30-minute auto-refresh scheduler

The Flask service remains loopback-only at `127.0.0.1:5050`. Tailscale Serve privately proxies that service only to authenticated devices on the same tailnet. Windows uses the Mac-hosted dashboard in a browser; it does not run a second scanner, scheduler, database, or cache.

Review changes made from either browser are immediately shared because both browsers reach the same Mac backend. Do not use file synchronization, OneDrive/Dropbox database sharing, or Windows-side cache/database copies.

## Windows access

1. Ensure Tailscale is connected on Windows and signed in to the same tailnet as the Mac.
2. Open `https://joshuas-macbook-air.tailbf3c8d.ts.net/queue`.
3. Bookmark the page.

Windows does **not** need:

- Python
- Git
- the Freshdesk Scanner repository
- a Freshdesk API key
- SQLite
- local cache files
- a separate auto-refresh process

### Optional Windows desktop shortcut

Create an Internet shortcut to the dashboard:

1. Right-click the Windows desktop.
2. Select **New → Shortcut**.
3. Enter `https://joshuas-macbook-air.tailbf3c8d.ts.net/queue`.
4. Name it **Freshdesk Scanner**.
5. Select **Finish**.

## Mac availability requirements

For Windows access to work, the Mac must remain:

- powered on
- awake
- connected to Tailscale
- running the Freshdesk Scanner

The scanner itself is currently started directly and does not have an automatic login/reboot startup service. It must be started on the Mac after a reboot until optional scanner auto-start is configured separately.

## Security

- Tailscale Serve is the only shared-access mechanism.
- Funnel is **OFF**.
- The dashboard is not publicly exposed.
- Flask remains bound to `127.0.0.1:5050`; no router ports or macOS firewall rules were changed.
