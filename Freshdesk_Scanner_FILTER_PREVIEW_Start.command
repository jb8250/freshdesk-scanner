#!/bin/bash
set -u

PROJECT="/Users/joshua/Projects/freshdesk-scanner-filter-preview"
PORT="5052"
URL="http://127.0.0.1:${PORT}/queue"
RUNTIME_DIR="$PROJECT/.preview-runtime"
PIDFILE="/tmp/freshdesk-scanner-filter-preview-${PORT}.pid"

if [ ! -x "$PROJECT/.venv/bin/python" ]; then
  exit 1
fi

if [ ! -f "$RUNTIME_DIR/review_state.sqlite3" ] || [ ! -f "$RUNTIME_DIR/queue_live_tickets.json" ] || [ ! -f "$RUNTIME_DIR/closed_tickets.json" ]; then
  exit 1
fi

EXISTING_PID="$(lsof -tiTCP@127.0.0.1:${PORT} -sTCP:LISTEN 2>/dev/null | head -n 1 || true)"
if [ -n "$EXISTING_PID" ]; then
  open "$URL"
  exit 0
fi

cd "$PROJECT" || exit 1
(
  sleep 2
  open "$URL"
) &

echo "$$" > "$PIDFILE"
cleanup() {
  rm -f "$PIDFILE"
}
trap cleanup EXIT INT TERM

FRESHDESK_OFFLINE=1 \
FRESHDESK_OFFLINE_CACHE=1 \
REVIEW_DB_PATH="$RUNTIME_DIR/review_state.sqlite3" \
QUEUE_CACHE_PATH="$RUNTIME_DIR/queue_live_tickets.json" \
CLOSED_CACHE_PATH="$RUNTIME_DIR/closed_tickets.json" \
HOST=127.0.0.1 PORT="$PORT" \
exec "$PROJECT/.venv/bin/python" "$PROJECT/app.py"
