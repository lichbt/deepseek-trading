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

# fix_runner.py loops internally (poll + stop-check). caffeinate keeps the mac
# awake; the while-loop restarts it if it ever crashes, so stops stay monitored.
while true; do
    echo "[$(date)] starting fix_runner.py --live" | tee -a "$LOG_DIR/service.log"
    PYTHONUNBUFFERED=1 caffeinate -i "$PYTHON" -u "$PROJECT_DIR/fix_runner.py" --live \
        >> "$LOG_DIR/fix.log" 2>&1
    echo "[$(date)] fix_runner exited ($?). Restart in 15s." | tee -a "$LOG_DIR/service.log"
    sleep 15
done
