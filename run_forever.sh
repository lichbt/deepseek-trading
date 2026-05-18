#!/bin/bash
# 24/7 auto research loop — restarts each batch automatically
# Managed by launchd: ~/Library/LaunchAgents/com.lich.autoresearch.plist

PROJECT_DIR="$(cd "$(dirname "$0")" && pwd)"
PYTHON="$PROJECT_DIR/venv/bin/python"
LOG_DIR="$PROJECT_DIR/.auto-research-logs"
MAX_ITER=10
TARGET=1
SLEEP_BETWEEN=30

# Load env vars and ensure full PATH
source ~/.zshrc 2>/dev/null
export PATH="/Users/lich/.local/bin:/usr/local/bin:/usr/bin:/bin:$PATH"
# Ensure keychain-accessible env vars are set (required for claude CLI auth)
export USER="${USER:-lich}"
export LOGNAME="${LOGNAME:-lich}"
export HOME="${HOME:-/Users/lich}"

PIDFILE="$LOG_DIR/run_forever.pid"

mkdir -p "$LOG_DIR"

# PID lock: exit immediately if another instance is already running
if [ -f "$PIDFILE" ]; then
    existing_pid=$(cat "$PIDFILE")
    if kill -0 "$existing_pid" 2>/dev/null; then
        echo "[$(date)] Already running (PID $existing_pid) — exiting duplicate." >&2
        exit 1
    fi
    rm -f "$PIDFILE"
fi
echo $$ > "$PIDFILE"
trap 'rm -f "$PIDFILE"' EXIT

echo "=== Auto Research 24/7 Loop started at $(date) ==="
echo "Max iter per batch: $MAX_ITER | Target: $TARGET"

while true; do
    TIMESTAMP=$(date +%Y%m%d_%H%M%S)
    LOG_FILE="$LOG_DIR/forever_${TIMESTAMP}.log"

    echo "[$(date)] Starting batch..." | tee -a "$LOG_FILE"

    PYTHONUNBUFFERED=1 USE_HISTORICAL_SPREADS=0 \
    caffeinate -i "$PYTHON" -u "$PROJECT_DIR/auto_research.py" \
        --max-iter "$MAX_ITER" \
        --target "$TARGET" \
        2>&1 | tee -a "$LOG_FILE"

    echo "[$(date)] Batch done. Sleeping ${SLEEP_BETWEEN}s before next batch..." | tee -a "$LOG_FILE"
    sleep "$SLEEP_BETWEEN"
done
