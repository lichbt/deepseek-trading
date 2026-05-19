"""
Pipeline Utilities: Core functions for strategy research, validation, and live testing.
Handles GT-Score calculation, grid search, walk-forward analysis, and database operations.
"""

import json
import hashlib
import signal
import sqlite3
from datetime import datetime, timedelta
from pathlib import Path
from typing import Dict, List, Tuple, Any, Optional
import warnings
import pandas as pd
import numpy as np
from contextlib import contextmanager

# Suppress noisy FutureWarnings from pandas fillna downcasting (cosmetic, not functional)
warnings.filterwarnings('ignore', category=FutureWarning, module='pandas')

# ============================================================================
# STRATEGY EXECUTION TIMEOUT
# Prevents AI-generated infinite loops from freezing the pipeline.
# ============================================================================

_STRATEGY_CALL_TIMEOUT = 30  # seconds per strategy_func(data, params) call


def _timeout_handler(signum, frame):
    raise TimeoutError(f"Strategy call exceeded {_STRATEGY_CALL_TIMEOUT}s timeout")


# ============================================================================
# GT-SCORE CALCULATION (Alexander Sheppert methodology)
# ============================================================================

def compute_gt_score(returns: pd.Series) -> float:
    """
    Compute GT-Score for a return series.

    Combines:
    - Sharpe ratio (annualised return / vol)
    - Sortino ratio (annualised return / downside deviation) — capped at 10×Sharpe
    - Win-rate consistency (active bars only)

    Returns 0.0 when there are fewer than 20 active (non-zero return) bars —
    too few trades for any ratio to be statistically meaningful.

    Typical range: 0.5–3.0 for genuine strategies.
    """
    if len(returns) < 2:
        return 0.0

    returns = returns.dropna()
    if len(returns) < 2:
        return 0.0

    # 1. Minimum active-trade guard — ratios are meaningless with < 20 trades
    active_returns = returns[returns != 0]
    if len(active_returns) < 20:
        return 0.0

    # 2. Annualised return and volatility
    annual_ret = returns.mean() * 252
    annual_vol = returns.std() * np.sqrt(252)

    if annual_vol < 1e-6:
        return 0.0

    # 3. Sharpe
    sharpe = annual_ret / annual_vol

    # 4. Sortino — cap at max(5.0, |sharpe| * 10) to prevent blow-up when
    #    all negative returns happen to be near-identical tiny values (std → 0).
    downside_returns = returns[returns < 0]
    if len(downside_returns) >= 2:
        downside_dev = downside_returns.std() * np.sqrt(252)
    elif len(downside_returns) == 1:
        # Single loss: treat its magnitude as the downside dev
        downside_dev = abs(downside_returns.iloc[0]) * np.sqrt(252)
    else:
        downside_dev = 0.0

    if downside_dev > 1e-8:
        sortino = annual_ret / downside_dev
    else:
        sortino = sharpe  # no losses → treat same as Sharpe

    sortino_cap = max(5.0, abs(sharpe) * 10)
    sortino = max(-sortino_cap, min(sortino_cap, sortino))

    # 5. Win-rate consistency (active bars only)
    win_rate = (active_returns > 0).sum() / len(active_returns)

    # 6. Combine
    gt_score = (sharpe + 2 * sortino + 2 * (win_rate - 0.5)) / 3.0
    gt_score = max(0.0, gt_score)

    return float(gt_score)


# ============================================================================
# GRID SEARCH
# ============================================================================

def grid_search(
    data: pd.DataFrame,
    strategy_func,
    param_grid: Dict[str, List],
    metric: str = 'gt_score',
    instrument: str = 'EUR_USD',
    granularity: str = 'D',
    apply_costs: bool = True
) -> Tuple[Dict, float]:
    """
    Run full combinatorial grid search over parameters.
    
    For each parameter combo, runs strategy_func on data, evaluates returns,
    computes metric (GT-Score by default).
    
    Args:
        data: pd.DataFrame with columns [date, open, high, low, close]
        strategy_func: callable(df, params) -> pd.Series of signals (-1, 0, 1)
        param_grid: dict of {param_name: [values]}
        metric: 'gt_score' (default)
    
    Returns:
        (best_params: dict, best_score: float)
    """
    if not param_grid:
        return {}, 0.0
    
    # Generate all combinations
    param_names = list(param_grid.keys())
    param_values = [param_grid[name] for name in param_names]
    
    best_params = {}
    best_score = -np.inf
    
    # Iterative combinatorial generation
    def generate_combos(names, values, combo=None):
        if combo is None:
            combo = {}
        
        if not names:
            yield combo.copy()
        else:
            name = names[0]
            rest_names = names[1:]
            rest_values = values[1:]
            for val in values[0]:
                combo[name] = val
                yield from generate_combos(rest_names, rest_values, combo)
    
    for params in generate_combos(param_names, param_values):
        try:
            # Run with timeout — infinite loops in AI-generated code raise TimeoutError
            old_handler = signal.signal(signal.SIGALRM, _timeout_handler)
            signal.alarm(_STRATEGY_CALL_TIMEOUT)
            try:
                signals = strategy_func(data, params)
            finally:
                signal.alarm(0)
                signal.signal(signal.SIGALRM, old_handler)

            if apply_costs:
                returns = compute_net_strategy_returns(data, signals, instrument, granularity)
            else:
                returns = compute_strategy_returns(data, signals)

            if metric == 'gt_score':
                score = compute_gt_score(returns)
            else:
                score = returns.mean() * 252  # Fallback: annualized return

            if score > best_score:
                best_score = score
                best_params = params.copy()

        except TimeoutError:
            # Don't swallow — one frozen combo kills the entire grid search for this strategy
            raise
        except Exception:
            continue

    return best_params, best_score


# ============================================================================
# WALK-FORWARD ANALYSIS
# ============================================================================

def walk_forward(
    full_data: pd.DataFrame,
    strategy_func,
    param_grid: Dict[str, List],
    n_windows: int = 5,
    train_length: Optional[int] = None,
    test_length: Optional[int] = None,
    metric: str = 'gt_score',
    instrument: str = 'EUR_USD',
    granularity: str = 'D',
    apply_costs: bool = True,
    min_valid_windows: int = 3  # Minimum windows that must have trades
) -> Dict[str, Any]:
    """
    Multi-window walk-forward analysis.

    Chronologically splits data into n_windows of train+test.
    For each: grid search on train, evaluate on test (OOS).

    If train_length or test_length are None, they are calculated dynamically
    to utilize the full dataset across n_windows (train=3x test).

    Args:
        full_data: pd.DataFrame with columns [date, open, high, low, close]
        strategy_func: callable(df, params) -> pd.Series
        param_grid: dict of parameter grid
        n_windows: number of walk-forward windows
        train_length: rows per training window
        test_length: rows per test window
        metric: 'gt_score'
        min_valid_windows: minimum windows that must have trades (default 3)

    Returns:
        dict with:
          - combined_gt_score: float
          - per_window_gt_scores: list of floats (only windows with trades)
          - min_window_score: float (min of valid windows only)
          - all_oos_returns: pd.Series of combined OOS returns
          - num_valid_windows: int (windows with at least 1 trade)
          - total_windows: int (total windows attempted)
    """
    data = full_data.reset_index(drop=True)
    total_bars = len(data)

    # Calculate lengths dynamically if not provided
    if train_length is None or test_length is None:
        # We want: train_length + (n_windows) * test_length <= total_bars
        # And we want train_length to be roughly 3x test_length
        # So: 3*test_length + n_windows*test_length = total_bars
        # test_length = total_bars / (n_windows + 3)
        test_len = max(total_bars // (n_windows + 3), 10)
        train_len = test_len * 3
    else:
        train_len = train_length
        test_len = test_length

    all_oos_returns = []
    per_window_scores = []
    per_window_trade_counts = []
    per_window_best_params = []
    total_windows_attempted = 0

    stride = test_len  # Non-overlapping test windows

    for window_idx in range(n_windows):
        train_start = window_idx * stride
        train_end = train_start + train_len
        test_start = train_end
        test_end = test_start + test_len

        if test_end > total_bars:
            break

        # Fetch train and test data
        train_data = data.iloc[train_start:train_end]
        test_data = data.iloc[test_start:test_end]

        total_windows_attempted += 1

        if len(train_data) < 10 or len(test_data) < 10:
            continue

        try:
            # Grid search on train (TimeoutError propagates up if strategy hangs)
            best_params, train_score = grid_search(
                train_data, strategy_func, param_grid, metric=metric,
                instrument=instrument, granularity=granularity, apply_costs=apply_costs
            )

            # Evaluate best params on test (OOS) — also guarded against hangs
            old_handler = signal.signal(signal.SIGALRM, _timeout_handler)
            signal.alarm(_STRATEGY_CALL_TIMEOUT)
            try:
                test_signals = strategy_func(test_data, best_params)
            finally:
                signal.alarm(0)
                signal.signal(signal.SIGALRM, old_handler)

            # Count non-zero signals (actual trades, not just flat)
            num_trades = (test_signals != 0).sum()

            # Skip windows with ZERO trades - these don't provide valid signal
            # "No trades" means "strategy stayed flat", not "strategy failed"
            if num_trades == 0:
                continue

            if apply_costs:
                test_returns = compute_net_strategy_returns(test_data, test_signals, instrument, granularity)
            else:
                test_returns = compute_strategy_returns(test_data, test_signals)
            test_score = compute_gt_score(test_returns)

            per_window_scores.append(test_score)
            per_window_trade_counts.append(num_trades)
            per_window_best_params.append(best_params)
            all_oos_returns.append(test_returns)

        except TimeoutError:
            raise  # Propagate up — frozen strategy kills entire walk-forward
        except Exception:
            pass

    # Combine all OOS returns (only from windows that had trades)
    num_valid_windows = len(per_window_scores)

    if all_oos_returns:
        combined_oos = pd.concat(all_oos_returns, ignore_index=True)
        combined_score = compute_gt_score(combined_oos)
        # min_window_score: only consider NEGATIVE windows (actual losses)
        # Breakeven (0.0) or positive windows don't fail the min threshold
        negative_scores = [s for s in per_window_scores if s < 0]
        min_score = min(negative_scores) if negative_scores else 0.0
        # windows_with_edge: how many windows had GT > 0 (profitable, not just flat)
        windows_with_edge = sum(1 for s in per_window_scores if s > 0)
    else:
        combined_oos = pd.Series(dtype=float)
        combined_score = 0.0
        min_score = 0.0
        windows_with_edge = 0

    # Check if we have enough valid windows
    has_sufficient_windows = num_valid_windows >= min_valid_windows

    return {
        'combined_gt_score': combined_score,
        'per_window_gt_scores': per_window_scores,
        'per_window_trade_counts': per_window_trade_counts,
        'per_window_best_params': per_window_best_params,
        'min_window_score': min_score,
        'windows_with_edge': windows_with_edge,
        'all_oos_returns': combined_oos,
        'num_valid_windows': num_valid_windows,
        'total_windows': total_windows_attempted,
        'has_sufficient_windows': has_sufficient_windows,
    }


# ============================================================================
# STRATEGY EVALUATION
# ============================================================================

def evaluate_on_data(
    data: pd.DataFrame,
    strategy_func,
    params: Dict,
    metric: str = 'gt_score',
    instrument: str = 'EUR_USD',
    granularity: str = 'D',
    apply_costs: bool = True
) -> float:
    """
    Evaluate strategy with given parameters on data.

    Args:
        data: pd.DataFrame
        strategy_func: callable(df, params) -> pd.Series
        params: dict of parameters
        metric: 'gt_score'
        instrument: instrument for cost lookup
        granularity: candle granularity
        apply_costs: apply spread/commission/swap costs

    Returns:
        GT-Score float
    """
    try:
        signals = strategy_func(data, params)
        if apply_costs:
            returns = compute_net_strategy_returns(data, signals, instrument, granularity)
        else:
            returns = compute_strategy_returns(data, signals)
        score = compute_gt_score(returns)
        return score
    except Exception as e:
        return 0.0


# ============================================================================
# TRADING COSTS CONFIG
# ============================================================================

# Typical bid-ask spread in pips per instrument (OANDA typical)
# JPY pairs use 2-decimal pips (0.01), others use 4-decimal (0.0001)
TYPICAL_SPREADS_PIPS = {
    'EUR_USD': 1.2,
    'GBP_USD': 1.6,
    'USD_JPY': 0.12,   # JPY pips are 0.01
    'USD_CHF': 1.4,
    'AUD_USD': 1.4,
    'USD_CAD': 1.6,
    'NZD_USD': 1.8,
    'XAU_USD': 30.0,   # gold: ~$0.30 = 30 pip units (each pip = $0.01)
    'XAG_USD': 3.0,   # silver
    'BTC_USD': 50.0,  # bitcoin: wide spread
    'ETH_USD': 100.0,  # ethereum: even wider
    'BCO_USD': 4.0,    # brent crude
    'WTICO_USD': 4.0,  # WTI crude
    'CORN_USD': 3.0,   # corn
    'NATGAS_USD': 3.0, # natural gas
}
DEFAULT_SPREAD_PIPS = 2.0  # fallback spread in pips

# Pip value per unit for each instrument family (fraction of unit)
# For forex: 1 pip = 0.0001 (except JPY = 0.01)
# For commodities: varies; we use fraction of price for simplicity
PIP_VALUE = {
    'default': 0.0001,
    'USD_JPY': 0.01,
    'XAU_USD': 0.01,   # $0.01 per pip per unit (gold)
    'XAG_USD': 0.01,   # $0.01 per pip per unit (silver)
    'BTC_USD': 0.01,   # $0.01 per pip (bitcoin)
    'ETH_USD': 0.01,   # $0.01 per pip
    'BCO_USD': 0.01,
    'WTICO_USD': 0.01,
    'CORN_USD': 0.01,
    'NATGAS_USD': 0.01,
}

# Price decimal precision for stop-loss orders
# OANDA enforces instrument-specific precision
PRICE_DECIMALS = {
    'default': 4,
    'USD_JPY': 3,
    'JPY': 3,
    'XAU_USD': 3,  # gold: 3 decimals (4738.575)
    'XAG_USD': 4,  # silver: 4 decimals (78.8380)
    'BTC_USD': 1,  # bitcoin: 1 decimal (81373.5)
    'ETH_USD': 2,   # ethereum: 2 decimals
    'WTICO_USD': 3,  # crude oil: 3 decimals
    'BCO_USD': 3,   # brent: 3 decimals
    'CORN_USD': 3,  # corn: 3 decimals
    'NATGAS_USD': 3,
    'GBP_USD': 4,
    'EUR_USD': 4,
    'AUD_USD': 4,
    'USD_CAD': 4,
    'USD_CHF': 4,
    'NZD_USD': 4,
}

# Commission per round-trip (units of instrument)
# OANDA practice: no commission on forex; small fee on commodities
COMMISSION_PER_TRADE = {
    'default': 0.0,
    'XAU_USD': 0.30,   # $0.30 per unit (round trip)
    'BCO_USD': 0.20,
    'WTICO_USD': 0.20,
    'CORN_USD': 0.10,
    'NATGAS_USD': 0.10,
}

# Approximate daily swap/roll per unit (long rate for 1 lot)
# Positive = you receive (carry credit), negative = you pay (carry cost)
# For daily granularity, this is added per bar held overnight
DAILY_SWAP_RATE = {
    'default': 0.0,
    'EUR_USD': -0.00003,   # small cost for holding EUR
    'GBP_USD': -0.00004,
    'USD_JPY': -0.00002,
    'XAU_USD': -0.00008,
}


DEFAULT_PIP_VALUE = 0.0001  # fallback pip value
DEFAULT_COMMISSION = 0.0  # fallback commission (forex typically 0)
DEFAULT_SWAP = 0.0  # fallback swap
DEFAULT_PRICE_DECIMALS = 4  # fallback price precision

# Live pricing cache: {instrument: (spread_pips, timestamp)}
_SPREAD_CACHE: dict = {}
_SPREAD_CACHE_TTL_SECONDS = 300  # 5 minutes


def get_spread_pips(instrument: str) -> float:
    """Get spread in pips for instrument.

    Tries live OANDA pricing first if USE_LIVE_PRICING=1, otherwise uses static defaults.
    """
    import os
    import time

    use_live = os.getenv('USE_LIVE_PRICING', '').lower() in ('1', 'true', 'yes')

    if use_live:
        # Check cache
        now = time.time()
        if instrument in _SPREAD_CACHE:
            spread, timestamp = _SPREAD_CACHE[instrument]
            if now - timestamp < _SPREAD_CACHE_TTL_SECONDS:
                return spread

        # Try to fetch live
        try:
            from data_fetcher import get_live_spreads
            raw_spreads = get_live_spreads([instrument])
            if instrument in raw_spreads:
                raw = raw_spreads[instrument]
                pip_val = get_pip_value(instrument)
                spread_pips = raw / pip_val
                _SPREAD_CACHE[instrument] = (spread_pips, now)
                return spread_pips
        except Exception as e:
            pass  # Fall back to static

    # Static fallback
    return TYPICAL_SPREADS_PIPS.get(instrument, DEFAULT_SPREAD_PIPS)


def get_pip_value(instrument: str) -> float:
    """Get pip value fraction for instrument."""
    return PIP_VALUE.get(instrument, DEFAULT_PIP_VALUE)


def get_commission(instrument: str) -> float:
    """Get commission per round-trip trade."""
    return COMMISSION_PER_TRADE.get(instrument, DEFAULT_COMMISSION)


def get_daily_swap(instrument: str) -> float:
    """Get daily swap/roll per unit for holding overnight."""
    return DAILY_SWAP_RATE.get(instrument, DEFAULT_SWAP)


# Average bars per calendar day for each granularity. Used to scale
# daily_swap so intraday strategies don't get penalised 6× / 24× / 48×.
# W is 1/5 because there are roughly 5 trading days in one weekly bar.
_BARS_PER_DAY = {
    'M30': 48.0,
    'H1':  24.0,
    'H4':   6.0,
    'D':    1.0,
    'W':    0.2,  # one weekly bar covers ~5 trading days
}


def _bars_per_day(granularity: str) -> float:
    """Return the average number of bars per calendar day for a granularity."""
    return _BARS_PER_DAY.get(granularity, 1.0)


def compute_strategy_returns(data: pd.DataFrame, signals: pd.Series) -> pd.Series:
    """
    Compute daily returns from signals and price data.

    Args:
        data: pd.DataFrame with 'close' column
        signals: pd.Series of 1 (long), -1 (short), 0 (flat)

    Returns:
        pd.Series of daily returns
    """
    price_returns = data['close'].pct_change()
    strategy_returns = signals.shift(1) * price_returns  # Enter next period
    return strategy_returns.dropna()


def apply_trading_costs(
    raw_returns: pd.Series,
    signals: pd.Series,
    instrument: str,
    granularity: str = 'D',
    data: pd.DataFrame = None
) -> pd.Series:
    """
    Subtract realistic trading costs from raw returns.
    If data contains 'spread_price', uses per-bar historical spread instead of static fallback.
    """
    net_returns = raw_returns.copy()
    if len(net_returns) == 0:
        return net_returns

    pip_val = get_pip_value(instrument)
    commission = get_commission(instrument)
    # Per-bar swap = daily_swap / bars_per_day for the granularity.
    # Without this, intraday strategies are penalised 6× (H4) or 24× (H1)
    # because daily_swap is applied to every bar of a held position.
    daily_swap = get_daily_swap(instrument) / _bars_per_day(granularity)

    # Use dynamic spread if available in data, else static
    has_dynamic_spread = (data is not None and 'spread_price' in data.columns and
                       data['spread_price'].notna().any())
    if has_dynamic_spread:
        # spread_price column stores pips (data_fetcher divides ask-bid price units by pip_val)
        spread_pips = get_spread_pips(instrument)
        dynamic_spread_pips = data['spread_price'].fillna(spread_pips).values[1:]
        if len(dynamic_spread_pips) > len(net_returns):
            dynamic_spread_pips = dynamic_spread_pips[:len(net_returns)]
        cost_price_units = dynamic_spread_pips * pip_val
    else:
        spread_pips = get_spread_pips(instrument)
        cost_price_units = spread_pips * pip_val

    # We must convert costs in price units (like $0.36) to percentage impact (like 0.0003)
    # The return at i is price_pct_change[i] = (close[i]-close[i-1])/close[i-1].
    # So the cost as a percentage is cost_price_units / close[i-1].
    
    # Get the entry prices (close[i-1]) aligned with returns[i]
    if data is not None and 'close' in data.columns:
        prev_close = data['close'].values[:-1]  # length n-1, matches raw_returns
    else:
        # Fallback if no data provided: assume unit price is 1.0 
        # (This is inaccurate for real pairs, but needed if only raw_returns passed)
        prev_close = 1.0

    cost_pct = cost_price_units / prev_close
    half_spread_cost = cost_pct * 0.5
    full_spread_cost = cost_pct

    # Also convert commission to pct
    commission_pct = commission / prev_close
    
    # Swap is already an absolute pct approximation or fraction in the static table,
    # but for accuracy, if the static table meant "units of price", it should also be / prev_close.
    # Looking at pipeline_utils, DAILY_SWAP_RATE is ~ -0.00003, which is tiny. 
    # For EUR_USD it's 0.003%. We'll leave swap as is since it's hardcoded as a small raw fraction.

    # Align signal changes with returns
    # raw_returns index is from 1 to len(signals)-1 (because of .dropna())
    # net_returns.index matches raw_returns.index
    # The return at i (which means period i-1 to i) was driven by signal[i-1]
    # The cost of changing from signal[i-1] to signal[i] should be deducted from return[i] (which is when we enter)
    # Actually, if we change from 0 at i-1 to 1 at i, we pay entry spread.
    # So the return at i+1 (period i to i+1) uses signal i.
    # Let's use boolean arrays for vectorized fast application.

    # Extract just the relevant signals (shift removes index 0 from returns)
    # signals_aligned contains [signal[1], signal[2], ... signal[n-1]]
    # prev_signals contains [signal[0], signal[1], ... signal[n-2]]
    # (Matches raw_returns shape)

    # It's safer to just do a loop or fast numpy mask on the signals series
    # pad with a 0 at start to represent "initial state = flat"
    s = signals.values
    s_prev = np.roll(s, 1)
    s_prev[0] = 0

    # We care about s_prev vs s
    # If s_prev == 0 and s == 1 -> Entry! Paid half spread.
    # If s_prev == 1 and s == 0 -> Exit! Paid half spread.
    # If s_prev == 1 and s == -1 -> Reversal! Paid full spread.

    is_entry = (s_prev == 0) & (s != 0)
    is_exit = (s_prev != 0) & (s == 0)
    is_reversal = (s_prev != 0) & (s != 0) & (s != s_prev)
    is_held = (s != 0)  # holding a position

    # We need to apply these costs to the *returns*.
    # If signal changes at i (so from s_prev[i] to s[i]), the return at i is
    # price_pct_change[i] * s_prev[i].
    # So the cost should be deducted at index i in the raw_returns.
    # raw_returns is indexed identically to signals, but dropna() removes index 0.
    # So raw_returns.loc[i] exists if i > 0.

    # Vectorized arrays (skip index 0)
    entry_mask = is_entry[1:]
    exit_mask = is_exit[1:]
    reversal_mask = is_reversal[1:]
    hold_mask = is_held[1:]

    # Modify net_returns using underlying numpy array for speed
    net_vals = net_returns.values

    # If half_spread_cost is an array, we must subset it using the mask
    # to avoid shape broadcast errors when assigning to net_vals[mask].
    is_array = isinstance(half_spread_cost, np.ndarray)

    # 1. Entry cost: half spread + full commission
    entry_deduct = (half_spread_cost[entry_mask] if is_array else half_spread_cost) + (commission_pct[entry_mask] if isinstance(commission_pct, np.ndarray) else commission_pct)
    net_vals[entry_mask] -= entry_deduct

    # 2. Exit cost: half spread
    exit_deduct = half_spread_cost[exit_mask] if is_array else half_spread_cost
    net_vals[exit_mask] -= exit_deduct

    # 3. Reversal: full spread (exit + entry) + commission
    rev_deduct = (full_spread_cost[reversal_mask] if is_array else full_spread_cost) + (commission_pct[reversal_mask] if isinstance(commission_pct, np.ndarray) else commission_pct)
    net_vals[reversal_mask] -= rev_deduct

    # 4. Swap: deducted per bar while in position
    net_vals[hold_mask] += daily_swap

    return net_returns


def compute_net_strategy_returns(
    data: pd.DataFrame,
    signals: pd.Series,
    instrument: str,
    granularity: str = 'D'
) -> pd.Series:
    """
    Compute net strategy returns with costs applied.

    Pipeline-friendly wrapper: computes raw returns then applies costs.

    Args:
        data: pd.DataFrame with 'close' column
        signals: pd.Series of positions (-1, 0, 1)
        instrument: e.g. 'EUR_USD'
        granularity: candle granularity

    Returns:
        pd.Series of net returns
    """
    raw = compute_strategy_returns(data, signals)
    if raw.empty:
        return raw
    return apply_trading_costs(raw, signals, instrument, granularity, data)


# ============================================================================
# FINGERPRINTING
# ============================================================================

def compute_strategy_fingerprint(code: str, param_grid: Dict, timeframe: str = 'D', instrument: str = '') -> str:
    """
    Compute SHA256 fingerprint of strategy code + param grid + timeframe + instrument.

    Args:
        code: Python source code string
        param_grid: dict of parameters
        timeframe: granularity string (default 'D')
        instrument: instrument symbol (e.g. 'EUR_USD', '' for legacy)

    Returns:
        SHA256 hex digest (lowercase)
    """
    param_json = json.dumps(param_grid, sort_keys=True)
    combined = code + param_json + timeframe + (instrument or '')
    return hashlib.sha256(combined.encode()).hexdigest()


# ============================================================================
# DATABASE OPERATIONS
# ============================================================================

DB_PATH = Path(__file__).parent / 'pipeline.db'


@contextmanager
def get_db_connection():
    """Context manager for database connection."""
    conn = sqlite3.connect(str(DB_PATH))
    conn.row_factory = sqlite3.Row
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def init_db() -> None:
    """Initialize database tables if not exist."""
    with get_db_connection() as conn:
        cursor = conn.cursor()
        
        # strategies table
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS strategies (
                id TEXT PRIMARY KEY,
                fingerprint TEXT UNIQUE NOT NULL,
                code TEXT NOT NULL,
                param_grid TEXT NOT NULL,
                rationale TEXT,
                timeframe TEXT NOT NULL DEFAULT 'D',
                status TEXT NOT NULL DEFAULT 'proposed',
                created_at TEXT NOT NULL
            )
        ''')
        
        # validation_results table
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS validation_results (
                strategy_id TEXT PRIMARY KEY REFERENCES strategies(id),
                best_params TEXT,
                is_gt_score REAL,
                walk_forward_gt_score REAL,
                holdout_gt_score REAL,
                final_status TEXT NOT NULL,
                tested_at TEXT NOT NULL,
                torture_flags TEXT DEFAULT '[]'
            )
        ''')
        # Migration: add torture_flags column to existing DBs.
        # Only swallow "duplicate column" — any other OperationalError (locked
        # DB, disk full, malformed) needs to be visible.
        try:
            cursor.execute("ALTER TABLE validation_results ADD COLUMN torture_flags TEXT DEFAULT '[]'")
        except sqlite3.OperationalError as e:
            if 'duplicate column' not in str(e).lower():
                raise
        
        # live_status table
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS live_status (
                strategy_id TEXT PRIMARY KEY REFERENCES strategies(id),
                start_date TEXT,
                equity_curve TEXT,
                current_gt_score REAL,
                last_updated TEXT,
                current_signal INTEGER DEFAULT 0
            )
        ''')
        # Migration: add columns to existing DBs
        for _col, _def in [
            ('current_signal',   'INTEGER DEFAULT 0'),
            ('current_position', 'INTEGER DEFAULT 0'),
            ('entry_price',      'REAL DEFAULT 0.0'),
            ('last_bar_time',    'TEXT DEFAULT NULL'),
            ('prev_signal',      'INTEGER DEFAULT 0'),
            ('oanda_trade_id',   'TEXT DEFAULT NULL'),
        ]:
            try:
                cursor.execute(f"ALTER TABLE live_status ADD COLUMN {_col} {_def}")
            except sqlite3.OperationalError as e:
                if 'duplicate column' not in str(e).lower():
                    raise
        
        # status_history table — audit trail for every status change
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS status_history (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                strategy_id TEXT NOT NULL REFERENCES strategies(id),
                old_status TEXT NOT NULL,
                new_status TEXT NOT NULL,
                reason TEXT,
                changed_at TEXT NOT NULL
            )
        ''')
        cursor.execute('''
            CREATE INDEX IF NOT EXISTS idx_status_history_sid
            ON status_history(strategy_id)
        ''')


def check_idea_is_new(fingerprint: str) -> Dict[str, Any]:
    """
    Check if strategy fingerprint already exists.
    
    Returns:
        {'new': True} if new, else {'new': False, 'status': <status>}
    """
    with get_db_connection() as conn:
        cursor = conn.cursor()
        cursor.execute('SELECT status FROM strategies WHERE fingerprint = ?', (fingerprint,))
        row = cursor.fetchone()
        
        if row is None:
            return {'new': True}
        else:
            return {'new': False, 'status': row['status']}


def insert_strategy(
    strategy_id: str,
    fingerprint: str,
    code: str,
    param_grid: Dict,
    rationale: str,
    timeframe: str = 'D'
) -> None:
    """Insert new proposed strategy."""
    with get_db_connection() as conn:
        cursor = conn.cursor()
        param_json = json.dumps(param_grid, sort_keys=True)
        now = datetime.utcnow().isoformat()

        cursor.execute('''
            INSERT INTO strategies (id, fingerprint, code, param_grid, rationale, timeframe, status, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        ''', (strategy_id, fingerprint, code, param_json, rationale, timeframe, 'proposed', now))

    _log_status_change(strategy_id, 'none', 'proposed', 'initial_submission')


def record_validation(
    strategy_id: str,
    best_params: Dict,
    is_score: Optional[float],
    wf_score: Optional[float],
    ho_score: Optional[float],
    final_status: str,
    torture_flags: Optional[List] = None
) -> None:
    """
    Record validation results and update strategy status.

    final_status: 'pass' or 'fail: <reason>'
    torture_flags: list of fragility flag strings (empty list = robust, None = not tested)
    """
    with get_db_connection() as conn:
        cursor = conn.cursor()
        now = datetime.utcnow().isoformat()
        best_params_json = json.dumps(best_params, sort_keys=True)
        torture_flags_json = json.dumps(torture_flags or [])

        # Fetch old status for audit trail
        cursor.execute('SELECT status FROM strategies WHERE id = ?', (strategy_id,))
        row = cursor.fetchone()
        old_status = row['status'] if row else 'unknown'

        # Insert validation result
        cursor.execute('''
            INSERT OR REPLACE INTO validation_results
            (strategy_id, best_params, is_gt_score, walk_forward_gt_score, holdout_gt_score, final_status, tested_at, torture_flags)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        ''', (strategy_id, best_params_json, is_score, wf_score, ho_score, final_status, now, torture_flags_json))

        # Update strategy status
        fl = final_status.lower()
        if 'pass' == fl or fl.startswith('pass'):
            # Fragile strategies get a distinct status so they aren't auto-promoted
            new_status = 'passed_but_fragile' if torture_flags else 'passed'
        elif 'holdout' in fl:
            new_status = 'holdout_failed'
        elif 'walk' in fl and 'forward' in fl:
            new_status = 'walk_forward_failed'
        elif 'in-sample' in fl or 'data fetch' in fl or 'code error' in fl or 'grid search' in fl:
            new_status = 'research_failed'
        elif fl.startswith('fail'):
            new_status = 'research_failed'
        else:
            new_status = 'proposed'
        
        cursor.execute('UPDATE strategies SET status = ? WHERE id = ?', (new_status, strategy_id))
    
    # Log status change
    _log_status_change(strategy_id, old_status, new_status, final_status)


def start_live_trading(strategy_id: str) -> None:
    """Initiate paper trading for a passed strategy."""
    with get_db_connection() as conn:
        cursor = conn.cursor()
        now = datetime.utcnow().isoformat()
        
        cursor.execute('SELECT status FROM strategies WHERE id = ?', (strategy_id,))
        row = cursor.fetchone()
        if row is None:
            raise ValueError(f'Strategy {strategy_id} not found')
        old_status = row['status']
        
        # Update strategy status
        cursor.execute('UPDATE strategies SET status = ? WHERE id = ?', ('paper_trading', strategy_id))
        
        # Insert live_status entry
        cursor.execute('''
            INSERT OR REPLACE INTO live_status (strategy_id, start_date, equity_curve, current_gt_score, last_updated)
            VALUES (?, ?, ?, ?, ?)
        ''', (strategy_id, now, '[]', 0.0, now))
    
    _log_status_change(strategy_id, old_status, 'paper_trading', 'deployed_for_live')


def update_live_metrics(
    strategy_id: str,
    equity_curve: List[Dict],
    current_gt_score: float
) -> None:
    """
    Update live trading metrics (append-only equity curve, rolling GT-Score).
    """
    with get_db_connection() as conn:
        cursor = conn.cursor()
        now = datetime.utcnow().isoformat()
        equity_json = json.dumps(equity_curve, sort_keys=True)
        
        cursor.execute('''
            UPDATE live_status 
            SET equity_curve = ?, current_gt_score = ?, last_updated = ?
            WHERE strategy_id = ?
        ''', (equity_json, current_gt_score, now, strategy_id))


def update_live_signal(strategy_id: str, signal: int) -> None:
    """
    Write the strategy's latest signal direction to live_status so peer traders
    can detect correlation conflicts before entering positions.

    signal: -1 (short), 0 (flat), +1 (long)
    """
    with get_db_connection() as conn:
        conn.execute(
            "UPDATE live_status SET current_signal = ? WHERE strategy_id = ?",
            (int(signal), strategy_id)
        )


def get_live_signals(strategy_ids: List[str]) -> Dict[str, int]:
    """
    Return {strategy_id: current_signal} for a list of peer strategy IDs.
    Missing rows get 0 (flat / unknown).
    """
    if not strategy_ids:
        return {}
    with get_db_connection() as conn:
        placeholders = ",".join("?" * len(strategy_ids))
        rows = conn.execute(
            f"SELECT strategy_id, current_signal FROM live_status WHERE strategy_id IN ({placeholders})",
            strategy_ids,
        ).fetchall()
    result = {sid: 0 for sid in strategy_ids}
    for row in rows:
        result[row[0]] = int(row[1] or 0)
    return result


def save_live_state(strategy_id: str, current_position: int, entry_price: float,
                    last_bar_time, prev_signal: int, oanda_trade_id: str = None) -> None:
    """Persist in-memory trader state to DB after every bar/order."""
    with get_db_connection() as conn:
        conn.execute(
            '''UPDATE live_status
               SET current_position=?, entry_price=?, last_bar_time=?,
                   prev_signal=?, oanda_trade_id=?
               WHERE strategy_id=?''',
            (current_position, entry_price,
             str(last_bar_time) if last_bar_time is not None else None,
             prev_signal, oanda_trade_id, strategy_id),
        )


def load_live_state(strategy_id: str) -> dict:
    """Load persisted trader state from DB. Returns safe defaults if no row exists."""
    with get_db_connection() as conn:
        conn.row_factory = sqlite3.Row
        row = conn.execute(
            'SELECT current_position, entry_price, last_bar_time, prev_signal, oanda_trade_id '
            'FROM live_status WHERE strategy_id=?',
            (strategy_id,),
        ).fetchone()
    if row:
        return dict(row)
    return {
        'current_position': 0,
        'entry_price': 0.0,
        'last_bar_time': None,
        'prev_signal': 0,
        'oanda_trade_id': None,
    }


def get_passed_strategies() -> List[Dict[str, Any]]:
    """Fetch all 'passed' strategies with validation results."""
    with get_db_connection() as conn:
        cursor = conn.cursor()
        cursor.execute('''
            SELECT s.id, s.code, s.param_grid, vr.best_params
            FROM strategies s
            LEFT JOIN validation_results vr ON s.id = vr.strategy_id
            WHERE s.status = 'passed'
        ''')
        
        results = []
        for row in cursor.fetchall():
            results.append({
                'id': row['id'],
                'code': row['code'],
                'param_grid': json.loads(row['param_grid']) if row['param_grid'] else {},
                'best_params': json.loads(row['best_params']) if row['best_params'] else {},
            })
        
        return results


def _log_status_change(strategy_id: str, old_status: str, new_status: str, reason: str = None) -> None:
    """Record status change in audit trail."""
    with get_db_connection() as conn:
        cursor = conn.cursor()
        now = datetime.utcnow().isoformat()
        cursor.execute('''
            INSERT INTO status_history (strategy_id, old_status, new_status, reason, changed_at)
            VALUES (?, ?, ?, ?, ?)
        ''', (strategy_id, old_status, new_status, reason, now))


def retire_strategy(strategy_id: str, reason: str = 'manual_retirement') -> None:
    """Mark a strategy as retired with audit trail."""
    with get_db_connection() as conn:
        cursor = conn.cursor()
        cursor.execute('SELECT status FROM strategies WHERE id = ?', (strategy_id,))
        row = cursor.fetchone()
        if row is None:
            raise ValueError(f'Strategy {strategy_id} not found')
        old_status = row['status']
        cursor.execute('UPDATE strategies SET status = ? WHERE id = ?', ('retired', strategy_id))
    _log_status_change(strategy_id, old_status, 'retired', reason)


def get_failed_strategies() -> List[Dict[str, Any]]:
    """Fetch all strategies that did NOT pass validation. Useful for auto-research loop."""
    with get_db_connection() as conn:
        cursor = conn.cursor()
        cursor.execute('''
            SELECT s.id, s.fingerprint, s.code, s.param_grid, s.rationale, s.status,
                   vr.final_status, vr.is_gt_score, vr.walk_forward_gt_score, vr.holdout_gt_score
            FROM strategies s
            LEFT JOIN validation_results vr ON s.id = vr.strategy_id
            WHERE s.status NOT IN ('passed', 'paper_trading', 'live')
            ORDER BY s.created_at DESC
        ''')
        results = []
        for row in cursor.fetchall():
            results.append({
                'id': row['id'],
                'fingerprint': row['fingerprint'],
                'code': row['code'],
                'param_grid': json.loads(row['param_grid']) if row['param_grid'] else {},
                'rationale': row['rationale'],
                'status': row['status'],
                'final_status': row['final_status'],
                'is_gt_score': row['is_gt_score'],
                'wf_gt_score': row['walk_forward_gt_score'],
                'ho_gt_score': row['holdout_gt_score'],
            })
        return results


def get_all_strategies(status_filter: str = None) -> List[Dict[str, Any]]:
    """Fetch all strategies, optionally filtered by status."""
    with get_db_connection() as conn:
        cursor = conn.cursor()
        if status_filter:
            cursor.execute('''
                SELECT id, fingerprint, code, param_grid, rationale, status, created_at
                FROM strategies WHERE status = ? ORDER BY created_at DESC
            ''', (status_filter,))
        else:
            cursor.execute('''
                SELECT id, fingerprint, code, param_grid, rationale, status, created_at
                FROM strategies ORDER BY created_at DESC
            ''')
        results = []
        for row in cursor.fetchall():
            results.append({
                'id': row['id'],
                'fingerprint': row['fingerprint'],
                'code': row['code'],
                'param_grid': json.loads(row['param_grid']) if row['param_grid'] else {},
                'rationale': row['rationale'],
                'status': row['status'],
                'created_at': row['created_at'],
            })
        return results


def get_strategy_status_history(strategy_id: str) -> List[Dict[str, Any]]:
    """Return full audit trail of status changes for a strategy."""
    with get_db_connection() as conn:
        cursor = conn.cursor()
        cursor.execute('''
            SELECT old_status, new_status, reason, changed_at
            FROM status_history
            WHERE strategy_id = ?
            ORDER BY changed_at ASC
        ''', (strategy_id,))
        return [dict(row) for row in cursor.fetchall()]


def get_strategy_by_id(strategy_id: str) -> Dict[str, Any]:
    """Fetch strategy details by ID."""
    with get_db_connection() as conn:
        cursor = conn.cursor()
        cursor.execute('''
            SELECT s.*, vr.best_params
            FROM strategies s
            LEFT JOIN validation_results vr ON s.id = vr.strategy_id
            WHERE s.id = ?
        ''', (strategy_id,))
        
        row = cursor.fetchone()
        if row is None:
            return {}
        
        return {
            'id': row['id'],
            'code': row['code'],
            'param_grid': json.loads(row['param_grid']) if row['param_grid'] else {},
            'best_params': json.loads(row['best_params']) if row['best_params'] else {},
            'status': row['status'],
            'rationale': row['rationale'],
            'timeframe': row['timeframe'] or 'D',
        }


# Instrument decimal precision for order placement
_INSTRUMENT_DECIMALS = {
    'EUR_USD': 5, 'GBP_USD': 5, 'AUD_USD': 5, 'NZD_USD': 5,
    'USD_CAD': 5, 'USD_CHF': 5, 'EUR_GBP': 5, 'EUR_AUD': 5,
    'EUR_CAD': 5, 'EUR_CHF': 5, 'GBP_AUD': 5, 'GBP_CAD': 5,
    'GBP_CHF': 5, 'GBP_NZD': 5, 'AUD_CAD': 5, 'AUD_CHF': 5,
    'AUD_NZD': 5, 'CAD_CHF': 5, 'NZD_CAD': 5, 'NZD_CHF': 5,
    'USD_JPY': 3, 'EUR_JPY': 3, 'GBP_JPY': 3, 'AUD_JPY': 3,
    'NZD_JPY': 3, 'CAD_JPY': 3, 'CHF_JPY': 3, 'EUR_NZD': 5,
    'XAU_USD': 2, 'XAG_USD': 4, 'BCO_USD': 3, 'WTICO_USD': 3,
    'NATGAS_USD': 4, 'CORN_USD': 4, 'SOYBN_USD': 4, 'WHEAT_USD': 4,
    'SPX500_USD': 1, 'US30_USD': 1, 'US100_USD': 1, 'US500_USD': 1,
    'BTC_USD': 2, 'ETH_USD': 2, 'LTC_USD': 2,
}

def get_price_decimals(instrument: str) -> int:
    """Return decimal precision for an instrument's price."""
    return _INSTRUMENT_DECIMALS.get(instrument, 5)
