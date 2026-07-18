#!/bin/bash
# Single-process FIX runner for The5ers (cTrader). Keeps fix_runner.py --live alive
# so the stop-loss monitoring never goes dark. Managed by launchd:
#   ~/Library/LaunchAgents/com.lich.fixtrading.plist
PROJECT_DIR="$(cd "$(dirname "$0")" && pwd)"
PYTHON="$PROJECT_DIR/venv/bin/python"
LOG_DIR="$PROJECT_DIR/.fix-logs"; mkdir -p "$LOG_DIR"

source ~/.zshrc 2>/dev/null
set -a; source "$PROJECT_DIR/.env" 2>/dev/null; set +a
export PATH="/Users/lich/.local/bin:/usr/local/bin:/usr/bin:/bin:$PATH"

if [ -z "$FIX_PASSWORD" ]; then
    echo "[$(date)] ERROR: FIX_PASSWORD not set — cannot start." | tee -a "$LOG_DIR/service.log"; exit 1
fi

# PID lock: one FIX session per account. A second runner would fight the first
# over the same logon, so refuse to start a duplicate (launchd + a manual run).
PIDFILE="$LOG_DIR/fixtrading.pid"
if [ -f "$PIDFILE" ] && kill -0 "$(cat "$PIDFILE" 2>/dev/null)" 2>/dev/null; then
    echo "[$(date)] Already running (PID $(cat "$PIDFILE")) — exiting duplicate." | tee -a "$LOG_DIR/service.log"; exit 0
fi
echo $$ > "$PIDFILE"
trap 'rm -f "$PIDFILE"' EXIT

# fix_runner.py loops internally (poll + stop-check). caffeinate keeps the mac
# awake; the while-loop restarts it if it ever crashes, so stops stay monitored.
while true; do
    set -a; source "$PROJECT_DIR/.env" 2>/dev/null; set +a   # re-read .env each restart so new
    echo "[$(date)] starting fix_runner.py --live" | tee -a "$LOG_DIR/service.log"  # vars take effect
    PYTHONUNBUFFERED=1 caffeinate -i "$PYTHON" -u "$PROJECT_DIR/fix_runner.py" --live \
        >> "$LOG_DIR/fix.log" 2>&1
    echo "[$(date)] fix_runner exited ($?). Restart in 15s." | tee -a "$LOG_DIR/service.log"
    sleep 15
done
