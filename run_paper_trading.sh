#!/bin/bash
# 24/7 paper trading loop — spawns live_test.py for each active paper_trading strategy
# Managed by launchd: ~/Library/LaunchAgents/com.lich.papertrading.plist

PROJECT_DIR="$(cd "$(dirname "$0")" && pwd)"
PYTHON="$PROJECT_DIR/venv/bin/python"
LOG_DIR="$PROJECT_DIR/.paper-trading-logs"

# Load env vars (OANDA_API_TOKEN, OANDA_ACCOUNT_ID, etc.)
source ~/.zshrc 2>/dev/null
export PATH="/Users/lich/.local/bin:/usr/local/bin:/usr/bin:/bin:$PATH"

# Hard-coded fallback credentials in case ~/.zshrc fails to load under launchd
export OANDA_API_TOKEN="${OANDA_API_TOKEN:-43f5e160ff289434d6248e5414cc226f-66bdf18f9199213b719671a19ac96998}"
export OANDA_ACCOUNT_ID="${OANDA_ACCOUNT_ID:-101-011-13677064-003}"

# Abort early if credentials are still missing
if [ -z "$OANDA_API_TOKEN" ] || [ -z "$OANDA_ACCOUNT_ID" ]; then
    echo "ERROR: OANDA credentials not set — cannot start paper trading." >&2
    exit 1
fi

mkdir -p "$LOG_DIR"

echo "=== Paper Trading Service started at $(date) ===" | tee "$LOG_DIR/service.log"

# Instrument inference: maps strategy IDs to OANDA instruments
# Uses Python (avoids bash 3.x uppercase limitations)
infer_instrument() {
    "$PYTHON" - "$1" <<'PYEOF'
import sys, re

_INSTRUMENT_MAP = {
    'EURUSD': 'EUR_USD', 'GBPUSD': 'GBP_USD', 'USDJPY': 'USD_JPY',
    'USDCHF': 'USD_CHF', 'AUDUSD': 'AUD_USD', 'NZDUSD': 'NZD_USD',
    'GBPJPY': 'GBP_JPY', 'EURJPY': 'EUR_JPY', 'EURGBP': 'EUR_GBP',
    'XAUUSD': 'XAU_USD', 'XAGUSD': 'XAG_USD', 'BCOUSD': 'BCO_USD',
    'WTICOUSD': 'WTICO_USD', 'NATGASUSD': 'NATGAS_USD',
    'BTCUSD': 'BTC_USD', 'ETHUSD': 'ETH_USD',
    'CORNUSD': 'CORN_USD', 'SOYBNUSD': 'SOYBN_USD', 'WHEATUSD': 'WHEAT_USD',
}
_PREFIX_MAP = {
    'EUR_USD': 'EUR_USD', 'GBP_USD': 'GBP_USD', 'USD_JPY': 'USD_JPY',
    'USD_CHF': 'USD_CHF', 'AUD_USD': 'AUD_USD', 'NZD_USD': 'NZD_USD',
    'GBP_JPY': 'GBP_JPY', 'EUR_JPY': 'EUR_JPY', 'EUR_GBP': 'EUR_GBP',
    'XAU_USD': 'XAU_USD', 'XAG_USD': 'XAG_USD', 'BCO_USD': 'BCO_USD',
    'BTC_USD': 'BTC_USD', 'ETH_USD': 'ETH_USD', 'WTICO_USD': 'WTICO_USD',
    'NATGAS_USD': 'NATGAS_USD',
}

sid = sys.argv[1]
sid_upper = sid.upper()
for prefix, inst in _PREFIX_MAP.items():
    p = prefix + '_'
    pnodash = prefix.replace('_', '') + '_'
    if sid_upper.startswith(p) or sid_upper.startswith(pnodash):
        print(inst); sys.exit(0)
raw = sid.split('_auto_')[0].upper().replace('_', '')
print(_INSTRUMENT_MAP.get(raw, 'EUR_USD'))
PYEOF
}

# Spawn one live_test.py process per strategy; restart if it exits
spawn_trader() {
    local sid="$1"
    local instrument="$2"
    local log="$LOG_DIR/${sid}.log"
    local pidfile="$LOG_DIR/${sid}.pid"

    # PID lock: bail out if another instance is already running for this strategy
    if [ -f "$pidfile" ]; then
        local existing_pid
        existing_pid=$(cat "$pidfile")
        if kill -0 "$existing_pid" 2>/dev/null; then
            echo "[$(date)] [${sid}] Already running (PID $existing_pid) — skipping duplicate spawn" \
                | tee -a "$LOG_DIR/service.log"
            return
        fi
        rm -f "$pidfile"
    fi

    echo "[$(date)] Starting trader: $sid  instrument=$instrument" | tee -a "$LOG_DIR/service.log"

    while true; do
        echo "[$(date)] [${sid}] Launching live_test.py ..." >> "$log"
        PYTHONUNBUFFERED=1 caffeinate -i "$PYTHON" -u "$PROJECT_DIR/live_test.py" \
            "$sid" --instrument "$instrument" \
            >> "$log" 2>&1 &
        local child_pid=$!
        echo "$child_pid" > "$pidfile"
        wait "$child_pid"
        rm -f "$pidfile"
        EXIT_CODE=$?
        echo "[$(date)] [${sid}] live_test.py exited with code $EXIT_CODE. Restarting in 30s..." \
            | tee -a "$log" "$LOG_DIR/service.log"
        sleep 30
    done
}

# ---- Main ----
# Query DB for all paper_trading strategies
STRATEGIES=$("$PYTHON" - "$PROJECT_DIR/pipeline.db" <<'PYEOF'
import sqlite3, sys
db = sys.argv[1]
conn = sqlite3.connect(db)
rows = conn.execute("SELECT id FROM strategies WHERE status='paper_trading' ORDER BY id").fetchall()
conn.close()
for r in rows:
    print(r[0])
PYEOF
)

if [ -z "$STRATEGIES" ]; then
    echo "ERROR: No paper_trading strategies found in DB. Exiting." | tee -a "$LOG_DIR/service.log"
    exit 1
fi

echo "Active strategies:" | tee -a "$LOG_DIR/service.log"
PIDS=()
while IFS= read -r sid; do
    instrument=$(infer_instrument "$sid")
    echo "  $sid  =>  $instrument" | tee -a "$LOG_DIR/service.log"
    spawn_trader "$sid" "$instrument" &
    PIDS+=($!)
done <<< "$STRATEGIES"

echo "Launched ${#PIDS[@]} trader(s). PIDs: ${PIDS[*]}" | tee -a "$LOG_DIR/service.log"

# Wait for all background jobs (launchd keeps the parent alive)
wait
