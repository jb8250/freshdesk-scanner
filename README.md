# Freshdesk Review Queue Scanner

Read-only Freshdesk triage page that surfaces tickets needing attention: customer-responded tickets with photo/video keywords in the subject, waiting-on-customer tickets, and overdue items.

## What It Does

- Scans Freshdesk tickets from the last 60 days
- Filters for photo/video keywords in the subject line
- Shows only untagged tickets (matches Chrome extension behavior)
- Flags overdue customer responses
- 30-minute cache with manual refresh
- No write actions — stays read-only

## Local Mac Setup

```bash
# 1. Clone this repo
git clone https://github.com/jb8250/freshdesk-scanner.git
cd freshdesk-scanner

# 2. Create venv and install deps
python3.11 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
python -m spacy download en_core_web_lg

# 3. Add your Freshdesk API key
mkdir -p ~/.config/furtouch
chmod 700 ~/.config/furtouch
nano ~/.config/furtouch/freshdesk_api_key
chmod 600 ~/.config/furtouch/freshdesk_api_key

**Freshdesk API key permissions:** Agent role is sufficient. The scanner only uses `GET /api/v2/tickets` (list endpoint). It does not need write access. Create the key in Freshdesk under **Admin > Security > API Keys**.

# 4. Run the scanner
flask run --host 127.0.0.1 --port 5050
```

Then open http://127.0.0.1:5050/queue

## One-command setup (Mac)

```bash
bash setup.sh
```

This automates steps 2–4 above, including venv creation, dependency install, spacy model download, and API key setup.

## Validate your setup

```bash
bash validate.sh
```

This checks Python version, venv, dependencies, spacy model, API key file, and file permissions.

## Notes

- API key is stored at `~/.config/furtouch/freshdesk_api_key` (outside this repo)
- Binds to 127.0.0.1 only — do not expose externally
- Port 5050 avoids AirPlay Receiver conflict on macOS
- Cache file is `cache/queue_cache.json` (gitignored)
