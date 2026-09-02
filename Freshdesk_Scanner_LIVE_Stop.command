#!/bin/bash
set -u

PROJECT="/Users/joshua/Projects/freshdesk-scanner"
PORT="5050"
PIDFILE="/tmp/freshdesk-scanner-${PORT}.pid"

echo "=============================================="
echo " Freshdesk Scanner — Stop"
echo "=============================================="
echo

stop_if_scanner_pid() {
  local pid="$1"

  if [ -z "$pid" ] || ! kill -0 "$pid" 2>/dev/null; then
    return 1
  fi

  local cwd
  cwd="$(lsof -a -p "$pid" -d cwd -Fn 2>/dev/null | sed -n 's/^n//p' | head -n 1)"

  if [ "$cwd" = "$PROJECT" ]; then
    echo "Stopping Freshdesk Scanner (PID $pid)..."
    kill "$pid"
    return 0
  fi

  return 1
}

if [ -f "$PIDFILE" ]; then
  PID="$(cat "$PIDFILE" 2>/dev/null || true)"
  if stop_if_scanner_pid "$PID"; then
    rm -f "$PIDFILE"
    echo "Stopped."
    exit 0
  fi
  rm -f "$PIDFILE"
fi

PID="$(lsof -tiTCP:${PORT} -sTCP:LISTEN 2>/dev/null | head -n 1 || true)"

if [ -z "$PID" ]; then
  echo "Freshdesk Scanner does not appear to be running on port $PORT."
  exit 0
fi

if stop_if_scanner_pid "$PID"; then
  echo "Stopped."
  exit 0
fi

echo "WARNING: Port $PORT is in use, but it does not appear to be"
echo "the Freshdesk Scanner process from:"
echo "$PROJECT"
echo "Nothing was stopped."
read -r -p "Press Enter to close..."
exit 1
