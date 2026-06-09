#!/bin/bash
# Daily portfolio rebalance wrapper for launchd (com.lich.portfolio).
#
# Runs portfolio.py --write to regenerate portfolio_state.json (the live weight
# file live_test.py consumes). It MUST source ~/.zshrc first so OANDA_ACCOUNT_ID
# / OANDA_API_TOKEN are present — without them build_strategy_returns can't fetch
# candles and live strategies (esp. H4/H1, which need fresh fetches) get dropped
# from the rebalance. The plist ran portfolio.py directly with no creds, so a
# cold-cache run silently produced a partial portfolio_state.json. Mirrors
# run_paper_trading.sh / run_forever.sh.

PROJECT_DIR="$(cd "$(dirname "$0")" && pwd)"
PYTHON="$PROJECT_DIR/venv/bin/python"

# Load env vars (OANDA creds live here) and ensure a full PATH.
source ~/.zshrc 2>/dev/null
export PATH="/Users/lich/.local/bin:/usr/local/bin:/usr/bin:/bin:$PATH"
export HOME="${HOME:-/Users/lich}"
export USER="${USER:-lich}"

cd "$PROJECT_DIR" || exit 1
exec "$PYTHON" "$PROJECT_DIR/portfolio.py" --write
