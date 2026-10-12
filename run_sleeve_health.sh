#!/bin/bash
# Rolling health grades for every live sleeve (3mo / 6mo / 12mo Sharpe).
#
# WHY. The RECENT30 decay check (`portfolio.py:recent_decay_status`) needs 30
# ENTRIES inside 36 months. 12 of the 21 live sleeves cannot reach that bar, so
# they carry no decay guard at all and read INSUFFICIENT forever. Two of them are
# the largest weights in the book. This check is entries-independent: it grades
# the trailing 3/6/12-month Sharpe of the reconstruction, which is computed from
# ACTIVE BARS, so a selective sleeve still gets graded.
#
# Grades: HEALTHY (6mo Sharpe > 0) / REVIEW (6mo < 0, 12mo > 0) /
# RETIRE (both < 0) / LOW_DATA (fewer than 30 active days in 12mo).
#
# READ-ONLY: reconstructs from stored code + candles, reads portfolio_state.json
# for weights, prints. Writes no DB row, places no order, touches no runner state,
# so it is NOT a trading action and needs no interlock. Retiring the sleeve is a
# human decision made from a fresh review (see .claude/skills/sleeve-ops).
#
# WEEKLY, StartInterval. The windows are 3-12 months long, so a shorter interval
# cannot change a grade sooner; the entries-based decay watch stays daily.
set -uo pipefail

PROJECT_DIR="$(cd "$(dirname "$0")" && pwd)"
PYTHON="$PROJECT_DIR/venv/bin/python"

source ~/.zshrc 2>/dev/null
export PATH="/Users/lich/.local/bin:/usr/local/bin:/usr/bin:/bin:$PATH"
export HOME="${HOME:-/Users/lich}"

mkdir -p "$PROJECT_DIR/.paper-trading-logs"
cd "$PROJECT_DIR" || exit 1
echo "=== sleeve_health $(date -u +%Y-%m-%dT%H:%M:%SZ) ==="
"$PYTHON" sleeve_health.py
