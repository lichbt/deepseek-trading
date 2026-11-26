#!/bin/bash
# Decay watcher — records verdict FLIPS to strategy_events and alerts on change.
#
# Runs at 00:20, fifteen minutes after com.lich.portfolio's 00:05 rebalance, so it
# reads the verdicts the book will actually act on rather than the previous day's.
#
# DAILY, not weekly, deliberately: the script diffs against the last recorded event
# and is silent when nothing changed, so a daily cadence costs nothing and reports a
# flip the day it happens instead of up to a week later. Read-only against the book;
# it never retires anything.
set -uo pipefail

PROJECT_DIR="$(cd "$(dirname "$0")" && pwd)"
PYTHON="$PROJECT_DIR/venv/bin/python"

source ~/.zshrc 2>/dev/null
export PATH="/Users/lich/.local/bin:/usr/local/bin:/usr/bin:/bin:$PATH"
export HOME="${HOME:-/Users/lich}"

cd "$PROJECT_DIR" || exit 1
echo "=== decay_watch $(date -u +%Y-%m-%dT%H:%M:%SZ) ==="
"$PYTHON" scripts/decay_watch.py
