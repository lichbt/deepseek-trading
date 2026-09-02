#!/bin/bash
# 24/7 paper trading loop — spawns live_test.py for each active paper_trading strategy
# Managed by launchd: ~/Library/LaunchAgents/com.lich.papertrading.plist

PROJECT_DIR="$(cd "$(dirname "$0")" && pwd)"
PYTHON="$PROJECT_DIR/venv/bin/python"
LOG_DIR="$PROJECT_DIR/.paper-trading-logs"

# Load env vars (OANDA_API_TOKEN, OANDA_ACCOUNT_ID, etc.)
source ~/.zshrc 2>/dev/null
set -a; source "$PROJECT_DIR/.env" 2>/dev/null; set +a
export PATH="/Users/lich/.local/bin:/usr/local/bin:/usr/bin:/bin:$PATH"

# Hard-coded fallback credentials in case ~/.zshrc fails to load under launchd
export OANDA_API_TOKEN="${OANDA_API_TOKEN:-43f5e160ff289434d6248e5414cc226f-66bdf18f9199213b719671a19ac96998}"
export OANDA_ACCOUNT_ID="${OANDA_ACCOUNT_ID:-101-011-13677064-003}"

# Netting: same-instrument sleeves each send only their own delta; the broker
# holds the running sum so their conviction sizing stacks instead of one
# shadowing the rest. Cut over 2026-06-25 (book flattened first).
export NETTING=1

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
    'BTCUSD': 'BTC_USD', 'ETHUSD': 'ETH_USD', 'LTCUSD': 'LTC_USD',
    'CORNUSD': 'CORN_USD', 'SOYBNUSD': 'SOYBN_USD', 'WHEATUSD': 'WHEAT_USD',
    # Indices + extra metals (pool expansion)
    'SPX500USD': 'SPX500_USD', 'NAS100USD': 'NAS100_USD', 'DE30EUR': 'DE30_EUR',
    'UK100GBP': 'UK100_GBP', 'JP225USD': 'JP225_USD', 'AU200AUD': 'AU200_AUD',
    'HK33HKD': 'HK33_HKD', 'CN50USD': 'CN50_USD',
    'XCUUSD': 'XCU_USD', 'XPTUSD': 'XPT_USD', 'XPDUSD': 'XPD_USD',
}
_PREFIX_MAP = {
    'EUR_USD': 'EUR_USD', 'GBP_USD': 'GBP_USD', 'USD_JPY': 'USD_JPY',
    'USD_CHF': 'USD_CHF', 'AUD_USD': 'AUD_USD', 'NZD_USD': 'NZD_USD',
    'GBP_JPY': 'GBP_JPY', 'EUR_JPY': 'EUR_JPY', 'EUR_GBP': 'EUR_GBP',
    'XAU_USD': 'XAU_USD', 'XAG_USD': 'XAG_USD', 'BCO_USD': 'BCO_USD',
    'BTC_USD': 'BTC_USD', 'ETH_USD': 'ETH_USD', 'WTICO_USD': 'WTICO_USD',
    'NATGAS_USD': 'NATGAS_USD',
    # Indices + extra metals (pool expansion)
    'SPX500_USD': 'SPX500_USD', 'NAS100_USD': 'NAS100_USD', 'DE30_EUR': 'DE30_EUR',
    'UK100_GBP': 'UK100_GBP', 'JP225_USD': 'JP225_USD', 'AU200_AUD': 'AU200_AUD',
    'HK33_HKD': 'HK33_HKD', 'CN50_USD': 'CN50_USD',
    'XCU_USD': 'XCU_USD', 'XPT_USD': 'XPT_USD', 'XPD_USD': 'XPD_USD',
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

    # PID lock: bail out if another instance is already running for this strategy.
    #
    # THE PID MUST BE IDENTIFIED, NOT MERELY ALIVE. `kill -0` only asks whether
    # SOMETHING holds that number, and the OS recycles PIDs — on 2026-08-17 a dead
    # trader's stale pidfile held 1400, which by then belonged to Microsoft Teams,
    # so the guard reported "already running" and eurusd_auto_20260722_043021_i25
    # was silently dropped from the book by a restart. A skipped sleeve trades
    # nothing and the only symptom is one line in service.log, which is the same
    # shape as the 2026-07-31 sleeve that stopped evaluating bars for twelve days.
    # So confirm the process is OUR trader by matching the sid in its command line.
    if [ -f "$pidfile" ]; then
        local existing_pid existing_cmd
        existing_pid=$(cat "$pidfile")
        existing_cmd=$(ps -p "$existing_pid" -o command= 2>/dev/null)
        if [ -n "$existing_cmd" ] && [[ "$existing_cmd" == *"live_test.py $sid"* ]]; then
            echo "[$(date)] [${sid}] Already running (PID $existing_pid) — skipping duplicate spawn" \
                | tee -a "$LOG_DIR/service.log"
            return
        fi
        if [ -n "$existing_cmd" ]; then
            echo "[$(date)] [${sid}] stale pidfile: PID $existing_pid is now someone" \
                 "else — spawning anyway" | tee -a "$LOG_DIR/service.log"
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
        # Capture the CHILD's status immediately. `rm` below resets $?, so
        # reading it afterwards reported rm's exit code (always 0) and every
        # crash was logged as "exited with code 0" — which is why the 2026-07
        # usdchf i21 stall left no usable exit-code evidence.
        EXIT_CODE=$?
        rm -f "$pidfile"
        echo "[$(date)] [${sid}] live_test.py exited with code $EXIT_CODE. Restarting in 30s..." \
            | tee -a "$log" "$LOG_DIR/service.log"
        sleep 30
    done
}

# ---- Main ----
# Query the DB for every sleeve that should trade on the PAPER book.
#   incubating    — observe-only bench: paper book ONLY, withheld from the prop account
#   paper_trading — live on the PROP account (Zeabur/cTrader), NOT run here
# fix_runner.load_sleeves() loads paper_trading alone, so an incubating sleeve
# can never reach real money before it is promoted.
#
# 2026-09-02: NARROWED from IN ('paper_trading','incubating') to incubating only.
# The paper book had become a SHADOW of the prop book, and a misleading one: since
# the 2026-07-27 cutover Zeabur is production, while this book ran the same sleeves
# at different sizing, processed a bar up to ~24h late, and filled through the
# MARKET_HALTED retry path up to 2 days after the signal. It held
# nas100usd_auto_20260701_011303_i9 through a -1.29% bar the pod had already
# exited. incubation.py compared its live returns against reconstruction and
# scored those execution artifacts as sleeve decay.
#
# DO NOT "fix" this by restatusing sleeves. 'paper_trading' is the SAME status
# that authorises the prop book (fix_runner.load_sleeves), so moving a sleeve off
# this launcher via its status silently drops it from Zeabur too. The launcher
# query below is the only correct separation point.
#
# COST, accepted knowingly: incubation.py and scripts/book_watch.py both select
# paper_trading and read this book, so their live-vs-reconstruction tracking now
# goes dark for the deployed sleeves. The three reconstruction-based retire
# signals (decay_scan, sleeve_health, evaluate_strategy RECENT30) are unaffected.
STRATEGIES=$("$PYTHON" - "$PROJECT_DIR/pipeline.db" <<'PYEOF'
import sqlite3, sys
db = sys.argv[1]
conn = sqlite3.connect(db)
rows = conn.execute(
    "SELECT id FROM strategies WHERE status = 'incubating' ORDER BY id"
).fetchall()
conn.close()
for r in rows:
    print(r[0])
PYEOF
)

if [ -z "$STRATEGIES" ]; then
    echo "ERROR: No incubating strategies found in DB. Exiting." | tee -a "$LOG_DIR/service.log"
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
