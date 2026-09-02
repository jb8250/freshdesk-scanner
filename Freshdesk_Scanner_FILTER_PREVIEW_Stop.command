#!/bin/bash
set -u

PROJECT="/Users/joshua/Projects/freshdesk-scanner-filter-preview"
PORT="5052"
PIDFILE="/tmp/freshdesk-scanner-filter-preview-${PORT}.pid"

stop_if_preview_pid() {
  local pid="$1"
  if [ -z "$pid" ] || ! kill -0 "$pid" 2>/dev/null; then
    return 1
  fi

  local cwd command
  cwd="$(lsof -a -p "$pid" -d cwd -Fn 2>/dev/null | sed -n 's/^n//p' | head -n 1)"
  command="$(ps -p "$pid" -o command= 2>/dev/null || true)"
  if [ "$cwd" = "$PROJECT" ] && [[ "$command" == *"$PROJECT/app.py"* ]]; then
    kill "$pid"
    return 0
  fi
  return 1
}

if [ -f "$PIDFILE" ]; then
  PID="$(cat "$PIDFILE" 2>/dev/null || true)"
  if stop_if_preview_pid "$PID"; then
    rm -f "$PIDFILE"
    exit 0
  fi
  rm -f "$PIDFILE"
fi

PID="$(lsof -tiTCP@127.0.0.1:${PORT} -sTCP:LISTEN 2>/dev/null | head -n 1 || true)"
if [ -n "$PID" ] && stop_if_preview_pid "$PID"; then
  exit 0
fi

exit 1
