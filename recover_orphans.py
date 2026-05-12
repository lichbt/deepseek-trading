#!/usr/bin/env python3
"""
Revalidate strategies in 'proposed' status that have no validation_results.
These lost their scores due to an interrupted revalidation run.

Usage:
    python recover_orphans.py --dry-run
    python recover_orphans.py
    python recover_orphans.py --limit 10
"""

import argparse
import json
import sqlite3
from pathlib import Path

DB_PATH = Path(__file__).parent / 'pipeline.db'

_MAP = {
    'EURUSD': 'EUR_USD', 'GBPUSD': 'GBP_USD', 'USDJPY': 'USD_JPY',
    'USDCHF': 'USD_CHF', 'AUDUSD': 'AUD_USD', 'NZDUSD': 'NZD_USD',
    'GBPJPY': 'GBP_JPY', 'EURJPY': 'EUR_JPY', 'EURGBP': 'EUR_GBP',
    'XAUUSD': 'XAU_USD', 'XAGUSD': 'XAG_USD', 'BCOUSD': 'BCO_USD',
    'WTICOUSD': 'WTICO_USD', 'NATGASUSD': 'NATGAS_USD',
    'BTCUSD': 'BTC_USD', 'ETHUSD': 'ETH_USD',
    'CORNUSD': 'CORN_USD', 'SOYBNUSD': 'SOYBN_USD', 'WHEATUSD': 'WHEAT_USD',
}

# Prefix patterns for strategies named like "aud_usd_*" or "gbp_jpy_*"
_PREFIX_MAP = {
    'EUR_USD': 'EUR_USD', 'GBP_USD': 'GBP_USD', 'USD_JPY': 'USD_JPY',
    'USD_CHF': 'USD_CHF', 'AUD_USD': 'AUD_USD', 'NZD_USD': 'NZD_USD',
    'GBP_JPY': 'GBP_JPY', 'EUR_JPY': 'EUR_JPY', 'EUR_GBP': 'EUR_GBP',
    'XAU_USD': 'XAU_USD', 'XAG_USD': 'XAG_USD', 'BCO_USD': 'BCO_USD',
    'BTC_USD': 'BTC_USD', 'ETH_USD': 'ETH_USD',
}


def infer_instrument(sid: str) -> str:
    sid_upper = sid.upper()
    # Check underscore-separated prefix first (e.g. "AUD_USD_*")
    for prefix, inst in _PREFIX_MAP.items():
        if sid_upper.startswith(prefix + '_') or sid_upper.startswith(prefix.replace('_', '') + '_'):
            return inst
    # For _auto_ IDs, strip suffix and match compacted form
    raw = sid.split('_auto_')[0].upper().replace('_', '')
    return _MAP.get(raw, 'EUR_USD')


def get_orphans(limit: int = None):
    conn = sqlite3.connect(str(DB_PATH))
    conn.row_factory = sqlite3.Row
    cur = conn.cursor()
    sql = '''
        SELECT s.id, s.code, s.param_grid, s.rationale, s.timeframe
        FROM strategies s
        LEFT JOIN validation_results vr ON s.id = vr.strategy_id
        WHERE s.status = 'proposed'
          AND vr.strategy_id IS NULL
        ORDER BY s.id
    '''
    if limit:
        sql += f' LIMIT {limit}'
    cur.execute(sql)
    rows = [dict(r) for r in cur.fetchall()]
    conn.close()
    return rows


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--dry-run', action='store_true')
    parser.add_argument('--limit', type=int, default=None, help='Max strategies to process')
    parser.add_argument('--instrument', type=str, default=None, help='Override instrument for all')
    args = parser.parse_args()

    import pipeline_utils as pu
    from validator import validate_strategy
    pu.init_db()

    orphans = get_orphans(args.limit)
    print(f"Found {len(orphans)} orphaned proposed strategies (no validation results)")

    if args.dry_run:
        for o in orphans:
            inst = args.instrument or infer_instrument(o['id'])
            print(f"  {o['id']:<55} [{inst}]")
        print('\n[DRY RUN] No changes made.')
        return

    print(f'\nRevalidating {len(orphans)} strategies...\n')

    passed_ids = []
    failed_ids = []
    error_ids = []

    for i, o in enumerate(orphans, 1):
        sid = o['id']
        instrument = args.instrument or infer_instrument(sid)
        param_grid = json.loads(o['param_grid']) if o['param_grid'] else {}

        candidate = {
            'strategy_id': sid,
            'code': o['code'],
            'param_grid': param_grid,
            'rationale': o['rationale'] or '',
            'timeframe': o['timeframe'] or 'D',
            'instrument': instrument,
        }

        print(f'[{i}/{len(orphans)}] {sid} [{instrument}]')

        try:
            passed, message = validate_strategy(candidate, skip_insert=True)
            if passed:
                print(f'  PASSED: {message}')
                passed_ids.append(sid)
            else:
                print(f'  FAILED: {message}')
                failed_ids.append(sid)
        except Exception as e:
            print(f'  ERROR: {e}')
            error_ids.append(sid)

    print(f'\n{"="*60}')
    print(f'Recovery complete.')
    print(f'  Passed : {len(passed_ids)}')
    print(f'  Failed : {len(failed_ids)}')
    print(f'  Errors : {len(error_ids)}')
    if passed_ids:
        print('Passed strategies:')
        for pid in passed_ids:
            print(f'  {pid}')


if __name__ == '__main__':
    main()
