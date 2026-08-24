#!/bin/bash
set -u

PROJECT="/Users/joshua/Projects/freshdesk-scanner"
PORT="5050"
URL="http://127.0.0.1:${PORT}/queue"
PIDFILE="/tmp/freshdesk-scanner-${PORT}.pid"

echo "=============================================="
echo " Freshdesk Scanner — LIVE"
echo "=============================================="
echo
echo "Project: $PROJECT"
echo "URL:     $URL"
echo

if [ ! -d "$PROJECT" ]; then
  echo "ERROR: Project folder not found:"
  echo "$PROJECT"
  read -r -p "Press Enter to close..."
  exit 1
fi

if [ ! -x "$PROJECT/.venv/bin/python" ]; then
  echo "ERROR: Python was not found in:"
  echo "$PROJECT/.venv/bin/python"
  read -r -p "Press Enter to close..."
  exit 1
fi

EXISTING_PID="$(lsof -tiTCP:${PORT} -sTCP:LISTEN 2>/dev/null | head -n 1 || true)"
if [ -n "$EXISTING_PID" ]; then
  echo "Port $PORT is already in use by PID $EXISTING_PID."
  echo "Opening the existing scanner..."
  open "$URL"
  read -r -p "Press Enter to close..."
  exit 0
fi

cd "$PROJECT" || exit 1
unset FRESHDESK_OFFLINE

echo "Starting LIVE Freshdesk Scanner..."
echo
echo "Opening /queue does not retrieve from Freshdesk."
echo "Apply Filters, presets, and Workflow tabs are local-only."
echo "Refresh Tickets is normal refresh; Reconcile Range re-checks a selected historical window and merges it into the existing cache without replacing local review history."
echo
echo "Press Ctrl+C in this window to stop the scanner."
echo

(
  sleep 2
  open "$URL"
) &

echo "$$" > "$PIDFILE"

cleanup() {
  rm -f "$PIDFILE"
}
trap cleanup EXIT INT TERM

HOST=127.0.0.1 PORT="$PORT" exec "$PROJECT/.venv/bin/python" "$PROJECT/app.py"
