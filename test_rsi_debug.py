#!/usr/bin/env python3
"""Debug test: simple RSI strategy, see IS/WF scores."""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent))

import os
os.environ['OANDA_ACCOUNT_ID'] = '101-011-13677064-003'
os.environ['OANDA_API_TOKEN'] = '43f5e160ff289434d6248e5414cc226f-66bdf18f9199213b719671a19ac96998'

from pipeline_utils import compute_gt_score, grid_search, walk_forward, compute_strategy_returns
from data_fetcher import get_candles_date_range
import pandas as pd
import numpy as np
import ta

# Simple RSI strategy code
CODE = """def generate_signals(df, params):
    import pandas as pd
    import numpy as np
    import ta
    rsi_window = params.get('rsi_window', 14)
    rsi_oversold = params.get('rsi_oversold', 30)
    rsi_overbought = params.get('rsi_overbought', 70)
    rsi = ta.momentum.rsi(df['close'], window=rsi_window)
    long_entry = rsi < rsi_oversold
    short_entry = rsi > rsi_overbought
    signals = pd.Series(0, index=df.index)
    signals[long_entry] = 1
    signals[short_entry] = -1
    return signals.fillna(0).astype(int)
"""

PARAM_GRID = {
    'rsi_window': [10, 14, 20, 30],
    'rsi_oversold': [25, 30, 35],
    'rsi_overbought': [65, 70, 75],
}

ns = {}
exec(CODE, ns)
fn = ns['generate_signals']

# Test on dev data (2015-2019)
print("Fetching dev data (2015-2019)...")
dev_data = get_candles_date_range('EUR_USD', '2015-01-01', '2019-12-31', 'D')
print(f"  Dev: {len(dev_data)} candles")

# Grid search on dev
print("\nGrid search on dev (2015-2019)...")
best_params, is_score = grid_search(dev_data, fn, PARAM_GRID)
print(f"  Best params: {best_params}")
print(f"  IS GT-Score: {is_score:.4f}")

# Check signals
signals = fn(dev_data, best_params)
non_zero = (signals != 0).sum()
print(f"  Non-zero signals: {non_zero}")

# Show returns
returns = compute_strategy_returns(dev_data, signals)
print(f"  Annual return: {returns.mean() * 252:.4f}")
print(f"  Win rate: {(returns > 0).sum() / len(returns):.4f}")

# Walk-forward
print("\nWalk-forward on full data...")
full_data = get_candles_date_range('EUR_USD', '2015-01-01', '2024-01-01', 'D')
print(f"  Full: {len(full_data)} candles")

wf_result = walk_forward(full_data, fn, PARAM_GRID, n_windows=5, train_length=1000, test_length=250)
print(f"  WF GT-Score: {wf_result['combined_gt_score']:.4f}")
print(f"  Min window: {wf_result['min_window_score']:.4f}")
print(f"  Per-window: {wf_result['per_window_gt_scores']}")

# Now test with grid search on full data for comparison
print("\nGrid search on FULL data (2015-2024)...")
best_params_full, score_full = grid_search(full_data, fn, PARAM_GRID)
print(f"  Best params: {best_params_full}")
print(f"  GT-Score on full: {score_full:.4f}")

signals_full = fn(full_data, best_params_full)
non_zero_full = (signals_full != 0).sum()
print(f"  Non-zero signals: {non_zero_full}")
print(f"  Annual return: {compute_strategy_returns(full_data, signals_full).mean() * 252:.4f}")
print(f"  Win rate: {(compute_strategy_returns(full_data, signals_full) > 0).sum() / len(compute_strategy_returns(full_data, signals_full)):.4f}")