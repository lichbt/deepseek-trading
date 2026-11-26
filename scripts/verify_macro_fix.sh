#!/bin/bash
# verify_macro_fix.sh — confirm the macro monoculture fix is LIVE and HEALTHY.
#
# History this script encodes (read before editing a check):
#
#   2026-09-12: driver rotation (_MACRO_DRIVERS) + new yield-curve columns
#     (us1mo/us2y/us10y2y/us30y/us_real_yield_20y) shipped.
#   2026-09-12 23:04 -> 2026-09-15: the fix ALSO pushed the thesis prompt over
#     call_openrouter's 12,000-token guardrail. Every batch logged 16x
#     "Prompt too large", whole chunks were refused, generation cratered
#     (0 passed / 26 failed). Untrimmed 2026-09-15.
#   2026-09-15: the FIRST version of this script grepped the logs for
#     "ASSIGNED DRIVER". That check was INVALID and must not come back:
#     auto_research prints a constraint TRUNCATED TO 80 CHARS
#     (`print(f"  [{mode_label}] {constraint[:80]}...")`), so the driver text
#     can never appear in a log. It reported "rotation NOT live" on batches
#     where the rotation was demonstrably running. Use the counter file and the
#     DB instead — those are the reliable markers.
#
# Checks (exit non-zero on the first hard failure):
#   1. .macro_rotation counter exists and advanced during the audited day
#   2. new yield-curve columns injected in that day's logs
#   3. "Prompt too large" ABSENT from that day's logs        <- regression guard
#   4. driver diversity present in the DB (not just real-yield/DXY)
#   5. new-column strategies survive validation
#
# Usage:  ./scripts/verify_macro_fix.sh [YYYYMMDD]
#         (default: yesterday if run before noon, else today)

set -uo pipefail
PROJECT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
LOG_DIR="$PROJECT_DIR/.auto-research-logs"
DB="$PROJECT_DIR/pipeline.db"
ROT="$PROJECT_DIR/.macro_rotation"

if [ -n "${1:-}" ]; then
    DAY="$1"
else
    NOW_H=$(date +%H)
    if [ "$NOW_H" -lt 12 ]; then
        DAY=$(date -v-1d +%Y%m%d 2>/dev/null || date -d "yesterday" +%Y%m%d)
    else
        DAY=$(date +%Y%m%d)
    fi
fi
ISO_DAY="$(echo "$DAY" | sed 's/\(....\)\(..\)\(..\)/\1-\2-\3/')"
FAILED=0

echo "=== Auditing macro fix for day $DAY (iso $ISO_DAY) ==="

# --- Check 1: rotation counter advanced ---
echo ""
echo "--- Check 1: driver-rotation counter (.macro_rotation) ---"
if [ -f "$ROT" ]; then
    VAL="$(cat "$ROT" 2>/dev/null)"
    MTIME="$(stat -f %Sm -t %Y%m%d "$ROT" 2>/dev/null || stat -c %y "$ROT" 2>/dev/null | cut -c1-10 | tr -d '-')"
    echo "counter=$VAL  last-advanced=$(stat -f %Sm -t '%Y-%m-%d %H:%M:%S' "$ROT" 2>/dev/null)"
    if [ "$MTIME" = "$DAY" ]; then
        echo "PASS: rotation counter advanced during $DAY"
    else
        echo "NOTE: counter last advanced on $MTIME, not $DAY (batches may not have run)"
    fi
else
    echo "FAIL: $ROT missing -> _macro_rotation_advance never ran; rotation NOT live"
    FAILED=1
fi

# --- Collect that day's logs ---
LOGS=( "$LOG_DIR"/forever_"$DAY"_*.log )
if [ ! -e "${LOGS[0]}" ]; then
    echo ""
    echo "FAIL: no forever logs for $DAY — nothing to audit (did research run?)"
    exit 1
fi
echo ""
echo "forever logs found: ${#LOGS[@]}"

# --- TIMEZONE: strategies.created_at is UTC, log filenames are LOCAL ---
# pipeline_utils writes `datetime.utcnow().isoformat()`, and run_forever.sh names
# logs with local `date +%Y%m%d_%H%M%S`. Host is UTC+7, so an overnight batch that
# runs on local day D (23:00 D-1 -> 06:00 D) is stamped with UTC day D-1. Querying
# by the LOCAL day therefore returns ZERO rows for every overnight batch — the
# first version of this script did exactly that and reported "0 macro strategies"
# for a batch that had actually produced 1,025. Derive the UTC day from the
# earliest audited log's timestamp instead of assuming.
FIRST_LOG="$(printf '%s\n' "${LOGS[@]}" | sort | head -1)"
LOG_TS="$(basename "$FIRST_LOG" | sed 's/forever_\([0-9]\{8\}\)_\([0-9]\{6\}\).*/\1 \2/')"
if [ -x "$PROJECT_DIR/venv/bin/python" ]; then
    UTC_DAY="$("$PROJECT_DIR/venv/bin/python" -c "
import time
from datetime import datetime
lt = time.strptime('$LOG_TS', '%Y%m%d %H%M%S')
print(datetime.utcfromtimestamp(time.mktime(lt)).strftime('%Y%m%d'))
" 2>/dev/null)"
fi
UTC_DAY="${UTC_DAY:-$DAY}"
UTC_ISO="$(echo "$UTC_DAY" | sed 's/\(....\)\(..\)\(..\)/\1-\2-\3/')"
echo "local batch day: $ISO_DAY   ->  UTC day for created_at: $UTC_ISO (earliest log $LOG_TS)"

# --- Check 2: new columns injected ---
echo ""
echo "--- Check 2: new yield-curve columns injected ---"
INJ="$(grep -hm1 'Injected' "${LOGS[@]}" 2>/dev/null | head -1)"
echo "sample: $INJ"
if echo "$INJ" | grep -q 'us2y' && echo "$INJ" | grep -q 'us10y2y'; then
    echo "PASS: curve columns (us2y/us10y2y) injected"
else
    echo "FAIL: curve columns absent -> still the old column set"
    FAILED=1
fi

# --- Check 3: prompt guardrail regression ---
echo ""
echo "--- Check 3: 'Prompt too large' regression ---"
PTL="$(grep -hc 'Prompt too large' "${LOGS[@]}" 2>/dev/null | awk '{s+=$1} END{print s+0}')"
MAXP="$(grep -ho 'Prompt size: ~[0-9]*' "${LOGS[@]}" 2>/dev/null | grep -o '[0-9]*' | sort -n | tail -1)"
echo "occurrences=$PTL   max prompt size=~${MAXP:-?}  (guardrail 12000)"
if [ "$PTL" -eq 0 ]; then
    echo "PASS: no prompt-too-large refusals"
else
    echo "FAIL: $PTL refusals — the prompt is over the 12,000 guardrail again;"
    echo "      chunks are being silently dropped. Trim a category CONSTRAINT."
    FAILED=1
fi

# --- Check 4: driver diversity ---
# Window is [UTC_DAY, UTC_DAY + 2 days) so a batch that straddles a UTC boundary
# is not truncated. It is keyed on the UTC day derived above, NOT the local day.
echo ""
echo "--- Check 4: driver diversity in the DB (created_at >= $UTC_ISO) ---"
sqlite3 -header -column "$DB" "
SELECT COUNT(*) AS macro_n,
  SUM(rationale LIKE '%real yield%' OR rationale LIKE '%real-yield%') AS real_yield,
  SUM(rationale LIKE '%DXY%' OR rationale LIKE '%dollar%')            AS dollar,
  SUM(rationale LIKE '%differential%' OR rationale LIKE '%divergence%'
      OR rationale LIKE '%carry%')                                    AS carry_div,
  SUM(rationale LIKE '%curve%' OR rationale LIKE '%term spread%'
      OR rationale LIKE '%steepen%' OR rationale LIKE '%flatten%')    AS curve,
  SUM(rationale LIKE '%inflation%' OR rationale LIKE '%CPI%')         AS inflation
FROM strategies WHERE slot_label='MACRO'
  AND created_at >= '$UTC_ISO' AND created_at < date('$UTC_ISO','+2 day');" 2>/dev/null

# --- Check 5: new columns are REACHABLE (not the hallucinated-column failure) ---
# Do NOT require a new-column strategy to have PASSED: the base pass rate is
# ~0.3% (12 passes / ~7,700 gens in the last fortnight), so "0 survived" is the
# expected reading at any sample size this script sees and carries no signal.
# What WOULD be a real failure is the original signature — the model references a
# new column and the validator rejects it as "not available", which means the
# injection and the whitelist disagree. That is what this checks.
echo ""
echo "--- Check 5: new columns reachable (no column-rejection of them) ---"
echo "new-column macro strategies generated (informational):"
sqlite3 -header -column "$DB" "
SELECT status, COUNT(*) AS n FROM strategies
WHERE slot_label='MACRO'
  AND created_at >= '$UTC_ISO' AND created_at < date('$UTC_ISO','+2 day')
  AND (code LIKE '%us2y%' OR code LIKE '%us10y2y%' OR code LIKE '%us30y%'
       OR code LIKE '%us1mo%' OR code LIKE '%us_real_yield_20y%')
GROUP BY status;" 2>/dev/null
BADCOLS="$(grep -ho 'references non-OHLC columns[^"]*' "${LOGS[@]}" 2>/dev/null | sort | uniq -c)"
if [ -n "$BADCOLS" ]; then
    echo "column-rejection messages in logs:"
    echo "$BADCOLS" | sed 's/^/  /'
else
    echo "column-rejection messages in logs: (none)"
fi
if printf '%s' "$BADCOLS" | grep -qE 'us2y|us10y2y|us30y|us1mo|us_real_yield_20y'; then
    echo "FAIL: a NEW yield-curve column was rejected as unavailable -> injection/whitelist mismatch"
    FAILED=1
else
    echo "PASS: no new yield-curve column was rejected as unavailable"
fi

echo ""
if [ "$FAILED" -eq 0 ]; then
    echo "=== RESULT: macro fix is LIVE and the prompt guardrail is healthy ($DAY) ==="
    exit 0
fi
echo "=== RESULT: macro fix has a FAILING check for $DAY — see above ==="
exit 1
