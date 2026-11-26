#!/bin/bash
# 4-hourly health check for the Zeabur PROP pod, reported through Telegram.
#
# READ-ONLY everywhere: it reads the deployment, the process table, the pass
# receipt and the guard state, and sends one message. It never scales, execs,
# retires, resizes or trades, so it is NOT a trading action and needs no interlock.
#
# It messages on EVERY run, including the all-clear. book_watch already covers the
# book alert-only; this exists to make "the pod is alive and trading" something
# you can see without asking, and so that a scheduler that silently stopped is
# itself noticed (no message for 8 hours is the signal).
#
# StartInterval, not StartCalendarInterval: the pass it watches is pinned to a UTC
# hour, and a wall-clock schedule would drift against it twice a year at DST.
set -uo pipefail

PROJECT_DIR="$(cd "$(dirname "$0")" && pwd)"
PYTHON="$PROJECT_DIR/venv/bin/python"

# launchd starts with a bare environment; the Telegram token/chat id live in
# ~/.zshrc (same path run_book_watch.sh uses), and .env is read by prop_health.py.
source ~/.zshrc 2>/dev/null
export PATH="/Users/lich/.local/bin:/usr/local/bin:/usr/bin:/bin:$PATH"
export HOME="${HOME:-/Users/lich}"

cd "$PROJECT_DIR" || exit 1
echo "=== prop_health $(date -u +%Y-%m-%dT%H:%M:%SZ) ==="
"$PYTHON" scripts/prop_health.py
