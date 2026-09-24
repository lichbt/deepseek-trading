#!/bin/bash
# Re-run the passed book on CURRENT data: does each stored verdict still hold?
#!/bin/bash
#
#!/bin/bash
# WHY. A pass is a claim about the data AND the code that produced it, and both
#!/bin/bash
# move underneath it. The 2026-07-31 data_fetcher dedup fix left ~31% of the
#!/bin/bash
# failed pool not reproducing; nothing re-runs the passed pool, so the same
#!/bin/bash
# drift there is invisible. This makes "still passing" a measurement.
#!/bin/bash
#
# READ-ONLY. scripts/revalidate_passed.py writes no strategies/validation_results
# row (no --write-cause), places no order, touches no runner state - NOT a
# trading action, no interlock. It stops before the holdout on purpose: that
# budget is spent once per candidate by the ordinary validator.
#
# Idempotent: same data + same validator gives the same verdicts, and the JSON is
# overwritten, so a duplicate fire costs one run and changes nothing.
#
# WEEKLY, StartInterval not StartCalendarInterval: no external event to align to,
# and a wall-clock time only adds DST drift. Weekly matches how fast these
# verdicts can move - the pool is re-scored by hand whenever a gate constant, a
# cost model or the candle source changes, which is the only thing a shorter
# interval would catch sooner.
set -uo pipefail

PROJECT_DIR="$(cd "$(dirname "$0")" && pwd)"
PYTHON="$PROJECT_DIR/venv/bin/python"

# launchd starts bare; data_fetcher reads .env and supplementary needs FRED. Same
# ~/.zshrc source as the other jobs.
source ~/.zshrc 2>/dev/null
export PATH="/Users/lich/.local/bin:/usr/local/bin:/usr/bin:/bin:$PATH"
export HOME="${HOME:-/Users/lich}"

mkdir -p "$PROJECT_DIR/.paper-trading-logs"
cd "$PROJECT_DIR" || exit 1
echo "=== revalidate_passed $(date -u +%Y-%m-%dT%H:%M:%SZ) ==="
"$PYTHON" scripts/revalidate_passed.py \
    --out "$PROJECT_DIR/.paper-trading-logs/revalidate_passed.json"
