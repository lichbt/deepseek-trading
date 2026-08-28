#!/bin/bash
# 24/7 auto research loop — restarts each batch automatically
# Managed by launchd: ~/Library/LaunchAgents/com.lich.autoresearch.plist

PROJECT_DIR="$(cd "$(dirname "$0")" && pwd)"
PYTHON="$PROJECT_DIR/venv/bin/python"
LOG_DIR="$PROJECT_DIR/.auto-research-logs"
MAX_ITER=31   # RESTORED to 31 on 2026-08-27; it was cut to 20 on 2026-07-24
# because "at ~4 min/iter on the gateway a full 31-instrument batch always hit the
# 2 h watchdog cap and got killed 1-2 iters short". That premise is now 17x stale:
# a measured batch on 2026-08-27 ran 14 iterations in 193s = 13.8 s/iter, so 31
# projects to ~7 minutes against the 2 h ABS_LIMIT. The gateway it described was
# replaced by alibaba MaaS on 2026-08-20 and the thesis/codegen heads were swapped
# for cheaper, faster ones; nothing about the old timing survived.
# At 31 the pool is covered in ONE batch instead of a random 20-of-31 window, and
# the schedule reaches slots i=21..31 that no batch has run since 2026-07-24 —
# including a second GAP slot: gap now fires at i=14 AND i=29, exactly 2.00 times
# per batch on every pool offset. The asset residue also gains i=22, but asset is
# instrument-dependent (_asset_mode_for returns None for the 11 pool instruments
# with no concept), so i=4 and i=22 each fire on 20 of the 31 offsets — ~1.3
# asset slots per batch, not 2.
# If this is ever cut again, re-render the schedule: family shares and which slots
# exist at all are a function of MAX_ITER, because `i` restarts every batch.
# TARGET=MAX_ITER => never early-stop: run the WHOLE batch so all MAX_ITER
# pre-generated thesis ideas get backtested (the thesis batch is one fixed LLM
# call upfront; stopping at the first pass threw the rest of the batch away).
TARGET="$MAX_ITER"
SLEEP_BETWEEN=30
GATE_SLEEP=600   # how long to wait before re-checking a budget/window hold
# Watchdog thresholds. A batch is killed only when it HANGS — detected as the
# log file going silent for STALE_LIMIT seconds. A slow-but-progressing batch
# keeps writing the log and is left to finish (so it can send its report); a
# batch stuck on a hung network call produces no output and gets killed.
# ABS_LIMIT is a hard backstop in case a batch somehow logs forever.
STALE_LIMIT=900     # 15 min of zero log output = hung
ABS_LIMIT=7200      # 2 h absolute backstop

# launchd redirects our stdout/stderr to launchd_stdout.log / launchd_stderr.log
# with no built-in rotation, so they grow unbounded — launchd_stdout.log hit
# 2.4 GB on 2026-07-24. Every batch's output is already captured in its own
# forever_*.log via `tee`, so these launchd logs are a redundant running
# aggregate and are safe to truncate. launchd holds the fd in append mode, so an
# in-place truncate frees the space immediately and writing continues at EOF.
LAUNCHD_LOG_CAP=$((500 * 1024 * 1024))   # 500 MB per file
cap_launchd_logs() {
    for f in "$LOG_DIR/launchd_stdout.log" "$LOG_DIR/launchd_stderr.log"; do
        [ -f "$f" ] || continue
        sz=$(stat -f %z "$f" 2>/dev/null || echo 0)
        if [ "$sz" -gt "$LAUNCHD_LOG_CAP" ]; then
            : > "$f"
            echo "[$(date)] Capped $(basename "$f") (was $((sz/1024/1024)) MB)" >&2
        fi
    done
}

# Load env vars and ensure full PATH
source ~/.zshrc 2>/dev/null
export PATH="/Users/lich/.local/bin:/usr/local/bin:/usr/bin:/bin:$PATH"
# Ensure keychain-accessible env vars are set (required for claude CLI auth)
export USER="${USER:-lich}"
export LOGNAME="${LOGNAME:-lich}"
export HOME="${HOME:-/Users/lich}"

# Data-driven generation (fingerprint + exploit slots) is ON by default as of
# 2026-06-18 — the DRIVEN-vs-NORMAL A/B graduated DRIVEN, so there is no per-batch
# toggle. To run another A/B, set AB_TEST_FINGERPRINT=1 (and optionally
# AB_DRIVEN_RATIO) here and restart; the harness is still in auto_research.py.

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

    cap_launchd_logs   # keep the unrotated launchd stdout/stderr logs bounded

    # Token budget gate. Measured 2026-08-22: this loop burns ~1.2M tokens/hour
    # against a ~14.6M/week plan, so running it around the clock is ~14x over
    # budget. scripts/token_budget.py holds the loop when the rolling cap is
    # reached or we are outside RESEARCH_WINDOW (both configured in .env).
    # It fails OPEN: if the gate itself errors, research continues rather than
    # silently stopping forever.
    if [ -x "$PYTHON" ] && [ -f "$PROJECT_DIR/scripts/token_budget.py" ]; then
        GATE=$("$PYTHON" "$PROJECT_DIR/scripts/token_budget.py" 2>&1)
        GATE_RC=$?
        if [ "$GATE_RC" -eq 1 ]; then
            echo "[$(date)] $GATE — holding ${GATE_SLEEP}s" >&2
            sleep "$GATE_SLEEP"
            continue
        fi
        echo "[$(date)] budget $GATE"
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
