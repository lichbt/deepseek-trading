#!/usr/bin/env python3
"""
Re-run validation with USE_HISTORICAL_SPREADS=1 for all passed strategies.
Updates validation_results in-place with spread-adjusted scores.

Usage:
    python backtest_with_spread.py
    python backtest_with_spread.py --ids xau_usd_weekly_golden_cross_volatility_filter btcusd_auto_20260508_005059_i17
    python backtest_with_spread.py --dry-run
"""

import argparse
import json
import os
import sqlite3
from pathlib import Path

os.environ['USE_HISTORICAL_SPREADS'] = '1'

DB_PATH = Path(__file__).parent / 'pipeline.db'

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
}


def infer_instrument(sid: str) -> str:
    sid_upper = sid.upper()
    for prefix, inst in _PREFIX_MAP.items():
        if sid_upper.startswith(prefix + '_') or sid_upper.startswith(prefix.replace('_', '') + '_'):
            return inst
    raw = sid.split('_auto_')[0].upper().replace('_', '')
    return _INSTRUMENT_MAP.get(raw, 'EUR_USD')


def get_strategies(ids=None):
    conn = sqlite3.connect(str(DB_PATH))
    conn.row_factory = sqlite3.Row
    cur = conn.cursor()
    if ids:
        placeholders = ','.join('?' * len(ids))
        cur.execute(f'''
            SELECT s.id, s.code, s.param_grid, s.rationale, s.timeframe,
                   vr.is_gt_score, vr.walk_forward_gt_score, vr.holdout_gt_score
            FROM strategies s
            JOIN validation_results vr ON s.id = vr.strategy_id
            WHERE s.id IN ({placeholders})
            ORDER BY vr.walk_forward_gt_score DESC
        ''', ids)
    else:
        cur.execute('''
            SELECT s.id, s.code, s.param_grid, s.rationale, s.timeframe,
                   vr.is_gt_score, vr.walk_forward_gt_score, vr.holdout_gt_score
            FROM strategies s
            JOIN validation_results vr ON s.id = vr.strategy_id
            WHERE s.status = 'passed'
            ORDER BY vr.walk_forward_gt_score DESC
        ''')
    rows = [dict(r) for r in cur.fetchall()]
    conn.close()
    return rows


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--ids', nargs='+', help='Specific strategy IDs (default: all passed)')
    parser.add_argument('--dry-run', action='store_true', help='List targets without running')
    args = parser.parse_args()

    import pipeline_utils as pu
    from validator import validate_strategy
    pu.init_db()

    strategies = get_strategies(args.ids)
    print(f"\nFound {len(strategies)} strategies to backtest with historical spreads\n")

    if args.dry_run:
        for s in strategies:
            inst = infer_instrument(s['id'])
            print(f"  {s['id']:<55} [{inst}]  WF={s['walk_forward_gt_score']:.3f}  HO={s['holdout_gt_score']:.3f}")
        print('\n[DRY RUN] No changes made.')
        return

    results = []
    for i, s in enumerate(strategies, 1):
        sid = s['id']
        instrument = infer_instrument(sid)
        param_grid = json.loads(s['param_grid']) if s['param_grid'] else {}
        orig_wf = s['walk_forward_gt_score'] or 0
        orig_ho = s['holdout_gt_score'] or 0

        print(f"\n[{i}/{len(strategies)}] {sid} [{instrument}]  (orig WF={orig_wf:.3f} HO={orig_ho:.3f})")

        candidate = {
            'strategy_id': sid,
            'code': s['code'],
            'param_grid': param_grid,
            'rationale': s['rationale'] or '',
            'timeframe': s['timeframe'] or 'D',
            'instrument': instrument,
        }

        try:
            passed, message = validate_strategy(candidate, skip_insert=True)
            results.append({'id': sid, 'instrument': instrument,
                            'orig_wf': orig_wf, 'orig_ho': orig_ho,
                            'passed': passed, 'message': message})
            status = 'PASS' if passed else 'FAIL'
            print(f"  => {status}: {message}")
        except Exception as e:
            print(f"  => ERROR: {e}")
            results.append({'id': sid, 'instrument': instrument,
                            'orig_wf': orig_wf, 'orig_ho': orig_ho,
                            'passed': False, 'message': f'ERROR: {e}'})

    # Final summary — re-read fresh scores from DB
    conn = sqlite3.connect(str(DB_PATH))
    conn.row_factory = sqlite3.Row
    cur = conn.cursor()

    print(f"\n\n{'='*115}")
    print(f"SPREAD IMPACT SUMMARY")
    print(f"{'='*115}")
    print(f"{'Strategy':<52} {'Inst':>8}  {'OldWF':>6} {'OldHO':>6}  =>  {'NewWF':>6} {'NewHO':>6}  {'Result'}")
    print('-' * 115)

    passed_count = 0
    for r in results:
        cur.execute('SELECT walk_forward_gt_score, holdout_gt_score FROM validation_results WHERE strategy_id = ?', (r['id'],))
        row = cur.fetchone()
        new_wf = row['walk_forward_gt_score'] if row else 0
        new_ho = row['holdout_gt_score'] if row else 0
        label = 'PASS' if r['passed'] else 'FAIL'
        if r['passed']:
            passed_count += 1
        print(f"{r['id']:<52} {r['instrument']:>8}  {r['orig_wf']:>6.3f} {r['orig_ho']:>6.3f}  =>  {(new_wf or 0):>6.3f} {(new_ho or 0):>6.3f}  {label}")

    conn.close()
    print(f"\nPassed with spread: {passed_count} / {len(results)}")


if __name__ == '__main__':
    main()
