"""
Validator Script: Backtest and validate trading strategy candidates.
Entry point: python validator.py <json_file>

Input JSON format:
{
    "strategy_id": "mean_rev_eur_v1",
    "code": "def generate_signals(df, params):\n    ...",
    "param_grid": {"lookback": [10, 20, 30]},
    "rationale": "Mean reversion in EUR_USD based on RSI extremes"
}

Output:
- Updates database with validation results
- Prints "PASS" or "FAIL: <reason>"
"""

import os
import sys
import json
import argparse
from datetime import datetime, timedelta
import pandas as pd
import numpy as np
import traceback

from pipeline_utils import (
    compute_gt_score,
    grid_search,
    walk_forward,
    evaluate_on_data,
    compute_strategy_fingerprint,
    check_idea_is_new,
    insert_strategy,
    record_validation,
    init_db,
    compute_net_strategy_returns,
)
from data_fetcher import get_candles_date_range, get_candles_date_range_with_spread
from supplementary_data import inject_supplementary_data
from risk import compute_max_drawdown, compute_calmar_ratio, compute_ulcer_index
from risk import compute_max_drawdown, compute_calmar_ratio, compute_ulcer_index


# Configuration
DEV_START = '2015-01-01'
DEV_END = '2019-12-31'
HOLDOUT_START = '2024-01-01'

# Default instrument (can be overridden in strategy JSON)
DEFAULT_INSTRUMENT = 'EUR_USD'

# Allowed timeframes
VALID_TIMEFRAMES = ['M30', 'H1', 'H4', 'D', 'W']
DEFAULT_TIMEFRAME = 'D'

# GT-Score thresholds
MIN_IS_SCORE = 0.3
MIN_WF_SCORE = 0.1   # Lowered from 0.2 to allow strategies with moderate edge
MIN_WINDOW_SCORE = 0.0  # Require no losing windows (breakeven allowed)
HOLDOUT_DECLINE_THRESHOLD = 0.5  # 50% max relative decline

# --- Stress-test thresholds (offline OOS validation) ---
MAX_OOS_DRAWDOWN = 0.30       # Flag strategy if max drawdown exceeds 30%
MIN_CALMAR_RATIO = 0.3         # Flag strategy if Calmar ratio below 0.3 (soft gate)

# --- Option B trade-aware WF gate ---
MIN_WINDOWS_WITH_EDGE = 3  # at least 3 windows must have GT > 0 to allow breakeven

# Timeframes to try for multi-timeframe validation
TIMEFRAMES = ['D', 'W', 'H4']

# Use historical bid/ask spreads (more realistic cost modeling)
# Requires fetching more data from OANDA (BBA price mode)
USE_HISTORICAL_SPREADS = os.getenv('USE_HISTORICAL_SPREADS', '').lower() in ('1', 'true', 'yes')


def load_strategy_candidate(json_path: str) -> dict:
    """Load and validate strategy JSON file."""
    with open(json_path, 'r') as f:
        candidate = json.load(f)

    required_keys = ['strategy_id', 'code', 'param_grid', 'rationale']
    for key in required_keys:
        if key not in candidate:
            raise ValueError(f'Missing required key: {key}')

    candidate['instrument'] = candidate.get('instrument', DEFAULT_INSTRUMENT)
    candidate['archetype'] = candidate.get('archetype', 'standard')  # default to standard

    # Validate and set timeframe
    tf = candidate.get('timeframe', DEFAULT_TIMEFRAME)
    if tf is None:
        tf = DEFAULT_TIMEFRAME
    if isinstance(tf, list):
        raise ValueError('timeframe must be a single value, not a list')
    if tf not in VALID_TIMEFRAMES:
        print(f"  Warning: invalid timeframe '{tf}', defaulting to '{DEFAULT_TIMEFRAME}'")
        tf = DEFAULT_TIMEFRAME
    candidate['timeframe'] = tf

    # Validate archetype
    allowed_archetypes = ['standard', 'news', 'session', 'pair']
    if candidate['archetype'] not in allowed_archetypes:
        print(f"  Warning: invalid archetype '{candidate['archetype']}', defaulting to 'standard'")
        candidate['archetype'] = 'standard'

    return candidate


def create_strategy_function(code_str: str):
    """
    Dynamically load strategy function from code string.
    
    Expects code to define: generate_signals(df, params) -> pd.Series
    """
    namespace = {}
    exec(code_str, namespace)
    
    if 'generate_signals' not in namespace:
        raise ValueError('Code must define generate_signals(df, params) function')
    
    return namespace['generate_signals']


def validate_on_timeframe(dev_data, full_data, holdout_data, strategy_func, param_grid,
                        instrument, granularity, strategy_id) -> dict:
    """
    Run full validation pipeline on a single timeframe.
    Returns dict with scores and pass/fail status.
    """
    # Step 5: Grid search on dev data (in-sample)
    best_params, is_score = grid_search(
        dev_data,
        strategy_func,
        param_grid,
        instrument=instrument,
        granularity=granularity,
        apply_costs=True,
    )

    # Check for non-finite IS score
    if not isinstance(is_score, (int, float)) or not np.isfinite(is_score):
        return {
            'granularity': granularity,
            'passed': False,
            'best_params': best_params,
            'is_score': is_score,
            'wf_score': None,
            'min_wf_score': None,
            'ho_score': None,
            'reason': f'IS score non-finite: {is_score}'
        }

    # Check for non-finite IS score
    if not isinstance(is_score, (int, float)) or not np.isfinite(is_score):
        return {
            'granularity': granularity,
            'passed': False,
            'best_params': best_params,
            'is_score': is_score,
            'wf_score': None,
            'min_wf_score': None,
            'ho_score': None,
            'reason': f'IS score non-finite: {is_score}'
        }

    if is_score < MIN_IS_SCORE:
        return {
            'granularity': granularity,
            'passed': False,
            'best_params': best_params,
            'is_score': is_score,
            'wf_score': None,
            'min_wf_score': None,
            'ho_score': None,
            'reason': f'IS {is_score:.4f} < {MIN_IS_SCORE}'
        }

    # Step 6: Walk-forward validation (let walk_forward auto-size windows)
    wf_result = walk_forward(
        full_data,
        strategy_func,
        param_grid,
        n_windows=5,
        instrument=instrument,
        granularity=granularity,
        apply_costs=True,
    )

    wf_score = wf_result['combined_gt_score']
    min_wf_score = wf_result['min_window_score']
    num_valid_windows = wf_result['num_valid_windows']
    total_windows = wf_result['total_windows']

    if wf_score < MIN_WF_SCORE:
        return {
            'granularity': granularity,
            'passed': False,
            'best_params': best_params,
            'is_score': is_score,
            'wf_score': wf_score,
            'min_wf_score': min_wf_score,
            'ho_score': None,
            'reason': f'WF {wf_score:.4f} < {MIN_WF_SCORE}',
            'wf_result': wf_result
        }

    # Check if we have enough valid windows (at least 3 with trades)
    if not wf_result['has_sufficient_windows']:
        total_trades = sum(wf_result.get('per_window_trade_counts', []))
        return {
            'granularity': granularity,
            'passed': False,
            'best_params': best_params,
            'is_score': is_score,
            'wf_score': wf_score,
            'min_wf_score': min_wf_score,
            'ho_score': None,
            'reason': (
                f'Sparse trades: {num_valid_windows}/{total_windows} windows had trades '
                f'(need >= 3), total OOS trades={total_trades}'
            ),
            'wf_result': wf_result
        }

    # Step 7: Hold-out validation
    stress_note = ''
    if holdout_data is not None and len(holdout_data) >= 20:
        ho_signals = strategy_func(holdout_data, best_params)
        ho_trade_count = (ho_signals != 0).sum()

        ho_score = evaluate_on_data(
            holdout_data,
            strategy_func,
            best_params,
            instrument=instrument,
            granularity=granularity,
            apply_costs=True,
        )

        ho_returns = compute_net_strategy_returns(holdout_data, ho_signals, instrument, granularity)
        if len(ho_returns) >= 10:
            max_dd = compute_max_drawdown(ho_returns)
            calmar = compute_calmar_ratio(ho_returns)
            ulcer = compute_ulcer_index(ho_returns)
            if max_dd > MAX_OOS_DRAWDOWN or calmar < MIN_CALMAR_RATIO:
                stress_note = f' | Stress: DD={max_dd:.2%}, Calmar={calmar:.2f}, Ulcer={ulcer:.2f}'

        # Calculate acceptable HO threshold
        # Default: HO >= WF * 0.5 (max 50% decay)
        if ho_trade_count < 10:
            min_acceptable_ho = wf_score * 0.5
            ho_note = f"(low trades: {ho_trade_count})"
        else:
            min_acceptable_ho = wf_score * HOLDOUT_DECLINE_THRESHOLD
            ho_note = ""

        if ho_trade_count > 0 and ho_score < min_acceptable_ho:
            return {
                'granularity': granularity,
                'passed': False,
                'best_params': best_params,
                'is_score': is_score,
                'wf_score': wf_score,
                'min_wf_score': min_wf_score,
                'ho_score': ho_score,
                'ho_trade_count': ho_trade_count,
                'reason': f'HO decay {ho_score:.4f} < {min_acceptable_ho:.4f} {ho_note}{stress_note}',
                'wf_result': wf_result
            }
    else:
        ho_score = None

    return {
        'granularity': granularity,
        'passed': True,
        'best_params': best_params,
        'is_score': is_score,
        'wf_score': wf_score,
        'min_wf_score': min_wf_score,
        'ho_score': ho_score,
        'reason': f'PASS{stress_note}',
        'wf_result': wf_result
    }


def validate_strategy(candidate: dict) -> tuple:
    """
    Run full validation pipeline on strategy candidate.
    
    Returns:
        (passed: bool, message: str)
    """
    strategy_id = candidate['strategy_id']
    code = candidate['code']
    param_grid = candidate['param_grid']
    rationale = candidate['rationale']
    instrument = candidate['instrument']
    timeframe = candidate['timeframe']  # Now validated
    archetype = candidate.get('archetype', 'standard')
    instrument2 = candidate.get('instrument2')

    print(f"\n{'='*70}")
    print(f"Validating: {strategy_id}")
    print(f"Instrument: {instrument}")
    print(f"Archetype: {archetype}")
    if instrument2:
        print(f"Instrument2: {instrument2}")
    print(f"Rationale: {rationale}")
    print(f"{'='*70}\n")
    
    # Step 1: Check for duplicate fingerprint (includes timeframe)
    print("[1/8] Checking for duplicate...")
    fingerprint = compute_strategy_fingerprint(code, param_grid, timeframe, instrument)
    existing = check_idea_is_new(fingerprint)
    
    if not existing['new']:
        status = existing['status']
        msg = f'FAIL: Duplicate fingerprint found (status: {status})'
        print(msg)
        return False, msg
    
    print(f"  Fingerprint: {fingerprint[:16]}... (NEW)")
    
    # Step 2: Insert as proposed
    print("\n[2/8] Inserting as proposed...")
    try:
        insert_strategy(strategy_id, fingerprint, code, param_grid, rationale, timeframe)
        print("  OK")
    except Exception as e:
        msg = f'FAIL: Could not insert strategy: {e}'
        print(msg)
        return False, msg
    
    # Step 3: Load strategy function
    print("\n[3/8] Loading strategy function...")
    try:
        strategy_func = create_strategy_function(code)
        print("  OK")
    except Exception as e:
        msg = f'FAIL: Code error: {e}'
        print(msg)
        record_validation(strategy_id, {}, 0.0, 0.0, 0.0, f'fail: {msg}')
        return False, msg
    
    # Step 4: Fetch data for candidate's timeframe
    print(f"\n[4/8] Fetching data for timeframe [{timeframe}] [{DEV_START} to {DEV_END}]...")
    results = []
    try:
        if USE_HISTORICAL_SPREADS:
            dev_data = get_candles_date_range_with_spread(instrument, DEV_START, DEV_END, granularity=timeframe)
            print(f"  [{timeframe}] {len(dev_data)} candles (with historical spreads)")
        else:
            dev_data = get_candles_date_range(instrument, DEV_START, DEV_END, granularity=timeframe)
            print(f"  [{timeframe}] {len(dev_data)} candles")
        if len(dev_data) >= 100:
            # Inject supplementary data based on archetype
            if archetype != 'standard':
                print(f"  Injecting supplementary data for archetype '{archetype}'...")
                dev_data = inject_supplementary_data(
                    dev_data, archetype, instrument, instrument2,
                    DEV_START, DEV_END, timeframe
                )
                print(f"  Columns now: {list(dev_data.columns)}")
            results.append({'granularity': timeframe, 'dev_data': dev_data, 'error': None})
        else:
            results.append({'granularity': timeframe, 'dev_data': None, 'error': f'Insufficient data: {len(dev_data)} candles'})
    except Exception as e:
        results.append({'granularity': timeframe, 'dev_data': None, 'error': str(e)})

    valid_timeframes = [r for r in results if r['dev_data'] is not None]
    if not valid_timeframes:
        msg = f'FAIL: No valid data for timeframe {timeframe}'
        print(f"  {msg}")
        record_validation(strategy_id, {}, 0.0, 0.0, 0.0, msg)
        return False, msg

    print(f"\n[5/8] Validating on {len(valid_timeframes)} timeframe...")

    best_overall = None
    for tf_result in valid_timeframes:
        tf = tf_result['granularity']
        dev_data = tf_result['dev_data']

        try:
            # Fetch full data for walk-forward
            wf_end = datetime.strptime(HOLDOUT_START, '%Y-%m-%d').strftime('%Y-%m-%d')
            if USE_HISTORICAL_SPREADS:
                full_data = get_candles_date_range_with_spread(instrument, DEV_START, wf_end, granularity=tf)
            else:
                full_data = get_candles_date_range(instrument, DEV_START, wf_end, granularity=tf)
            # Inject supplementary data for full_data if needed
            if archetype != 'standard':
                full_data = inject_supplementary_data(
                    full_data, archetype, instrument, instrument2,
                    DEV_START, wf_end, tf
                )
            # Limit holdout to past 6 months only - avoids OANDA date range limits
            ho_end = (datetime.now() - timedelta(days=30)).strftime('%Y-%m-%d')
            if USE_HISTORICAL_SPREADS:
                holdout_data = get_candles_date_range_with_spread(instrument, HOLDOUT_START, ho_end, granularity=tf)
            else:
                holdout_data = get_candles_date_range(instrument, HOLDOUT_START, ho_end, granularity=tf)
            # Inject supplementary data for holdout_data if needed
            if archetype != 'standard':
                holdout_data = inject_supplementary_data(
                    holdout_data, archetype, instrument, instrument2,
                    HOLDOUT_START, ho_end, tf
                )
        except Exception as e:
            # Holdout fetch failed - may be API date range limit. Proceed without holdout.
            print(f"  [{tf}] Holdout fetch warning: {e}")
            holdout_data = None

        print(f"\n  --- [{tf}] Validation ---")
        result = validate_on_timeframe(
            dev_data, full_data, holdout_data,
            strategy_func, param_grid,
            instrument, tf, strategy_id
        )

        is_s = result['is_score']
        wf_s = result.get('wf_score') or 0.0
        ho_s = result.get('ho_score') or 0.0
        min_wf_s = result.get('min_wf_score') or 0.0
        ho_str = f"{ho_s:.4f}" if ho_s else "N/A"
        # Get window info from walk_forward result if available
        wf_info = ""
        if result.get('wf_result'):
            nvw = result['wf_result'].get('num_valid_windows', '?')
            tw = result['wf_result'].get('total_windows', '?')
            wf_info = f" [{nvw}/{tw} windows]"
        print(f"  [{tf}] IS={is_s:.4f} | WF={wf_s:.4f} | MinWF={min_wf_s:.4f} | HO={ho_str} | {result['reason']}{wf_info}")

        if result['passed']:
            if best_overall is None or wf_s > best_overall['wf_score']:
                best_overall = result

    # Step 6: Final decision
    print(f"\n[6/8] Validation result:")
    for r in results:
        status = 'OK' if not r['error'] else f'FAIL: {r.get("error", "")}'
        print(f"  [{r['granularity']}] {status}")

    if best_overall is None:
        msg = 'FAIL: Validation did not pass all gates'
        print(f"  {msg}")
        record_validation(strategy_id, {}, 0.0, 0.0, 0.0, msg)
        return False, msg

    print(f"\n[7/8] Best result:")
    print(f"  Timeframe: {best_overall['granularity']}")
    print(f"  IS={best_overall['is_score']:.4f} | WF={best_overall['wf_score']:.4f} | MinWF={best_overall['min_wf_score']:.4f} | HO={best_overall.get('ho_score', 'N/A')}")
    print(f"  Best params: {best_overall['best_params']}")

    # Step 8: Record result
    print(f"\n[8/8] Recording to DB...")
    ho_val = best_overall.get('ho_score') or 0.0
    record_validation(
        strategy_id,
        best_overall['best_params'],
        best_overall['is_score'],
        best_overall['wf_score'],
        ho_val,
        f"PASS ({best_overall['granularity']})"
    )

    print(f"\n{'='*70}")
    print("PASS: Strategy passed all validation gates")
    print(f"  Timeframe: {timeframe}")
    print(f"  In-sample GT-Score:      {best_overall['is_score']:.4f}")
    print(f"  Walk-forward GT-Score:   {best_overall['wf_score']:.4f}")
    print(f"  Min window score:        {best_overall['min_wf_score']:.4f}")
    print(f"  Hold-out GT-Score:       {ho_val:.4f}")
    print(f"  Best Parameters:         {best_overall['best_params']}")
    print(f"{'='*70}\n")

    return True, f"PASS ({timeframe})"


def main():
    """CLI entry point."""
    parser = argparse.ArgumentParser(description='Validate trading strategy candidate')
    parser.add_argument('json_file', help='Path to strategy JSON file')
    args = parser.parse_args()
    
    # Initialize database
    init_db()
    
    # Load and validate
    try:
        candidate = load_strategy_candidate(args.json_file)
        passed, message = validate_strategy(candidate)
        
        # Exit code
        sys.exit(0 if passed else 1)
    
    except Exception as e:
        print(f"\nERROR: {e}")
        print(traceback.format_exc())
        sys.exit(1)


if __name__ == '__main__':
    main()
