#!/bin/bash
set -u
PORT="5051"
PIDFILE="/tmp/freshdesk-scanner-theme-preview-${PORT}.pid"
if [ -f "$PIDFILE" ]; then
  PID="$(tr -d '[:space:]' < "$PIDFILE")"
  if [ -n "$PID" ] && ps -p "$PID" -o command= | grep -Fq "/freshdesk-scanner-theme-preview/app.py"; then kill "$PID"; fi
fi
LISTENING="$(lsof -tiTCP:${PORT} -sTCP:LISTEN 2>/dev/null | head -n 1 || true)"
if [ -n "$LISTENING" ] && ps -p "$LISTENING" -o command= | grep -Fq "/freshdesk-scanner-theme-preview/app.py"; then kill "$LISTENING"; fi
rm -f "$PIDFILE"
