#!/bin/bash
# 24/7 auto research loop — restarts each batch automatically
# Managed by launchd: ~/Library/LaunchAgents/com.lich.autoresearch.plist

PROJECT_DIR="$(cd "$(dirname "$0")" && pwd)"
PYTHON="$PROJECT_DIR/venv/bin/python"
LOG_DIR="$PROJECT_DIR/.auto-research-logs"
MAX_ITER=10
TARGET=1
SLEEP_BETWEEN=30
# Watchdog thresholds. A batch is killed only when it HANGS — detected as the
# log file going silent for STALE_LIMIT seconds. A slow-but-progressing batch
# keeps writing the log and is left to finish (so it can send its report); a
# batch stuck on a hung network call produces no output and gets killed.
# ABS_LIMIT is a hard backstop in case a batch somehow logs forever.
STALE_LIMIT=900     # 15 min of zero log output = hung
ABS_LIMIT=7200      # 2 h absolute backstop

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

    # Watchdog: kill the batch only when it HANGS. A hung batch (stuck network
    # call) stops writing the log; a slow-but-progressing batch keeps writing.
    # Kill on STALE_LIMIT of log silence, or ABS_LIMIT total as a backstop.
    # pkill matches the python invocation — backgrounded-pipeline PIDs are
    # unreliable to kill directly.
    (
        batch_started=$(date +%s)
        while kill -0 "$BATCH_PID" 2>/dev/null; do
            sleep 120
            now=$(date +%s)
            if [ -f "$LOG_FILE" ]; then
                last_mod=$(stat -f %m "$LOG_FILE" 2>/dev/null || echo "$batch_started")
                if [ $((now - last_mod)) -gt "$STALE_LIMIT" ]; then
                    echo "[$(date)] Watchdog: no log output for ${STALE_LIMIT}s — batch hung, killing." | tee -a "$LOG_FILE"
                    pkill -f "auto_research.py --max-iter" 2>/dev/null
                    break
                fi
            fi
            if [ $((now - batch_started)) -gt "$ABS_LIMIT" ]; then
                echo "[$(date)] Watchdog: batch exceeded ${ABS_LIMIT}s hard cap — killing." | tee -a "$LOG_FILE"
                pkill -f "auto_research.py --max-iter" 2>/dev/null
                break
            fi
        done
    ) &
    WATCHDOG_PID=$!

    wait "$BATCH_PID" 2>/dev/null
    kill "$WATCHDOG_PID" 2>/dev/null   # batch finished on its own — cancel watchdog

    echo "[$(date)] Batch done. Sleeping ${SLEEP_BETWEEN}s before next batch..." | tee -a "$LOG_FILE"
    sleep "$SLEEP_BETWEEN"
done
