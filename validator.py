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

import sys
import json
import argparse
from datetime import datetime
import pandas as pd
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
)
from data_fetcher import get_candles_date_range


# Configuration
DEV_START = '2015-01-01'
DEV_END = '2019-12-31'
HOLDOUT_START = '2024-01-01'

# Default instrument (can be overridden in strategy JSON)
DEFAULT_INSTRUMENT = 'EUR_USD'

# GT-Score thresholds
MIN_IS_SCORE = 0.3
MIN_WF_SCORE = 0.2
MIN_WINDOW_SCORE = 0.05
HOLDOUT_DECLINE_THRESHOLD = 0.5  # 50% max relative decline


def load_strategy_candidate(json_path: str) -> dict:
    """Load and validate strategy JSON file."""
    with open(json_path, 'r') as f:
        candidate = json.load(f)
    
    required_keys = ['strategy_id', 'code', 'param_grid', 'rationale']
    for key in required_keys:
        if key not in candidate:
            raise ValueError(f'Missing required key: {key}')
    
    candidate['instrument'] = candidate.get('instrument', DEFAULT_INSTRUMENT)
    
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
    
    print(f"\n{'='*70}")
    print(f"Validating: {strategy_id}")
    print(f"Instrument: {instrument}")
    print(f"Rationale: {rationale}")
    print(f"{'='*70}\n")
    
    # Step 1: Check for duplicate fingerprint
    print("[1/8] Checking for duplicate...")
    fingerprint = compute_strategy_fingerprint(code, param_grid)
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
        insert_strategy(strategy_id, fingerprint, code, param_grid, rationale)
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
    
    # Step 4: Fetch development data (in-sample)
    print(f"\n[4/8] Fetching dev data [{DEV_START} to {DEV_END}]...")
    try:
        dev_data = get_candles_date_range(instrument, DEV_START, DEV_END)
        print(f"  {len(dev_data)} candles")
        if len(dev_data) < 100:
            raise ValueError(f'Insufficient data: {len(dev_data)} candles')
    except Exception as e:
        msg = f'FAIL: Data fetch error: {e}'
        print(msg)
        record_validation(strategy_id, {}, 0.0, 0.0, 0.0, f'fail: {msg}')
        return False, msg
    
    # Step 5: Grid search on dev data (in-sample)
    print("\n[5/8] Grid search on dev data (in-sample)...")
    try:
        best_params, is_score = grid_search(dev_data, strategy_func, param_grid)
        print(f"  Best params: {best_params}")
        print(f"  In-sample GT-Score: {is_score:.4f}")
        
        if is_score < MIN_IS_SCORE:
            msg = f'FAIL: In-sample GT-Score {is_score:.4f} < {MIN_IS_SCORE}'
            print(f"  {msg}")
            record_validation(strategy_id, best_params, is_score, None, None, msg)
            return False, msg
    
    except Exception as e:
        msg = f'FAIL: Grid search error: {e}'
        print(msg)
        print(traceback.format_exc())
        record_validation(strategy_id, {}, 0.0, 0.0, 0.0, f'fail: {msg}')
        return False, msg
    
    # Step 6: Fetch full data and run walk-forward
    print("\n[6/8] Walk-forward validation (excluding hold-out)...")
    try:
        # Fetch data up to day before holdout starts (or latest available)
        wf_end = datetime.strptime(HOLDOUT_START, '%Y-%m-%d').strftime('%Y-%m-%d')
        full_data = get_candles_date_range(instrument, DEV_START, wf_end)
        print(f"  {len(full_data)} candles (dev + intermediate)")
        
        wf_result = walk_forward(
            full_data,
            strategy_func,
            param_grid,
            n_windows=5,
            train_length=1000,
            test_length=250
        )
        
        wf_score = wf_result['combined_gt_score']
        min_wf_score = wf_result['min_window_score']
        per_window = wf_result['per_window_gt_scores']
        
        print(f"  Combined WF GT-Score: {wf_score:.4f}")
        print(f"  Per-window scores: {[f'{s:.4f}' for s in per_window]}")
        print(f"  Min window score: {min_wf_score:.4f}")
        
        if wf_score < MIN_WF_SCORE:
            msg = f'FAIL: Walk-forward GT-Score {wf_score:.4f} < {MIN_WF_SCORE}'
            print(f"  {msg}")
            record_validation(strategy_id, best_params, is_score, wf_score, None, msg)
            return False, msg
        
        if min_wf_score < MIN_WINDOW_SCORE:
            msg = f'FAIL: Min window GT-Score {min_wf_score:.4f} < {MIN_WINDOW_SCORE}'
            print(f"  {msg}")
            record_validation(strategy_id, best_params, is_score, wf_score, None, msg)
            return False, msg
    
    except Exception as e:
        msg = f'FAIL: Walk-forward error: {e}'
        print(msg)
        print(traceback.format_exc())
        record_validation(strategy_id, best_params, is_score, None, None, f'fail: {msg}')
        return False, msg
    
    # Step 7: Fetch hold-out data
    print(f"\n[7/8] Hold-out validation (starting {HOLDOUT_START})...")
    try:
        # Fetch from HOLDOUT_START to latest available
        latest_date = datetime.now().strftime('%Y-%m-%d')
        holdout_data = get_candles_date_range(instrument, HOLDOUT_START, latest_date)
        print(f"  {len(holdout_data)} candles (hold-out)")
        
        if len(holdout_data) < 20:
            raise ValueError(f'Hold-out data too sparse: {len(holdout_data)} candles')
    
    except Exception as e:
        msg = f'FAIL: Hold-out data fetch error: {e}'
        print(msg)
        record_validation(strategy_id, best_params, is_score, wf_score, None, f'fail: {msg}')
        return False, msg
    
    # Step 8: Evaluate on hold-out
    print("\n[8/8] Evaluating on hold-out (OOS) with best params...")
    try:
        ho_score = evaluate_on_data(holdout_data, strategy_func, best_params)
        min_ho_score = ho_score  # For now, single hold-out window
        print(f"  Hold-out GT-Score: {ho_score:.4f}")
        
        # Check hold-out decline
        min_acceptable_ho = wf_score * HOLDOUT_DECLINE_THRESHOLD
        if ho_score < min_acceptable_ho:
            msg = f'FAIL: Hold-out decay: {ho_score:.4f} < {min_acceptable_ho:.4f} (30% decline from WF)'
            print(f"  {msg}")
            record_validation(strategy_id, best_params, is_score, wf_score, ho_score, msg)
            return False, msg
    
    except Exception as e:
        msg = f'FAIL: Hold-out evaluation error: {e}'
        print(msg)
        print(traceback.format_exc())
        record_validation(strategy_id, best_params, is_score, wf_score, None, f'fail: {msg}')
        return False, msg
    
    # All checks passed
    print(f"\n{'='*70}")
    print("PASS: Strategy passed all validation gates")
    print(f"{'='*70}")
    print(f"  In-sample GT-Score:      {is_score:.4f}")
    print(f"  Walk-forward GT-Score:   {wf_score:.4f}")
    print(f"  Hold-out GT-Score:       {ho_score:.4f}")
    print(f"  Best Parameters:         {best_params}")
    print(f"{'='*70}\n")
    
    record_validation(strategy_id, best_params, is_score, wf_score, ho_score, 'pass')
    return True, 'PASS'


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
