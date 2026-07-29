"""
Pipeline Utilities: Core functions for strategy research, validation, and live testing.
Handles GT-Score calculation, grid search, walk-forward analysis, and database operations.
"""

import json
import hashlib
import signal
import sys
import time
import sqlite3
from datetime import datetime, timedelta
from pathlib import Path
from typing import Dict, List, Tuple, Any, Optional
import warnings
import pandas as pd
import numpy as np
from contextlib import contextmanager

try:
    from reason_codes import classify as _classify_reason
except ImportError:
    _classify_reason = None

# Suppress noisy FutureWarnings from pandas fillna/ffill downcasting (cosmetic, not
# functional). NB: do NOT restrict module= — these warnings are raised from inside
# exec'd LLM-generated strategy code (module '<string>'), so a module='pandas' filter
# never matches and the warnings leak (28k lines/day observed 2026-05-31).
warnings.filterwarnings('ignore', category=FutureWarning)

# ============================================================================
# STRATEGY EXECUTION TIMEOUT
# Prevents AI-generated infinite loops from freezing the pipeline.
# ============================================================================

_STRATEGY_CALL_TIMEOUT = 30  # seconds per strategy_func(data, params) call

# Wall-clock budget for an ENTIRE grid_search combo sweep. The per-call SIGALRM
# above only catches a single hung call; a strategy that is merely slow (e.g.
# 10s/call) never trips it but, summed across ~200 combos × 5 walk-forward
# windows, runs for over an hour and stalls the batch until the watchdog kills
# it. This budget aborts the whole sweep so the validator fails it cleanly.
_GRID_SEARCH_BUDGET = 60  # seconds for one full grid_search() call


def _timeout_handler(signum, frame):
    raise TimeoutError(f"Strategy call exceeded {_STRATEGY_CALL_TIMEOUT}s timeout")


# ============================================================================
# GT-SCORE CALCULATION (Alexander Sheppert methodology)
# ============================================================================

# Fixed ceiling on the Sortino term. Previously this was max(5.0, |sharpe|*10),
# which coupled the cap to Sharpe: on a low-drawdown window the raw Sortino
# blows up, saturates the 10x cap, and — because Sortino is double-weighted —
# collapses the whole score to ~7x Sharpe (a 30-bar winning streak at Sharpe ~2.2
# read GT ~15.8). That inflation only bit SHORT low-loss windows; on validation-
# scale windows (~1y) the raw Sortino never reaches 5, so decoupling to a fixed
# cap leaves every stored IS/WF/HO score identical (verified: |Δ|=0 across the
# live book) while bounding the pathological live case to ~4.2.
SORTINO_CAP = 5.0

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

    # 4. Sortino — cap at a FIXED SORTINO_CAP to prevent blow-up when all
    #    negative returns happen to be near-identical tiny values (std → 0).
    #    Fixed (not |sharpe|*10) so the term can't scale with Sharpe and inflate
    #    short low-loss windows — see SORTINO_CAP note above.
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

    sortino = max(-SORTINO_CAP, min(SORTINO_CAP, sortino))

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
    
    _deadline = time.monotonic() + _GRID_SEARCH_BUDGET

    for params in generate_combos(param_names, param_values):
        # Wall-clock backstop: a slow-but-finite strategy never trips the
        # per-call SIGALRM, but its cumulative cost across combos can be huge.
        if time.monotonic() > _deadline:
            raise TimeoutError(
                f"Grid search exceeded {_GRID_SEARCH_BUDGET}s wall-clock budget "
                f"(strategy too slow across param combos)"
            )
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
                returns = compute_net_strategy_returns(data, signals, instrument, granularity, params=params)
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
                test_returns = compute_net_strategy_returns(test_data, test_signals, instrument, granularity, params=best_params)
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
            returns = compute_net_strategy_returns(data, signals, instrument, granularity, params=params)
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
    # Crypto spreads are DOLLARS-scale, not cents (pip-value 0.01): the old
    # 50/100-pip entries modeled $0.50 / $1.00 — ~85x under live OANDA spreads
    # ($43 / $3.00 measured 2026-06-11), so crypto validations were near-free.
    'BTC_USD': 4300.0,   # ~$43  -> ~0.07% RT at ~$62k
    'ETH_USD': 300.0,    # ~$3   -> ~0.18% RT at ~$1.6k
    'BCO_USD': 4.0,    # brent crude
    'WTICO_USD': 4.0,  # WTI crude
    'CORN_USD': 1.0,   # corn: live OANDA spread ~1.0 pip / ~0.47% RT (2026-07-06). Was 3.0
                       #   — an uncalibrated 3x-too-high placeholder that made EVERY corn
                       #   strategy lose money after costs -> 90% IS=0 (424/468). Matches WHEAT/SOYBN.
    'NATGAS_USD': 1.0, # natural gas: live spread ~0.6 pip; was 3.0 (5x too high). 1.0 keeps a
                       #   small buffer for its high vol (spreads widen in stress).
    # --- Pool-expansion: equity indices ---------------------------------
    # Spread is quoted in INDEX POINTS (pip-value 1.0 below). The trailing
    # comment is the resulting round-trip cost as a % of price at recent
    # levels — these instruments were previously absent and silently fell
    # back to forex defaults (2.0 pips x 0.0001 ~ 0% on a ~25,000 index),
    # so every index strategy was validated at near-zero spread.
    'SPX500_USD': 0.8,   # ~0.011% RT  (S&P 500 ~7360)
    'NAS100_USD': 2.0,   # ~0.007% RT  (Nasdaq 100 ~28800)
    'US30_USD':   3.0,   # ~0.006% RT  (Dow ~50700)
    'DE30_EUR':   1.8,   # ~0.007% RT  (DAX ~24600)
    'UK100_GBP':  1.5,   # ~0.015% RT  (FTSE ~10340)
    'JP225_USD':  12.0,  # ~0.019% RT  (Nikkei ~63800)
    'AU200_AUD':  1.8,   # ~0.021% RT  (ASX 200 ~8500)
    'HK33_HKD':   11.0,  # ~0.045% RT  (Hang Seng ~24600)
    'CN50_USD':   13.0,  # ~0.085% RT  (China A50 ~15300, illiquid/wide)
    # --- Pool-expansion: other metals (pip-value as noted below) --------
    'XCU_USD':  25.0,    # ~0.040% RT  (copper ~6.22, pip 0.0001)
    'XPT_USD':  250.0,   # ~0.142% RT  (platinum ~1764, pip 0.01, illiquid)
    'XPD_USD':  350.0,   # ~0.289% RT  (palladium ~1213, pip 0.01, illiquid)
    # --- Pool-expansion: crypto -----------------------------------------
    'LTC_USD':  18.0,    # ~0.412% RT  (litecoin ~43.7, pip 0.01)
    # --- Pool-expansion: grains -----------------------------------------
    'WHEAT_USD': 0.7,    # ~0.122% RT  (wheat ~5.75, pip 0.01)
    'SOYBN_USD': 1.0,    # ~0.090% RT  (soybean ~11.12, pip 0.01)
    # --- Pool-expansion: FX crosses (were on forex default already, but
    #     JPY crosses need pip 0.01, not 0.0001, or cost rounds to ~0) ---
    'EUR_GBP': 1.5,      # ~0.017% RT  (~0.864)
    'EUR_JPY': 2.0,      # ~0.011% RT  (~184.7, pip 0.01)
    'GBP_JPY': 3.0,      # ~0.014% RT  (~213.9, pip 0.01)
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
    # Pool-expansion: indices priced in points -> pip-value 1.0 so the
    # spread above is read as index points (e.g. HK33 11 pts * 1.0 = 11).
    'SPX500_USD': 1.0, 'NAS100_USD': 1.0, 'US30_USD': 1.0, 'DE30_EUR': 1.0,
    'UK100_GBP': 1.0, 'JP225_USD': 1.0, 'AU200_AUD': 1.0,
    'HK33_HKD': 1.0, 'CN50_USD': 1.0,
    # Other metals / crypto / grains
    'XCU_USD': 0.0001, 'XPT_USD': 0.01, 'XPD_USD': 0.01,
    'LTC_USD': 0.01,
    'WHEAT_USD': 0.01, 'SOYBN_USD': 0.01,
    # JPY crosses need 0.01 (1 pip = 0.01), not the 0.0001 forex default
    'EUR_JPY': 0.01, 'GBP_JPY': 0.01,
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
    # CORN/NATGAS were 0.10 — a flat $0.10/unit commission that is trivial on a
    # $2000 instrument (gold) but is ~2.4% PER TRADE on a ~$4 grain, which made
    # every strategy lose money after costs (the real cause of the 90% IS=0
    # wall, 424/468 corn). OANDA charges no separate commission on these CFDs
    # (cost is in the spread) — 0 like WHEAT/SOYBN. 2026-07-06.
    'CORN_USD': 0.0,
    'NATGAS_USD': 0.0,
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
    # Equity-index CFDs charge daily financing (~benchmark rate + ~2.5%
    # admin) on the full notional. Modelled as a per-day fraction; applied
    # symmetrically (conservative — slightly over-penalises short-index
    # strategies). Previously 0, so index strategies held positions free.
    # ~0.00018/day ~= 6.5%/yr (USD funding ~4.3% + 2.5% admin).
    'SPX500_USD': -0.00018, 'NAS100_USD': -0.00018, 'US30_USD': -0.00018,
    'JP225_USD': -0.00016, 'AU200_AUD': -0.00016, 'CN50_USD': -0.00018,
    'HK33_HKD': -0.00018,
    'DE30_EUR': -0.00012,   # EUR funding lower (~2% + 2.5% admin)
    'UK100_GBP': -0.00018,  # GBP funding similar to USD
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
    Subtract realistic trading costs from raw returns using the static
    per-instrument spread model (get_spread_pips).
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

    # Static per-instrument spread
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


# Coarse ATR-stop multiplier sweep auto-injected into every search grid so the
# optimizer co-optimizes the stop with the strategy params. Widest (loosest)
# first per the loosest-first convention — a wider stop fires fewer stop-outs.
# Kept deliberately coarse: the stop is a risk overlay, not an edge dial;
# fine-optimizing it would worsen overfitting. atr_window is NOT swept (fixed
# at the live default of 14 unless the strategy already defines it).
STOP_MULT_SWEEP = [3.0, 2.0, 1.5]
DEFAULT_ATR_WINDOW = 14


def compute_returns_with_stop(
    data: pd.DataFrame,
    signals: pd.Series,
    stop_mult: float,
    atr_window: int = DEFAULT_ATR_WINDOW,
):
    """
    Bar-level returns with the LIVE ATR stop-loss modeled, so validation scores
    the strategy that is actually traded (see live_test `_place_order`).

    Mirrors live behaviour:
      - stop placed at entry: long = entry - mult*ATR, short = entry + mult*ATR
      - ATR = rolling mean of true range over `atr_window`
      - intrabar trigger (low<=stop for long, high>=stop for short), filled at
        the stop price
      - after a stop-out, stay FLAT until the signal value changes (no re-entry
        on a continuously-held signal — matches the live no-flip-no-order rule)

    Returns:
        (gross_returns, held_positions)
        gross_returns  — pd.Series length n-1 (matches compute_strategy_returns)
        held_positions — pd.Series length n; the position actually held each bar
                         (goes flat after a stop) for correct cost accounting
    """
    s = signals.reset_index(drop=True).values.astype(float)
    close = data['close'].values
    low = data['low'].values
    high = data['high'].values
    n = len(data)
    if n < 2:
        return pd.Series(dtype=float), pd.Series(np.zeros(n))

    pc = np.roll(close, 1); pc[0] = close[0]
    tr = np.maximum(high - low, np.maximum(np.abs(high - pc), np.abs(low - pc)))
    atr = pd.Series(tr).rolling(int(atr_window)).mean().values

    out = np.zeros(n)
    held = np.zeros(n)
    i = 1
    while i < n:
        pos = s[i - 1]
        if pos == 0:
            i += 1
            continue
        entry = close[i - 1]
        a = atr[i - 1]
        stop = None if (np.isnan(a) or a <= 0) else (
            entry - stop_mult * a if pos > 0 else entry + stop_mult * a)
        stopped = False
        while i < n and s[i - 1] == pos:
            prev_c = close[i - 1]
            if stopped:
                held[i] = 0.0
                out[i] = 0.0
                i += 1
                continue
            held[i] = pos
            if stop is not None and ((pos > 0 and low[i] <= stop) or (pos < 0 and high[i] >= stop)):
                out[i] = (stop - prev_c) / prev_c if pos > 0 else (prev_c - stop) / prev_c
                stopped = True
            else:
                br = (close[i] - prev_c) / prev_c
                out[i] = br if pos > 0 else -br
            i += 1
    return pd.Series(out[1:]).reset_index(drop=True), pd.Series(held)


def compute_net_strategy_returns(
    data: pd.DataFrame,
    signals: pd.Series,
    instrument: str,
    granularity: str = 'D',
    params: Dict = None,
) -> pd.Series:
    """
    Compute net strategy returns with costs applied.

    Pipeline-friendly wrapper: computes raw returns then applies costs.

    If `params` carries a 'stop_mult', the live ATR stop is modeled (so the
    validated return stream matches what is actually traded); otherwise the
    legacy no-stop calc is used. Backward compatible: params=None → old path.

    Note: trading costs are applied on the post-stop position series, so the
    stop-induced exit IS charged a spread; this is the faithful net stream.
    """
    stop_mult = params.get('stop_mult') if params else None
    if stop_mult:
        atr_window = int(params.get('atr_window', DEFAULT_ATR_WINDOW))
        gross, held = compute_returns_with_stop(data, signals, stop_mult, atr_window)
        if gross.empty:
            return gross
        return apply_trading_costs(gross, held, instrument, granularity, data)

    raw = compute_strategy_returns(data, signals)
    if raw.empty:
        return raw
    return apply_trading_costs(raw, signals, instrument, granularity, data)


# ============================================================================
# FINGERPRINTING
# ============================================================================

def compute_strategy_fingerprint(code: str, param_grid: Dict, timeframe: str = 'D',
                                  instrument: str = '', archetype: str = 'standard') -> str:
    """
    Compute SHA256 fingerprint of strategy code + param grid + timeframe +
    instrument + (optional) archetype.

    Backward-compat: archetype='standard' produces the same hash as the
    legacy 4-arg form, so existing fingerprints in the DB remain valid.
    Only non-standard archetypes (macro/session/news/pair) get a distinct
    hash, which prevents the previous collision where two strategies with
    identical code but different supplementary-data archetypes deduped.

    Args:
        code: Python source code string
        param_grid: dict of parameters
        timeframe: granularity string (default 'D')
        instrument: instrument symbol (e.g. 'EUR_USD', '' for legacy)
        archetype: data archetype ('standard'/'macro'/'session'/'news'/'pair')

    Returns:
        SHA256 hex digest (lowercase)
    """
    param_json = json.dumps(param_grid, sort_keys=True)
    combined = code + param_json + timeframe + (instrument or '')
    if archetype and archetype != 'standard':
        combined += '|' + archetype
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


# Canonical DDL for the append-only lifecycle store. migrations/001_lifecycle.sql
# and 002_seal.sql were the one-time application to the existing research DB and
# are kept as the record of that; THIS constant is the source of truth from here
# on, so a DB created anywhere gets the tables automatically.
#
# Without it the schema lived only in hand-applied .sql files, so a fresh
# pipeline.db would silently lack these tables: _log_status_change would hit the
# missing-table path, warn, and let the status change succeed while events
# quietly stopped being recorded — the same shape as the live_status rows that
# went missing for five sleeves.
#
# Everything is IF NOT EXISTS, so this is safe to run on every startup.
#
# Sealing here is deliberate: the triggers block UPDATE/DELETE but NOT INSERT, so
# a backfill still works against a sealed table. The one-time ordering caution in
# 002_seal.sql (seal AFTER the backfill) is about being able to REPAIR a botched
# 148k-row classification, not about correctness.
LIFECYCLE_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS evaluations (
    id INTEGER PRIMARY KEY,
    strategy_id TEXT NOT NULL,
    run_at TEXT NOT NULL,
    window_start TEXT NOT NULL,
    window_end TEXT NOT NULL,
    recent_gt REAL, gt_floor REAL, decay_status TEXT, near_miss INTEGER,
    entries_in_window INTEGER, entries_lifetime INTEGER, capped_by TEXT,
    r12 REAL, sharpe REAL, maxdd REAL, inmkt REAL, tot_return REAL,
    verdict TEXT,
    source TEXT NOT NULL DEFAULT 'live',
    FOREIGN KEY (strategy_id) REFERENCES strategies(id)
);
CREATE INDEX IF NOT EXISTS idx_evaluations_sid_run ON evaluations (strategy_id, run_at);

CREATE TABLE IF NOT EXISTS strategy_events (
    id INTEGER PRIMARY KEY,
    strategy_id TEXT NOT NULL,
    occurred_at TEXT NOT NULL,
    old_status TEXT,
    new_status TEXT,
    reason_code TEXT NOT NULL,
    reason_prose TEXT,
    source TEXT NOT NULL DEFAULT 'live',
    history_id INTEGER,
    FOREIGN KEY (strategy_id) REFERENCES strategies(id)
);
CREATE INDEX IF NOT EXISTS idx_events_sid_at ON strategy_events (strategy_id, occurred_at);
CREATE INDEX IF NOT EXISTS idx_events_reason ON strategy_events (reason_code);
CREATE UNIQUE INDEX IF NOT EXISTS idx_events_history_id ON strategy_events (history_id);

CREATE TABLE IF NOT EXISTS sleeve_equity (
    id INTEGER PRIMARY KEY,
    sleeve_id TEXT NOT NULL,
    bar_time TEXT NOT NULL,
    own_units REAL,
    price REAL,
    sleeve_pnl REAL,
    written_at TEXT,
    UNIQUE (sleeve_id, bar_time)
);
CREATE INDEX IF NOT EXISTS idx_sleeve_equity_sid_bar ON sleeve_equity (sleeve_id, bar_time);

CREATE TRIGGER IF NOT EXISTS evaluations_no_update BEFORE UPDATE ON evaluations
BEGIN SELECT RAISE(ABORT, 'evaluations is append-only: UPDATE refused. Append a new evaluation instead.'); END;
CREATE TRIGGER IF NOT EXISTS evaluations_no_delete BEFORE DELETE ON evaluations
BEGIN SELECT RAISE(ABORT, 'evaluations is append-only: DELETE refused.'); END;

CREATE TRIGGER IF NOT EXISTS strategy_events_no_update BEFORE UPDATE ON strategy_events
BEGIN SELECT RAISE(ABORT, 'strategy_events is append-only: UPDATE refused. Append a correcting event instead.'); END;
CREATE TRIGGER IF NOT EXISTS strategy_events_no_delete BEFORE DELETE ON strategy_events
BEGIN SELECT RAISE(ABORT, 'strategy_events is append-only: DELETE refused.'); END;

CREATE TRIGGER IF NOT EXISTS sleeve_equity_no_update BEFORE UPDATE ON sleeve_equity
BEGIN SELECT RAISE(ABORT, 'sleeve_equity is append-only: UPDATE refused. A restart must never rewrite stored bars.'); END;
CREATE TRIGGER IF NOT EXISTS sleeve_equity_no_delete BEFORE DELETE ON sleeve_equity
BEGIN SELECT RAISE(ABORT, 'sleeve_equity is append-only: DELETE refused.'); END;
"""


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

        # Strategy metadata needed by non-standard archetypes at deployment
        # time. Older rows did not persist these fields; keep them nullable and
        # let callers infer where possible.
        for _col, _def in [
            ('instrument',  'TEXT'),
            ('archetype',   "TEXT DEFAULT 'standard'"),
            ('instrument2', 'TEXT'),
        ]:
            try:
                cursor.execute(f"ALTER TABLE strategies ADD COLUMN {_col} {_def}")
            except sqlite3.OperationalError as e:
                if 'duplicate column' not in str(e).lower():
                    raise

        cursor.execute('CREATE INDEX IF NOT EXISTS idx_strategies_status ON strategies(status)')

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

        # Append-only lifecycle store (evaluations / strategy_events / sleeve_equity).
        # Idempotent, so this is a no-op on a DB that already has them.
        cursor.executescript(LIFECYCLE_SCHEMA_SQL)


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
    timeframe: str = 'D',
    instrument: str = '',
    archetype: str = 'standard',
    instrument2: str = ''
) -> None:
    """Insert new proposed strategy."""
    with get_db_connection() as conn:
        cursor = conn.cursor()
        param_json = json.dumps(param_grid, sort_keys=True)
        now = datetime.utcnow().isoformat()

        cursor.execute('''
            INSERT INTO strategies (
                id, fingerprint, code, param_grid, rationale, timeframe,
                instrument, archetype, instrument2, status, created_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ''', (
            strategy_id, fingerprint, code, param_json, rationale, timeframe,
            instrument or None, archetype or 'standard', instrument2 or None,
            'proposed', now,
        ))

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


# The two books read DIFFERENT statuses, and that difference is the whole gate:
#   INCUBATING     -> OANDA paper book only (run_paper_trading.sh)
#   PAPER_TRADING  -> paper book AND the live prop account (fix_runner.load_sleeves)
# So a sleeve must EARN promotion out of INCUBATING before it can risk real money.
# Everything else — portfolio.py, build_deploy_db.py — keys on PAPER_TRADING and is
# therefore unaffected by an incubating sleeve, which is what keeps incubation
# observe-only: n does not move, so no live weight moves.
INCUBATING = 'incubating'
PAPER_TRADING = 'paper_trading'


def _activate(strategy_id: str, new_status: str, reason: str) -> None:
    """Set a strategy's status and create its live_status row, atomically."""
    with get_db_connection() as conn:
        cursor = conn.cursor()
        now = datetime.utcnow().isoformat()

        cursor.execute('SELECT status FROM strategies WHERE id = ?', (strategy_id,))
        row = cursor.fetchone()
        if row is None:
            raise ValueError(f'Strategy {strategy_id} not found')
        old_status = row['status']

        cursor.execute('UPDATE strategies SET status = ? WHERE id = ?', (new_status, strategy_id))

        # INSERT OR REPLACE, not UPDATE: a missing live_status row is how five
        # sleeves ended up trading correctly while writing metrics into the void.
        cursor.execute('''
            INSERT OR REPLACE INTO live_status (strategy_id, start_date, equity_curve, current_gt_score, last_updated)
            VALUES (?, ?, ?, ?, ?)
        ''', (strategy_id, now, '[]', 0.0, now))

    _log_status_change(strategy_id, old_status, new_status, reason)


def start_incubation(strategy_id: str) -> None:
    """Begin OBSERVE-ONLY incubation: paper book yes, prop account no.

    This is the entry point for a newly passed strategy. It deliberately does NOT
    reach the prop book — incubation.py compares the sleeve's live bar returns
    against its own reconstruction, and a sleeve should demonstrate it does what
    its code says before any real capital is at risk.

    Sizing needs no special handling: an incubating sleeve is absent from
    portfolio_state.json, and _load_portfolio_state falls back to
    weights.get(sid, 1.0/n) * n == 1.0, i.e. the equal-weight baseline. That is
    also why incubation cannot perturb the live book.
    """
    _activate(strategy_id, INCUBATING, 'incubation_started')


def promote_sleeve(strategy_id: str) -> None:
    """Promote incubating -> paper_trading, i.e. onto the LIVE prop account.

    This is a real deploy and everything the deploy procedure says still applies:
    n rises, cluster caps tighten (cap_frac = CLUSTER_CAP/n, which does NOT
    renormalise), so every remaining sleeve's weight moves. Rebuild
    portfolio_state.json and re-check deployed risk afterwards.
    """
    with get_db_connection() as conn:
        row = conn.execute('SELECT status FROM strategies WHERE id = ?',
                           (strategy_id,)).fetchone()
    if row is None:
        raise ValueError(f'Strategy {strategy_id} not found')
    if row['status'] != INCUBATING:
        # Refuse rather than silently re-deploying something already live, or
        # promoting a retired/failed strategy straight onto the prop account.
        raise ValueError(
            f'{strategy_id} is {row["status"]!r}, not {INCUBATING!r} — '
            'only an incubating sleeve can be promoted')

    _activate(strategy_id, PAPER_TRADING, 'promoted_from_incubation')


def start_live_trading(strategy_id: str) -> None:
    """Deploy straight to the live prop account, skipping incubation.

    Kept for the existing deploy path and for grandfathered sleeves. Prefer
    start_incubation() -> promote_sleeve() for anything new: this function puts
    real capital behind a strategy that has never been observed live.
    """
    _activate(strategy_id, PAPER_TRADING, 'deployed_for_live')


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

        # A bare UPDATE is a silent no-op when the row is missing, and the row
        # is only ever created by start_paper_trading() — so a sleeve activated
        # by any other path trades correctly but writes its metrics into the
        # void, and drops out of the report's sleeve count. Insert it instead.
        if cursor.rowcount == 0:
            cursor.execute('''
                INSERT INTO live_status
                    (strategy_id, start_date, equity_curve, current_gt_score, last_updated)
                VALUES (?, ?, ?, ?, ?)
            ''', (strategy_id, now, equity_json, current_gt_score, now))


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

        try:
            reason_code = _classify_reason(new_status, reason) if _classify_reason else 'UNCLASSIFIED'
            cursor.execute('''
                INSERT INTO strategy_events (strategy_id, occurred_at, old_status, new_status, reason_code, reason_prose, source, history_id)
                VALUES (?, ?, ?, ?, ?, ?, 'live', NULL)
            ''', (strategy_id, now, old_status, new_status, reason_code, reason))
        except Exception as e:
            # Never let the event write break or roll back the status change itself —
            # status_history is the system of record. But do NOT swallow silently:
            # a dual-write that quietly stops is indistinguishable from one that was
            # never called, which is exactly how live_status rows went missing for
            # five sleeves without anyone noticing.
            print(f"WARNING: strategy_events write failed for {strategy_id}: {e}",
                  file=sys.stderr)


def flatten_sleeve(strategy_id: str) -> Dict[str, Any]:
    """Close the broker units a sleeve owns under NETTING, then clear its book row.

    Under NETTING each sleeve holds its own share of the instrument's net
    position (sleeve_units) and only ever sends its own delta. So a sleeve whose
    live_test process stops — which is what retirement does — strands that share:
    nothing revises it and its software stop (evaluated per-bar inside the loop,
    since netted positions carry no broker-side stop) is never checked again.

    Returns {'units': <closed>, 'price': ..., 'pl': ...}; 'units' is 0.0 when the
    sleeve owned nothing. Raises RuntimeError if the close does not fill — the
    caller MUST leave the sleeve running in that case (see retire_strategy).
    """
    import os
    import requests

    with get_db_connection() as conn:
        conn.execute('CREATE TABLE IF NOT EXISTS sleeve_units'
                     '(sleeve_id TEXT PRIMARY KEY, units REAL, stop REAL)')
        row = conn.execute('SELECT units FROM sleeve_units WHERE sleeve_id = ?',
                           (strategy_id,)).fetchone()
    units = float(row['units']) if row and row['units'] else 0.0

    if units:
        from live_test import _get_instrument_sizing
        from portfolio import _infer_instrument

        account = os.getenv('OANDA_ACCOUNT_ID', '')
        token = os.getenv('OANDA_API_TOKEN', '')
        if not account or not token:
            raise RuntimeError(f'{strategy_id}: cannot flatten {units:+.4f} units — '
                               'OANDA_ACCOUNT_ID / OANDA_API_TOKEN not set')
        instrument = _infer_instrument(strategy_id)
        precision = _get_instrument_sizing(instrument)['unit_precision']
        resp = requests.post(
            f'https://api-fxpractice.oanda.com/v3/accounts/{account}/orders',
            headers={'Authorization': f'Bearer {token}', 'Content-Type': 'application/json'},
            json={'order': {'instrument': instrument,
                            'units': f'{-units:.{precision}f}',
                            'type': 'MARKET',
                            'timeInForce': 'FOK',
                            # REDUCE_ONLY so a stale sleeve_units row can never
                            # flip the instrument's net position the other way.
                            'positionFill': 'REDUCE_ONLY',
                            'tradeClientExtensions': {'comment': f'retire:{strategy_id}'[:128]}}},
            timeout=15)
        resp.raise_for_status()
        data = resp.json()
        fill = data.get('orderFillTransaction')
        if not fill:
            # MARKET_HALTED is the common one (weekend / broker maintenance) and
            # is exactly how sleeves got stranded before — so refuse, loudly.
            cancel_reason = (data.get('orderCancelTransaction') or {}).get('reason', 'no fill')
            raise RuntimeError(f'{strategy_id}: flatten of {units:+.4f} {instrument} '
                               f'REJECTED ({cancel_reason}) — sleeve left running')
        result = {'units': units, 'price': fill.get('price'), 'pl': fill.get('pl')}
    else:
        result = {'units': 0.0, 'price': None, 'pl': None}

    with get_db_connection() as conn:
        conn.execute('DELETE FROM sleeve_units WHERE sleeve_id = ?', (strategy_id,))
        conn.execute('UPDATE live_status SET current_position = 0, entry_price = 0.0 '
                     'WHERE strategy_id = ?', (strategy_id,))
    return result


def retire_strategy(strategy_id: str, reason: str = 'manual_retirement',
                    flatten: bool = True, force: bool = False) -> None:
    """Mark a strategy as retired with audit trail, flattening it first.

    Retirement stops the sleeve's live_test process, so any position it still
    owns becomes unmanaged and unstopped. Flatten BEFORE flipping the status, and
    abort the retirement if the close doesn't fill — a still-running sleeve is
    strictly safer than a stranded one. Pass force=True to retire anyway (the
    residual is recorded in the audit trail); flatten=False skips the close for
    sleeves known to be flat.
    """
    with get_db_connection() as conn:
        cursor = conn.cursor()
        cursor.execute('SELECT status FROM strategies WHERE id = ?', (strategy_id,))
        row = cursor.fetchone()
        if row is None:
            raise ValueError(f'Strategy {strategy_id} not found')
        old_status = row['status']

    if flatten:
        try:
            closed = flatten_sleeve(strategy_id)
            if closed['units']:
                reason = f"{reason} | flattened {closed['units']:+.4f} @ {closed['price']} pl={closed['pl']}"
        except Exception as exc:
            if not force:
                raise
            reason = f'{reason} | FLATTEN FAILED, exposure stranded: {exc}'

    with get_db_connection() as conn:
        conn.execute('UPDATE strategies SET status = ? WHERE id = ?', ('retired', strategy_id))
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
            'instrument': row['instrument'] if 'instrument' in row.keys() else None,
            'archetype': row['archetype'] if 'archetype' in row.keys() else 'standard',
            'instrument2': row['instrument2'] if 'instrument2' in row.keys() else None,
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
    # Pool-expansion indices (OANDA displayPrecision, verified via instruments API)
    'NAS100_USD': 1, 'DE30_EUR': 1, 'UK100_GBP': 1, 'JP225_USD': 1,
    'AU200_AUD': 1, 'HK33_HKD': 1, 'CN50_USD': 1,
    # Pool-expansion metals
    'XCU_USD': 5, 'XPT_USD': 3, 'XPD_USD': 3,
    'BTC_USD': 2, 'ETH_USD': 2, 'LTC_USD': 2,
}

def get_price_decimals(instrument: str) -> int:
    """Return decimal precision for an instrument's price."""
    return _INSTRUMENT_DECIMALS.get(instrument, 5)
