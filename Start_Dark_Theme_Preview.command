#!/bin/bash
set -u
PROJECT="/Users/joshua/Projects/freshdesk-scanner-theme-preview"
PORT="5051"
URL="http://127.0.0.1:${PORT}/queue"
PIDFILE="/tmp/freshdesk-scanner-theme-preview-${PORT}.pid"
PYTHON="$PROJECT/.venv/bin/python"
if [ ! -x "$PYTHON" ]; then
  PYTHON="/Users/joshua/Projects/freshdesk-scanner/.venv/bin/python"
fi
if [ ! -x "$PYTHON" ]; then echo "ERROR: No project virtual-environment Python was found."; exit 1; fi
if [ "$(lsof -tiTCP:${PORT} -sTCP:LISTEN 2>/dev/null | head -n 1)" ]; then open "$URL"; exit 0; fi
cd "$PROJECT" || exit 1
export FRESHDESK_OFFLINE=1 FRESHDESK_PREVIEW=1 HOST=127.0.0.1 PORT="$PORT"
export REVIEW_DB_PATH="$PROJECT/.preview-runtime/review_state_preview.sqlite3"
export QUEUE_CACHE_PATH="$PROJECT/.preview-runtime/queue_live_tickets_preview.json"
export CLOSED_CACHE_PATH="$PROJECT/.preview-runtime/closed_tickets_preview.json"
(
  sleep 2
  open "$URL"
) &
echo "$$" > "$PIDFILE"
cleanup() { rm -f "$PIDFILE"; }
trap cleanup EXIT INT TERM
exec "$PYTHON" "$PROJECT/app.py"
