"""
Pipeline Utilities: Core functions for strategy research, validation, and live testing.
Handles GT-Score calculation, grid search, walk-forward analysis, and database operations.
"""

import json
import hashlib
import sqlite3
from datetime import datetime, timedelta
from pathlib import Path
from typing import Dict, List, Tuple, Any, Optional
import pandas as pd
import numpy as np
from contextlib import contextmanager


# ============================================================================
# GT-SCORE CALCULATION (Alexander Sheppert methodology)
# ============================================================================

def compute_gt_score(returns: pd.Series) -> float:
    """
    Compute GT-Score for a return series.
    
    Combines:
    - t-statistic of R² (compound excess return metric)
    - Downside deviation (captures drawdown risk)
    - Consistency (Sharpe-like component)
    
    Args:
        returns: pd.Series of returns (daily or period returns)
    
    Returns:
        GT-Score float. Higher is better. Typically 0.5-3.0 range.
    """
    if len(returns) < 2:
        return 0.0
    
    returns = returns.dropna()
    if len(returns) < 2:
        return 0.0
    
    # 1. Compute annualized return and volatility
    annual_ret = returns.mean() * 252  # Assume 252 trading days
    annual_vol = returns.std() * np.sqrt(252)
    
    if annual_vol < 1e-6:  # Avoid division by zero
        return 0.0
    
    # 2. Compute Sharpe ratio (excess return / volatility, assuming 0% risk-free)
    sharpe = annual_ret / annual_vol
    
    # 3. Compute downside deviation (only negative returns)
    downside_returns = returns[returns < 0]
    if len(downside_returns) > 0:
        downside_dev = downside_returns.std() * np.sqrt(252)
    else:
        downside_dev = 0.001  # Avoid division by zero if no losses
    
    # 4. Compute Sortino ratio (annualized return / downside deviation)
    if downside_dev > 0:
        sortino = annual_ret / downside_dev
    else:
        sortino = sharpe
    
    # 5. Compute consistency metric: fraction of positive periods (active bars only)
    # Flat bars (return=0) don't count as wins — they dilute win rate unfairly
    active_returns = returns[returns != 0]
    if len(active_returns) > 0:
        win_rate = (active_returns > 0).sum() / len(active_returns)
    else:
        win_rate = 0.5  # No active trades = neutral consistency

    # 6. Combine into GT-Score
    # Formula: base on Sharpe + Sortino + consistency weight
    gt_score = (sharpe + 2 * sortino + 2 * (win_rate - 0.5)) / 3.0
    
    # Ensure non-negative (shift if needed)
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
            signals = strategy_func(data, params)
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
        
        except Exception as e:
            # Skip malformed combos
            pass
    
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
    apply_costs: bool = True
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

    Returns:
        dict with:
          - combined_gt_score: float
          - per_window_gt_scores: list of floats
          - min_window_score: float
          - all_oos_returns: pd.Series of combined OOS returns
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
        
        if len(train_data) < 10 or len(test_data) < 10:
            continue
        
        try:
            # Grid search on train
            best_params, train_score = grid_search(
                train_data, strategy_func, param_grid, metric=metric,
                instrument=instrument, granularity=granularity, apply_costs=apply_costs
            )

            # Evaluate best params on test (OOS)
            test_signals = strategy_func(test_data, best_params)
            if apply_costs:
                test_returns = compute_net_strategy_returns(test_data, test_signals, instrument, granularity)
            else:
                test_returns = compute_strategy_returns(test_data, test_signals)
            test_score = compute_gt_score(test_returns)
            
            per_window_scores.append(test_score)
            all_oos_returns.append(test_returns)
        
        except Exception as e:
            pass
    
    # Combine all OOS returns
    if all_oos_returns:
        combined_oos = pd.concat(all_oos_returns, ignore_index=True)
        combined_score = compute_gt_score(combined_oos)
        min_score = min(per_window_scores) if per_window_scores else 0.0
    else:
        combined_oos = pd.Series(dtype=float)
        combined_score = 0.0
        min_score = 0.0
    
    return {
        'combined_gt_score': combined_score,
        'per_window_gt_scores': per_window_scores,
        'min_window_score': min_score,
        'all_oos_returns': combined_oos,
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
    'XAU_USD': 40.0,   # gold: ~$0.40 = 40 pip units (each pip = $0.01)
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
    'XAU_USD': 0.01,   # $0.01 per pip per unit
    'BCO_USD': 0.01,
    'WTICO_USD': 0.01,
    'CORN_USD': 0.01,
    'NATGAS_USD': 0.01,
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
    daily_swap = get_daily_swap(instrument)

    # Use dynamic spread if available in data, else static
    has_dynamic_spread = (data is not None and 'spread_price' in data.columns and
                       data['spread_price'].notna().any())
    if has_dynamic_spread:
        spread_pips = get_spread_pips(instrument)
        dynamic_spread_pips = data['spread_price'].fillna(spread_pips).values[1:]
        cost_price_units = dynamic_spread_pips * pip_val
        if len(cost_price_units) > len(net_returns):
            cost_price_units = cost_price_units[:len(net_returns)]
    else:
        spread_pips = get_spread_pips(instrument)
        static_cost = spread_pips * pip_val
        cost_price_units = static_cost

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

def compute_strategy_fingerprint(code: str, param_grid: Dict, timeframe: str = 'D') -> str:
    """
    Compute SHA256 fingerprint of strategy code + param grid + timeframe.

    Args:
        code: Python source code string
        param_grid: dict of parameters
        timeframe: granularity string (default 'D')

    Returns:
        SHA256 hex digest (lowercase)
    """
    param_json = json.dumps(param_grid, sort_keys=True)
    combined = code + param_json + timeframe
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
                tested_at TEXT NOT NULL
            )
        ''')
        
        # live_status table
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS live_status (
                strategy_id TEXT PRIMARY KEY REFERENCES strategies(id),
                start_date TEXT,
                equity_curve TEXT,
                current_gt_score REAL,
                last_updated TEXT
            )
        ''')
        
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
    final_status: str
) -> None:
    """
    Record validation results and update strategy status.
    
    final_status: 'pass' or 'fail: <reason>'
    """
    with get_db_connection() as conn:
        cursor = conn.cursor()
        now = datetime.utcnow().isoformat()
        best_params_json = json.dumps(best_params, sort_keys=True)
        
        # Fetch old status for audit trail
        cursor.execute('SELECT status FROM strategies WHERE id = ?', (strategy_id,))
        row = cursor.fetchone()
        old_status = row['status'] if row else 'unknown'
        
        # Insert validation result
        cursor.execute('''
            INSERT INTO validation_results
            (strategy_id, best_params, is_gt_score, walk_forward_gt_score, holdout_gt_score, final_status, tested_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
        ''', (strategy_id, best_params_json, is_score, wf_score, ho_score, final_status, now))
        
        # Update strategy status
        fl = final_status.lower()
        if 'pass' == fl or fl.startswith('pass'):
            new_status = 'passed'
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
        }
