#!/bin/bash
#
# Auto-research watchdog: runs the research loop with auto-restart on crash.
# Logs all output to timestamped files for debugging.
#
# Usage:
#   ./run_auto_research.sh [--target N] [--max-iter N] [--instrument INST]
#
# Example:
#   ./run_auto_research.sh --target 5 --max-iter 200
#

set -euo pipefail

# Configuration
LOG_DIR="${LOG_DIR:-.auto-research-logs}"
RESTART_DELAY="${RESTART_DELAY:-5}"  # seconds between restarts
MAX_RESTARTS="${MAX_RESTARTS:-0}"     # 0 = infinite

# Create log directory
mkdir -p "$LOG_DIR"

# Parse arguments
ARGS=()
while [[ $# -gt 0 ]]; do
  ARGS+=("$1")
  shift
done

# Counter for restarts
RESTART_COUNT=0

echo "🤖 Auto-Research Watchdog Started"
echo "   Logs: $LOG_DIR"
echo "   Args: ${ARGS[@]:-default}"
echo "   Restart delay: ${RESTART_DELAY}s"
if [ "$MAX_RESTARTS" -gt 0 ]; then
  echo "   Max restarts: $MAX_RESTARTS"
fi
echo ""

# Main loop
while true; do
  TIMESTAMP=$(date +"%Y%m%d_%H%M%S")
  LOG_FILE="$LOG_DIR/run_${TIMESTAMP}.log"

  echo "[$(date '+%Y-%m-%d %H:%M:%S')] Starting iteration (restart #$RESTART_COUNT)..."
  echo "[$(date '+%Y-%m-%d %H:%M:%S')] Logs: $LOG_FILE"

  # Run auto_research, capture exit code
  if caffeinate -dimsu python3 auto_research.py "${ARGS[@]}" 2>&1 | tee -a "$LOG_FILE"; then
    # Exited with 0 (success — target reached)
    echo "[$(date '+%Y-%m-%d %H:%M:%S')] ✓ Auto-research target reached"
    exit 0
  else
    EXIT_CODE=$?
    # Exit code 2 = exhausted iterations (not a crash) — restart automatically
    if [ "$EXIT_CODE" -eq 2 ]; then
      echo "[$(date '+%Y-%m-%d %H:%M:%S')] ⏹ Exhausted iterations — restarting for another batch..."
      RESTART_COUNT=$((RESTART_COUNT + 1))
      # Sleep before restart (shorter delay for exhaustion case)
      sleep 3
      echo ""
      continue
    fi
    echo "[$(date '+%Y-%m-%d %H:%M:%S')] ❌ Auto-research crashed with exit code $EXIT_CODE"

    # Check restart limit
    RESTART_COUNT=$((RESTART_COUNT + 1))
    if [ "$MAX_RESTARTS" -gt 0 ] && [ "$RESTART_COUNT" -ge "$MAX_RESTARTS" ]; then
      echo "[$(date '+%Y-%m-%d %H:%M:%S')] 🛑 Max restarts ($MAX_RESTARTS) reached. Exiting."
      exit $EXIT_CODE
    fi

    # Sleep before restart
    echo "[$(date '+%Y-%m-%d %H:%M:%S')] 😴 Sleeping ${RESTART_DELAY}s before restart..."
    sleep "$RESTART_DELAY"
    echo ""
  fi
done
