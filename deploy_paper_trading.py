#!/usr/bin/env python3
"""
Auto-Deploy: Deploy passed strategies to paper trading automatically.

Workflow:
1. Find latest 'passed' strategy not already in live_status
2. Mark as 'paper_trading', spawn live_test.py in background
3. Monitor live GT-Score vs walk-forward threshold
4. Auto-retire if live decays >30% below expected for 30+ days

Usage: python3 deploy_paper_trading.py [--strategy-id ID] [--dry-run]
"""

import argparse
import json
import sqlite3
import subprocess
import sys
import time
import os
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional, Dict, Any

# Add project root to path
PROJECT_ROOT = Path(__file__).parent
sys.path.insert(0, str(PROJECT_ROOT))

from pipeline_utils import (
    init_db,
    get_strategy_by_id,
    retire_strategy,
)
from telegram_bot import notify_html


DB_PATH = PROJECT_ROOT / 'pipeline.db'
DECAY_THRESHOLD = 0.70  # Retire if live GT-Score < 70% of walk-forward
DECAY_CONSECUTIVE_DAYS = 30  # Need 30 days below threshold before retiring
MONITOR_INTERVAL_HOURS = 6  # Check every 6 hours


def get_walk_forward_score(strategy_id: str) -> Optional[float]:
    """Fetch walk-forward GT-Score from DB."""
    conn = sqlite3.connect(str(DB_PATH))
    cursor = conn.cursor()
    cursor.execute(
        'SELECT walk_forward_gt_score FROM validation_results WHERE strategy_id = ?',
        (strategy_id,)
    )
    row = cursor.fetchone()
    conn.close()
    return row[0] if row else None


def get_live_status(strategy_id: str) -> Optional[Dict[str, Any]]:
    """Fetch live_status entry."""
    conn = sqlite3.connect(str(DB_PATH))
    cursor = conn.cursor()
    cursor.execute(
        'SELECT start_date, equity_curve, current_gt_score, last_updated FROM live_status WHERE strategy_id = ?',
        (strategy_id,)
    )
    row = cursor.fetchone()
    conn.close()
    if not row:
        return None
    return {
        'start_date': row[0],
        'equity_curve': json.loads(row[1]) if row[1] else [],
        'current_gt_score': row[2],
        'last_updated': row[3],
    }


def is_already_deployed(strategy_id: str) -> bool:
    """Check if strategy already deployed for paper trading."""
    return get_live_status(strategy_id) is not None


def find_pending_strategy() -> Optional[Dict[str, Any]]:
    """Find next strategy to deploy."""
    init_db()
    conn = sqlite3.connect(str(DB_PATH))
    cursor = conn.cursor()

    # Find passed strategies not in live_status
    cursor.execute('''
        SELECT s.id, s.rationale, vr.walk_forward_gt_score
        FROM strategies s
        LEFT JOIN validation_results vr ON s.id = vr.strategy_id
        LEFT JOIN live_status ls ON s.id = ls.strategy_id
        WHERE s.status = 'passed' AND ls.strategy_id IS NULL
        ORDER BY s.created_at ASC
        LIMIT 1
    ''')
    row = cursor.fetchone()
    conn.close()

    if not row:
        return None
    return {
        'id': row[0],
        'rationale': row[1],
        'walk_forward_gt_score': row[2],
    }


def deploy_strategy(strategy_id: str, instrument: str = 'EUR_USD', dry_run: bool = False) -> bool:
    """Deploy a strategy to paper trading."""
    print(f"\n{'='*60}")
    print(f"Deploying: {strategy_id}")
    print(f"Instrument: {instrument}")
    print(f"{'='*60}\n")

    # Fetch from DB to get best_params
    strat = get_strategy_by_id(strategy_id)
    if not strat:
        print(f"ERROR: Strategy {strategy_id} not found")
        return False

    wf_score = get_walk_forward_score(strategy_id)
    print(f"Walk-forward GT-Score: {wf_score:.4f}" if wf_score else "Walk-forward: N/A")

    if dry_run:
        print(f"[DRY RUN] Would launch live_test.py for {strategy_id}")
        return True

    # Send Telegram notification
    notify_html(
        f"<b>🚀 Deploying to Paper Trading</b>\n"
        f"Strategy: {strategy_id}\n"
        f"Instrument: {instrument}\n"
        f"Walk-forward GT-Score: {wf_score:.4f}" if wf_score else "Walk-forward: N/A"
    )

    # Spawn live_test.py in background
    env = os.environ.copy()
    env['PYTHONPATH'] = str(PROJECT_ROOT)
    cmd = [
        sys.executable,
        str(PROJECT_ROOT / 'live_test.py'),
        strategy_id,
        '--instrument', instrument,
    ]
    proc = subprocess.Popen(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env=env,
        bufsize=1,
    )

    print(f"Started live_test.py (PID: {proc.pid})")
    return True


def monitor_live_performance(strategy_id: str) -> Dict[str, Any]:
    """Check live performance vs expected."""
    live_status = get_live_status(strategy_id)
    if not live_status:
        return {'error': 'not_deployed'}

    wf_score = get_walk_forward_score(strategy_id)
    live_score = live_status.get('current_gt_score', 0.0)

    if not wf_score or wf_score <= 0:
        return {'error': 'no_wf_score', 'live_score': live_score}

    threshold = wf_score * DECAY_THRESHOLD

    return {
        'wf_score': wf_score,
        'live_score': live_score,
        'threshold': threshold,
        'below_threshold': live_score < threshold,
        'pct_of_expected': live_score / wf_score if wf_score > 0 else 0,
    }


def monitor_loop(strategy_id: str):
    """Monitor deployed strategy, auto-retire on decay."""
    print(f"Starting monitor loop for {strategy_id}...")

    consecutive_bad_days = 0

    while True:
        result = monitor_live_performance(strategy_id)

        if 'error' in result:
            print(f"Monitor error: {result['error']}, exiting loop")
            break

        wf = result['wf_score']
        live = result['live_score']
        below = result['below_threshold']
        pct = result['pct_of_expected']

        print(f"[{datetime.now().isoformat()}] WF={wf:.4f}, Live={live:.4f}, "
              f"Pct={pct:.1%}, Below={below}")

        if below:
            consecutive_bad_days += 1
            print(f"  -> {consecutive_bad_days}/{DECAY_CONSECUTIVE_DAYS} days below threshold")
            if consecutive_bad_days >= DECAY_CONSECUTIVE_DAYS:
                reason = f'live_gt_score_decay {live:.4f} < {wf:.4f}*{DECAY_THRESHOLD}'
                print(f"Auto-retiring: {reason}")
                notify_html(f"<b>⚠️ Auto-Retiring Strategy</b>\n"
                          f"Strategy: {strategy_id}\n"
                          f"Reason: {reason}\n"
                          f"Live GT-Score: {live:.4f}\n"
                          f"Walk-forward: {wf:.4f}")
                retire_strategy(strategy_id, reason)
                break
        else:
            if consecutive_bad_days > 0:
                print(f"  -> Recovered, resetting bad day counter")
            consecutive_bad_days = 0

        time.sleep(MONITOR_INTERVAL_HOURS * 3600)


def main():
    parser = argparse.ArgumentParser(description='Auto-deploy passed strategies to paper trading')
    parser.add_argument('--strategy-id', help='Specific strategy ID to deploy')
    parser.add_argument('--instrument', default='EUR_USD', help='Instrument to trade')
    parser.add_argument('--dry-run', action='store_true', help='Dry run, no deployment')
    parser.add_argument('--monitor', action='store_true', help='Run monitor loop instead')
    args = parser.parse_args()

    init_db()

    if args.monitor:
        if not args.strategy_id:
            # Find any paper_trading strategy
            conn = sqlite3.connect(str(DB_PATH))
            cursor = conn.cursor()
            cursor.execute(
                "SELECT id FROM strategies WHERE status = 'paper_trading' ORDER BY created_at DESC LIMIT 1"
            )
            row = cursor.fetchone()
            conn.close()
            if not row:
                print("No paper_trading strategies found")
                return
            strategy_id = row[0]
        else:
            strategy_id = args.strategy_id

        monitor_loop(strategy_id)
        return

    # Deployment mode
    if args.strategy_id:
        if is_already_deployed(args.strategy_id):
            print(f"Strategy {args.strategy_id} already deployed")
            return
        deploy_strategy(args.strategy_id, args.instrument, args.dry_run)
    else:
        pending = find_pending_strategy()
        if not pending:
            print("No pending strategies to deploy")
            return
        deploy_strategy(pending['id'], args.instrument, args.dry_run)


if __name__ == '__main__':
    main()