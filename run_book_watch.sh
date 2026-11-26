#!/bin/bash
# Book watcher — records book-wide loss days and STALLED sleeves, and alerts once
# per episode. Read-only against the book, append-only against the DB: it never
# retires, resizes, flattens or trades, so this is NOT a trading action.
#
# The stall check is the reason this runs at all. A sleeve that stops evaluating
# bars is indistinguishable from a healthy one from the outside, and because
# live_test enforces its software stop INSIDE the same per-bar block, a stalled
# sleeve holding a position is effectively unstopped. usdchf_i21 sat that way for
# twelve days in July 2026 and nothing noticed.
#
# EVERY 4 HOURS, and an interval rather than a wall-clock time on purpose. A
# StartCalendarInterval has to be written in local time and drifts against the
# 21:00 UTC bar close twice a year at DST — the same trap that once put the prop
# trade pass inside the index close. Re-running costs nothing: dedup is structural
# via UNIQUE(event_code, sleeve_id, bar_time), and a stall's bar_time does not move
# while it is stuck, so an ongoing stall alerts ONCE PER EPISODE however often this
# fires. Four-hourly finds it up to twenty hours sooner than a daily job would.
set -uo pipefail

PROJECT_DIR="$(cd "$(dirname "$0")" && pwd)"
PYTHON="$PROJECT_DIR/venv/bin/python"

source ~/.zshrc 2>/dev/null
export PATH="/Users/lich/.local/bin:/usr/local/bin:/usr/bin:/bin:$PATH"
export HOME="${HOME:-/Users/lich}"

cd "$PROJECT_DIR" || exit 1
echo "=== book_watch $(date -u +%Y-%m-%dT%H:%M:%SZ) ==="
"$PYTHON" scripts/book_watch.py
