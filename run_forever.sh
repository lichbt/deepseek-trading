#!/bin/bash
# 24/7 auto research loop — restarts each batch automatically
# Managed by launchd: ~/Library/LaunchAgents/com.lich.autoresearch.plist

PROJECT_DIR="$(cd "$(dirname "$0")" && pwd)"
PYTHON="$PROJECT_DIR/venv/bin/python"
LOG_DIR="$PROJECT_DIR/.auto-research-logs"
MAX_ITER=10
TARGET=1
SLEEP_BETWEEN=30
# Hard wall-clock cap per batch. A healthy 10-iteration batch is ~20-40 min;
# anything longer means a hung network call (a requests.post to OpenRouter has
# been seen to stall for hours past its own timeout). The watchdog kills such a
# batch so the loop can move on instead of freezing.
MAX_BATCH_SECONDS=2700

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
    # Self-terminate if a newer instance has taken ownership of the PID lock
    if [ -f "$PIDFILE" ] && [ "$(cat "$PIDFILE" 2>/dev/null)" != "$$" ]; then
        echo "[$(date)] PID lock owned by another instance — exiting stale loop." >&2
        exit 1
    fi

    TIMESTAMP=$(date +%Y%m%d_%H%M%S)
    LOG_FILE="$LOG_DIR/forever_${TIMESTAMP}.log"

    echo "[$(date)] Starting batch..." | tee -a "$LOG_FILE"

    PYTHONUNBUFFERED=1 \
    caffeinate -i "$PYTHON" -u "$PROJECT_DIR/auto_research.py" \
        --max-iter "$MAX_ITER" \
        --target "$TARGET" \
        2>&1 | tee -a "$LOG_FILE" &
    BATCH_PID=$!

    # Watchdog: kill a batch that hangs past MAX_BATCH_SECONDS (e.g. a stuck
    # network call) so it can't freeze the loop. pkill matches the python
    # invocation directly — backgrounded-pipeline PIDs are unreliable to kill.
    (
        sleep "$MAX_BATCH_SECONDS"
        if kill -0 "$BATCH_PID" 2>/dev/null; then
            echo "[$(date)] Watchdog: batch exceeded ${MAX_BATCH_SECONDS}s — killing." | tee -a "$LOG_FILE"
            pkill -f "auto_research.py --max-iter" 2>/dev/null
        fi
    ) &
    WATCHDOG_PID=$!

    wait "$BATCH_PID" 2>/dev/null
    kill "$WATCHDOG_PID" 2>/dev/null   # batch finished on its own — cancel watchdog

    echo "[$(date)] Batch done. Sleeping ${SLEEP_BETWEEN}s before next batch..." | tee -a "$LOG_FILE"
    sleep "$SLEEP_BETWEEN"
done
