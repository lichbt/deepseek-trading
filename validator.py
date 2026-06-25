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
    STOP_MULT_SWEEP,
)
from data_fetcher import get_candles_date_range
from supplementary_data import inject_supplementary_data
from risk import compute_max_drawdown, compute_calmar_ratio, compute_ulcer_index
from risk import compute_max_drawdown, compute_calmar_ratio, compute_ulcer_index


# ---------------------------------------------------------------------------
# Honesty layer: locked holdout + trials accounting + deflated-Sharpe gate.
# Math lives in strategy_honesty.py. Everything here FAILS OPEN — a bug in the
# honesty layer must never block or crash validation.
# ---------------------------------------------------------------------------
LOCKED_HOLDOUT_START = '2025-12-20'   # recent window the validation loop NEVER
                                      # scores; only final_holdout.py reads it,
                                      # once, for the single final winner.
TRIALS_DB = os.path.join(os.path.dirname(__file__), 'trials.db')
DSR_MIN = 0.95                        # promote an HO-passer only if deflated Sharpe >= this
DSR_GATE_ENABLED = os.environ.get('DSR_GATE', '1') != '0'


def _failure_tag(final_status):
    """Map a validator final_status string to a strategy_honesty FAILURE_TAGS key
    (fixed vocabulary -> aggregatable). Order = priority; 'too few ... trades'
    must be caught before 'holdout' so it isn't mislabelled ho_decay."""
    s = (final_status or '').lower()
    if 'pass' in s:                                            return None
    if 'drawdown' in s:                                        return 'dd_breach'
    if 'too few' in s or 'sparse' in s or 'unverified' in s or 'entries' in s:
        return 'low_sample'
    if 'decay' in s or 'deflated' in s:                        return 'ho_decay'
    if 'directional' in s or 'shuffle' in s or 'torture' in s: return 'regime_fragile'
    return 'insufficient_folds'


def _honesty_record(strategy_id, strategy_func, best_params, data,
                    instrument, granularity, passed_wf, passed_ho, final_status):
    """Log this trial's per-period (daily) Sharpe to TRIALS_DB so the search's
    variance is known. Returns the candidate's daily return array (for the DSR
    gate) or None. Fail-open."""
    try:
        import strategy_honesty as H
        sig = strategy_func(data, best_params)
        ret = compute_net_strategy_returns(data, sig, instrument, granularity, params=best_params)
        if ret is None or len(ret) < 20:
            return None
        ret = np.asarray(ret, dtype=float)
        sd = ret.std()
        sharpe = float(ret.mean() / sd) if sd > 0 else 0.0
        H.record_trial(TRIALS_DB, strategy_id, sharpe,
                       bool(passed_wf), bool(passed_ho), _failure_tag(final_status))
        return ret
    except Exception as e:
        print(f"  [honesty] trial log skipped: {e}", flush=True)
        return None


def _instrument_trial_sharpes(strategy_id):
    """Trial Sharpes for the SAME instrument only (matched on the id prefix, e.g.
    'eurusd' in 'eurusd_auto_...'). DSR must deflate against the same-instrument
    search: the expected-max-Sharpe assumes one distribution, so a winner on a
    barely-tried instrument shouldn't be taxed for thousands of trials on another
    (different N AND different Sharpe variance)."""
    import sqlite3, strategy_honesty as H
    prefix = strategy_id.split('_auto_')[0]
    con = sqlite3.connect(TRIALS_DB); con.execute(H._SCHEMA)
    out = [s for (h, s) in con.execute("SELECT hash, sharpe FROM trials")
           if h.split('_auto_')[0] == prefix]
    con.close()
    return out


def _dsr_gate(cand_returns, strategy_id):
    """(promote_ok, dsr): deflate the candidate's Sharpe against the trial pool
    FOR THIS INSTRUMENT (winners + losers). Reject as ho_decay if below DSR_MIN.
    Pool starts small (lenient) and tightens as same-instrument trials accrue.
    Fail-open -> (True, None)."""
    if not DSR_GATE_ENABLED or cand_returns is None:
        return True, None
    try:
        import strategy_honesty as H
        dsr = H.deflated_sharpe_ratio(cand_returns, _instrument_trial_sharpes(strategy_id))
        return (dsr >= DSR_MIN), float(dsr)
    except Exception as e:
        print(f"  [honesty] DSR gate skipped: {e}", flush=True)
        return True, None


# Configuration
DEV_START = '2015-01-01'
DEV_END = '2019-12-31'
HOLDOUT_START = '2024-01-01'

# Per-instrument DEV window overrides for instruments whose OANDA history
# doesn't go back to 2015. Crypto launched on OANDA in 2019-2020.
#
# The earlier (2026-05-25) attempt only overrode DEV_START and left DEV_END at
# 2019-12-31, which produced dev_start > DEV_END for ETH/LTC → empty range →
# all 40 ETH/LTC iterations the next day still failed "No valid data". Override
# BOTH dates as a tuple so the window is always sane. Each override gives the
# instrument at least 2 years of dev data (the WF needs ~5 windows × ~10 bars
# minimum). HOLDOUT_START stays 2024-01-01.
#
# Verified empirically: BTC has D candles from 2019-01-01; ETH/LTC from
# 2020-01-02 (so we start at 02 to skip the first incomplete candle).
DEV_OVERRIDES = {
    'BTC_USD': ('2019-01-01', '2020-12-31'),   # 2y dev
    'ETH_USD': ('2020-01-02', '2021-12-31'),   # 2y dev
    'LTC_USD': ('2020-01-02', '2021-12-31'),   # 2y dev
}


def get_dev_window(instrument: str) -> tuple:
    """(dev_start, dev_end) for the instrument. Pushed forward for new
    instruments (crypto) without OANDA history back to 2015. Falls back to the
    global DEV_START / DEV_END for everything else."""
    return DEV_OVERRIDES.get(instrument, (DEV_START, DEV_END))


# Back-compat alias for any external caller still using the older function name.
def get_dev_start(instrument: str) -> str:
    return get_dev_window(instrument)[0]

# Default instrument (can be overridden in strategy JSON)
DEFAULT_INSTRUMENT = 'EUR_USD'

# Allowed timeframes
VALID_TIMEFRAMES = ['M30', 'H1', 'H4', 'D', 'W']
DEFAULT_TIMEFRAME = 'D'

# GT-Score thresholds
MIN_IS_SCORE = 0.3
MIN_WF_SCORE = 0.5            # Raised 0.3->0.5 (2026-06-08): walk-forward is the
                             # out-of-sample quality gate; 0.3-0.5 passes were
                             # consistently marginal/rejected on manual review, so
                             # require clearer OOS edge. IS stays 0.3.
MIN_WINDOW_SCORE = 0.0        # Require no losing windows (breakeven allowed)
MIN_HO_SCORE = 0.10           # Absolute HO floor — prevents near-zero HO on weak WF strategies
HOLDOUT_DECLINE_THRESHOLD = 0.6  # Raised from 0.5 — max 40% relative decay WF→HO
MIN_HO_ENTRIES = 10           # Min DISTINCT holdout trades (entries/flips, not bars in
                              # position). A high HO score from a handful of trades is
                              # noise, not edge — daily breakout/skew strategies kept
                              # passing on 5-7 holdout trades with an inflated HO.

# --- Stress-test thresholds (offline OOS validation) ---
MAX_OOS_DRAWDOWN = 0.30       # Flag strategy if max drawdown exceeds 30%
MIN_CALMAR_RATIO = 0.3         # Flag strategy if Calmar ratio below 0.3 (soft gate)

# --- Hard drawdown gate (2026-06-14) ---
# Reconstruct the strategy's CONTINUOUS full-history equity (dev + WF + holdout,
# fixed WF-chosen params) and HARD-REJECT if peak-to-trough exceeds this. The
# GT-score is risk-adjusted but a strategy can still post a strong WF while
# carrying a crater-deep drawdown that would blow a prop account's static limit
# — exactly the class that kept reaching manual review (BTC 51%, Brent 36.5%,
# palladium 38%). Measured on ONE continuous series so a drawdown straddling the
# WF/holdout boundary is caught (a holdout-only window resets the peak and hides
# it). Calibrated against the live book: 0.30 clears every kept sleeve (worst
# now silver ~21%) while rejecting the 36%+ beta candidates.
MAX_DRAWDOWN_HARD = 0.30

# --- Option B trade-aware WF gate ---
MIN_WINDOWS_WITH_EDGE = 3  # at least 3 windows must have GT > 0 to allow breakeven

# Timeframes to try for multi-timeframe validation
TIMEFRAMES = ['D', 'W', 'H4']


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
    allowed_archetypes = ['standard', 'news', 'session', 'pair', 'macro']
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


def reconstructed_max_drawdown(strategy_func, best_params, full_data, holdout_data,
                               instrument, granularity) -> float:
    """Continuous full-history peak-to-trough drawdown (fraction >= 0) of the
    strategy at its WF-chosen params: dev+WF (full_data) spliced with the holdout
    into ONE equity series, so a drawdown crossing the WF/holdout boundary is
    measured rather than hidden by a peak reset. Returns 0.0 when it can't be
    computed (too little data / any error) so the DD gate FAILS OPEN — a
    reconstruction glitch must never reject a strategy."""
    try:
        if holdout_data is not None and len(holdout_data):
            oos = pd.concat([full_data, holdout_data], ignore_index=True)
            if 'date' in oos.columns:
                oos = oos.drop_duplicates(subset='date').reset_index(drop=True)
        else:
            oos = full_data
        sig = strategy_func(oos, best_params)
        ret = compute_net_strategy_returns(oos, sig, instrument, granularity, params=best_params)
        if ret is None or len(ret) < 20:
            return 0.0
        return float(compute_max_drawdown(ret))
    except Exception as e:
        print(f"  [DD gate] reconstruction failed ({e}) — failing open", flush=True)
        return 0.0


def validate_on_timeframe(dev_data, full_data, holdout_data, strategy_func, param_grid,
                        instrument, granularity, strategy_id) -> dict:
    """
    Run full validation pipeline on a single timeframe.
    Returns dict with scores and pass/fail status.
    """
    # Co-optimize the live ATR stop with the strategy params: auto-inject a
    # coarse stop_mult sweep into the SEARCH grid so grid_search/walk_forward
    # pick the stop alongside the entry/exit params. The fingerprint was already
    # computed from the ORIGINAL param_grid (caller), so dedup is unaffected —
    # this augmented grid is search-only.
    search_grid = dict(param_grid)
    if 'stop_mult' not in search_grid:
        search_grid['stop_mult'] = list(STOP_MULT_SWEEP)

    # Step 5: Grid search on dev data (in-sample)
    try:
        best_params, is_score = grid_search(
            dev_data,
            strategy_func,
            search_grid,
            instrument=instrument,
            granularity=granularity,
            apply_costs=True,
        )
    except TimeoutError:
        return {
            'granularity': granularity,
            'passed': False,
            'best_params': {},
            'is_score': 0.0,
            'wf_score': None,
            'min_wf_score': None,
            'ho_score': None,
            'reason': 'Strategy timed out during grid search (infinite loop in code)'
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
    try:
        wf_result = walk_forward(
            full_data,
            strategy_func,
            search_grid,
            n_windows=5,
            instrument=instrument,
            granularity=granularity,
            apply_costs=True,
        )
    except TimeoutError:
        return {
            'granularity': granularity,
            'passed': False,
            'best_params': best_params,
            'is_score': is_score,
            'wf_score': None,
            'min_wf_score': None,
            'ho_score': None,
            'reason': 'Strategy timed out during walk-forward (infinite loop in code)'
        }

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

    # Multi-regime gate: the combined WF score can clear MIN_WF_SCORE on the
    # strength of a single exceptional window (e.g. one historical crash) while
    # every other window scores 0. Require the edge to show up in at least
    # MIN_WINDOWS_WITH_EDGE separate windows so we reject single-event flukes.
    windows_with_edge = wf_result.get('windows_with_edge', 0)
    if windows_with_edge < MIN_WINDOWS_WITH_EDGE:
        per_window = wf_result.get('per_window_gt_scores', [])
        return {
            'granularity': granularity,
            'passed': False,
            'best_params': best_params,
            'is_score': is_score,
            'wf_score': wf_score,
            'min_wf_score': min_wf_score,
            'ho_score': None,
            'reason': (
                f'Single-regime edge: only {windows_with_edge}/{num_valid_windows} '
                f'windows profitable (need >= {MIN_WINDOWS_WITH_EDGE}); '
                f'per-window={[round(s, 3) for s in per_window]}'
            ),
            'wf_result': wf_result
        }

    # Step 6b: Hard drawdown gate — reject crater-deep DD that a strong WF can
    # still hide (see MAX_DRAWDOWN_HARD). Measured on the continuous full-history
    # equity so boundary-crossing drawdowns are caught; fails open on any error.
    recon_dd = reconstructed_max_drawdown(
        strategy_func, best_params, full_data, holdout_data, instrument, granularity
    )
    if recon_dd > MAX_DRAWDOWN_HARD:
        return {
            'granularity': granularity,
            'passed': False,
            'best_params': best_params,
            'is_score': is_score,
            'wf_score': wf_score,
            'min_wf_score': min_wf_score,
            'ho_score': None,
            'reason': (f'Max drawdown {recon_dd:.1%} > {MAX_DRAWDOWN_HARD:.0%} '
                       f'(full reconstructed equity) — prop-disqualifying'),
            'wf_result': wf_result,
        }

    # Step 7: Hold-out validation
    stress_note = ''
    if holdout_data is not None and len(holdout_data) >= 20:
        ho_signals = strategy_func(holdout_data, best_params)
        ho_trade_count = (ho_signals != 0).sum()

        # Distinct trade entries (transitions into / flips of a position), NOT bars
        # in position. ho_trade_count above counts bars, so a strategy with a few
        # entries held over many bars looks like it has hundreds of "trades". The
        # statistical reliability of the holdout result depends on the number of
        # independent trades, so count entries explicitly.
        _ho_sig = pd.Series(np.asarray(ho_signals)).fillna(0)
        ho_entries = int(((_ho_sig != 0) & (_ho_sig != _ho_sig.shift(1).fillna(0))).sum())

        # Zero holdout trades = strategy is unverified out-of-sample. The HO
        # decay check below is `if ho_trade_count > 0 ...`, so a zero-trade
        # strategy used to skip the check entirely and auto-pass with HO=0.
        # A strategy that never fires in 2+ years of recent data must NOT pass.
        if ho_trade_count == 0:
            return {
                'granularity': granularity,
                'passed': False,
                'best_params': best_params,
                'is_score': is_score,
                'wf_score': wf_score,
                'min_wf_score': min_wf_score,
                'ho_score': 0.0,
                'ho_trade_count': 0,
                'reason': (
                    f'No holdout trades — strategy unverified out-of-sample '
                    f'({len(holdout_data)} holdout bars, 0 signals)'
                ),
                'wf_result': wf_result
            }

        # Too few DISTINCT holdout trades = the holdout score is not statistically
        # reliable (a high HO from a handful of trades is noise, not edge). Reject
        # rather than let it pass on an inflated small-sample holdout.
        if ho_entries < MIN_HO_ENTRIES:
            return {
                'granularity': granularity,
                'passed': False,
                'best_params': best_params,
                'is_score': is_score,
                'wf_score': wf_score,
                'min_wf_score': min_wf_score,
                'ho_score': 0.0,
                'ho_trade_count': int(ho_trade_count),
                'ho_entries': ho_entries,
                'reason': (
                    f'Too few holdout trades ({ho_entries} distinct entries '
                    f'< {MIN_HO_ENTRIES}) — holdout result not statistically reliable'
                ),
                'wf_result': wf_result
            }

        ho_score = evaluate_on_data(
            holdout_data,
            strategy_func,
            best_params,
            instrument=instrument,
            granularity=granularity,
            apply_costs=True,
        )

        ho_returns = compute_net_strategy_returns(holdout_data, ho_signals, instrument, granularity, params=best_params)
        # Raw annualised holdout return. ho_score (a GT-Score) is floored at 0,
        # so a strategy that lost 0.5% and one that lost 40% both record HO=0.
        # The raw return preserves the magnitude so HO failures are analyzable.
        ho_ann_return = float(ho_returns.mean() * 252) if len(ho_returns) else 0.0
        if len(ho_returns) >= 10:
            max_dd = compute_max_drawdown(ho_returns)
            calmar = compute_calmar_ratio(ho_returns)
            ulcer = compute_ulcer_index(ho_returns)
            if max_dd > MAX_OOS_DRAWDOWN or calmar < MIN_CALMAR_RATIO:
                stress_note = f' | Stress: DD={max_dd:.2%}, Calmar={calmar:.2f}, Ulcer={ulcer:.2f}'

        # Calculate acceptable HO threshold
        # HO must clear both an absolute floor and a relative decay limit
        if ho_trade_count < 10:
            min_acceptable_ho = max(MIN_HO_SCORE, wf_score * 0.5)
            ho_note = f"(low trades: {ho_trade_count})"
        else:
            min_acceptable_ho = max(MIN_HO_SCORE, wf_score * HOLDOUT_DECLINE_THRESHOLD)
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
                'reason': (f'HO decay {ho_score:.4f} < {min_acceptable_ho:.4f} '
                           f'(raw_ann={ho_ann_return:+.1%}) {ho_note}{stress_note}'),
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


# ---------------------------------------------------------------------------
# Torture Tests — post-PASS robustness battery
# ---------------------------------------------------------------------------

_PEER_INSTRUMENT = {
    'EUR_USD': 'GBP_USD', 'GBP_USD': 'EUR_USD',
    'USD_JPY': 'EUR_JPY', 'EUR_JPY': 'USD_JPY',
    'XAU_USD': 'XAG_USD',
    'WTICO_USD': 'BCO_USD', 'BCO_USD': 'WTICO_USD',
    'AUD_USD': 'NZD_USD', 'NZD_USD': 'AUD_USD',
}


def run_torture_tests(
    strategy_func,
    best_params: dict,
    dev_data: pd.DataFrame,
    wf_result: dict,
    instrument: str,
    granularity: str,
    n_shuffle: int = 200,
    param_grid: dict = None,
) -> list:
    """
    Run post-PASS robustness checks on a strategy that passed all validation gates.

    Returns a list of flag strings (empty = robust). Never raises — any internal
    error causes that test to be skipped (not counted as a flag).

    Tests:
      1. signal_shuffle   — real GT-Score must beat 90th-pct of 200 random permutations
      2. instrument_transfer — same logic on peer instrument must score >= 0.03
      3. param_instability   — WF-window best_params must not jump wildly (CoV <= 1.0)
      4. directional_bias    — long fraction > 60%, OR structurally one-sided
                               (only longs or only shorts) — both flag beta, not edge
    """
    import signal as _signal

    flags = []

    # Adaptive shuffle count: cap at 100 for large intraday datasets to bound runtime
    n_shuf = 100 if len(dev_data) > 2000 else n_shuffle

    # ── Test 4: Directional Bias ──────────────────────────────────────────────
    # A strategy is a directional BETA bet (not an edge) when it sits in the
    # market in ONE direction MOST of the time — it's just holding the asset.
    #   (a) long >60% of bars — always-long beta.
    #   (b) one-directional AND in-market >60% of bars — catches always-SHORT
    #       beta too (which (a) misses, since it only looks at long_frac).
    # Crucially, a SELECTIVE one-sided strategy is NOT beta and is allowed: if
    # it's flat most bars and only takes (say) longs during a specific regime,
    # that's timing an edge, not riding drift. Example: a macro DXY-weakness
    # NZD/USD long that is flat ~65% of the time over hundreds of distinct
    # entries — one-directional by thesis, but clearly selective. Earlier the
    # one_sided check fired on "never shorts" regardless of selectivity and
    # hard-rejected exactly these legitimate regime-conditioned macro edges.
    try:
        bias_sigs   = strategy_func(dev_data, best_params)
        long_frac   = float((bias_sigs > 0).mean())
        active_frac = float((bias_sigs != 0).mean())
        has_long    = bool((bias_sigs > 0).any())
        has_short   = bool((bias_sigs < 0).any())
        n_trades    = int((bias_sigs != 0).sum())

        biased = long_frac > 0.60
        # One-sided is only a beta-bet when the strategy is ALSO in the market
        # most of the time (active_frac > 0.60). A one-directional strategy that
        # trades selectively (low active_frac) is timing a regime, not holding
        # beta, and is allowed. n_trades >= 20 ensures the one-sidedness is
        # structural, not a quiet sample.
        one_sided = (n_trades >= 20) and (has_long != has_short) and (active_frac > 0.60)

        if biased:
            flags.append(f'directional_bias(long={long_frac:.0%})')
        if one_sided and not biased:
            side = 'long' if has_long else 'short'
            flags.append(f'directional_bias(one_sided_{side}={active_frac:.0%})')
        verdict = 'FRAGILE' if (biased or one_sided) else 'OK'
        print(
            f"  [Torture] Directional bias: long={long_frac:.0%} "
            f"active={active_frac:.0%} one_sided={one_sided} ({n_trades} trades) → {verdict}",
            flush=True,
        )
    except Exception as e:
        print(f"  [Torture] Directional bias test skipped: {e}", flush=True)

    # ── Test 1: Signal Shuffle ────────────────────────────────────────────────
    try:
        real_sigs = strategy_func(dev_data, best_params)
        real_returns = compute_net_strategy_returns(dev_data, real_sigs, instrument, granularity, params=best_params)
        real_score = compute_gt_score(real_returns)

        shuffled_scores = []
        sig_vals = real_sigs.values.copy()
        for _ in range(n_shuf):
            shuffled = np.random.permutation(sig_vals)
            s = pd.Series(shuffled, index=real_sigs.index)
            r = compute_net_strategy_returns(dev_data, s, instrument, granularity, params=best_params)
            shuffled_scores.append(compute_gt_score(r))

        pct90 = float(np.percentile(shuffled_scores, 90))
        fragile = real_score <= pct90
        if fragile:
            flags.append('signal_shuffle')
        print(
            f"  [Torture] Shuffle ({n_shuf}x): real={real_score:.4f} vs "
            f"90th-pct={pct90:.4f} → {'FRAGILE' if fragile else 'OK'}",
            flush=True,
        )
    except Exception as e:
        print(f"  [Torture] Shuffle test skipped: {e}", flush=True)

    # ── Test 2: Instrument Transfer ───────────────────────────────────────────
    peer = _PEER_INSTRUMENT.get(instrument)
    if peer:
        try:
            start = dev_data['date'].iloc[0].strftime('%Y-%m-%d')
            end   = dev_data['date'].iloc[-1].strftime('%Y-%m-%d')
            peer_data = get_candles_date_range(peer, start, end, granularity=granularity)
            if len(peer_data) >= 100:
                peer_sigs    = strategy_func(peer_data, best_params)
                peer_returns = compute_net_strategy_returns(peer_data, peer_sigs, peer, granularity, params=best_params)
                peer_score   = compute_gt_score(peer_returns)
                fragile      = peer_score < 0.03
                if fragile:
                    flags.append('instrument_transfer')
                print(
                    f"  [Torture] Transfer ({peer}): score={peer_score:.4f} "
                    f"→ {'FRAGILE' if fragile else 'OK'}",
                    flush=True,
                )
            else:
                print(f"  [Torture] Transfer ({peer}): skipped (only {len(peer_data)} bars)", flush=True)
        except Exception as e:
            print(f"  [Torture] Transfer test skipped: {e}", flush=True)

    # ── Test 3: WF Parameter Stability ───────────────────────────────────────
    try:
        per_window_params = wf_result.get('per_window_best_params', [])
        if len(per_window_params) >= 3:
            param_keys = [k for k, v in per_window_params[0].items() if isinstance(v, (int, float))]
            unstable = []
            grid = param_grid or {}
            for k in param_keys:
                vals = [w[k] for w in per_window_params if k in w and isinstance(w[k], (int, float))]
                if len(vals) >= 2:
                    mean = float(np.mean(vals))
                    if abs(mean) > 1e-9:
                        cov = float(np.std(vals)) / abs(mean)
                        if cov > 1.0:
                            # CoV degenerates when the mean sits near zero: a param that
                            # merely TOGGLES between two adjacent grid options (e.g.
                            # regime_thresh in {0.0, 0.05}) yields std > mean and trips the
                            # threshold despite being economically stable. Only flag if the
                            # WF-chosen values actually scatter across NON-adjacent grid
                            # levels (genuine non-convergence), not 1-step boundary jitter.
                            grid_vals = sorted({float(g) for g in grid.get(k, [])
                                                if isinstance(g, (int, float))})
                            if len(grid_vals) >= 2:
                                # bisect to the grid level nearest each chosen extreme,
                                # robust to float drift / values not exactly in the grid.
                                import bisect
                                lo_i = min(bisect.bisect_left(grid_vals, float(min(vals))),
                                           len(grid_vals) - 1)
                                hi_i = min(bisect.bisect_left(grid_vals, float(max(vals))),
                                           len(grid_vals) - 1)
                                levels_spanned = hi_i - lo_i
                            else:
                                # grid unavailable for this key → preserve old behaviour
                                levels_spanned = 2
                            if levels_spanned >= 2:
                                unstable.append(f"{k}(CoV={cov:.2f},span={levels_spanned})")
            fragile = bool(unstable)
            if fragile:
                flags.append('param_instability')
            print(
                f"  [Torture] Param stability: unstable={unstable} "
                f"→ {'FRAGILE' if fragile else 'OK'}",
                flush=True,
            )
        else:
            print(
                f"  [Torture] Param stability: skipped "
                f"(only {len(per_window_params)} WF windows with params)",
                flush=True,
            )
    except Exception as e:
        print(f"  [Torture] Param stability test skipped: {e}", flush=True)

    return flags


def validate_strategy(candidate: dict, skip_insert: bool = False) -> tuple:
    """
    Run full validation pipeline on strategy candidate.

    skip_insert: if True, skip the duplicate-fingerprint check and DB insert.
                 Use this when revalidating strategies that already exist in the DB.

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

    if skip_insert:
        print("[1/8] Skipping duplicate check (revalidation mode)")
        print("[2/8] Skipping insert (strategy already in DB)")
    else:
        # Step 1: Check for duplicate fingerprint (includes timeframe)
        print("[1/8] Checking for duplicate...")
        fingerprint = compute_strategy_fingerprint(code, param_grid, timeframe, instrument, archetype)
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
    # Per-instrument DEV window so BTC/ETH/LTC don't fail every validation —
    # OANDA crypto only goes back to ~2019-2020. Both start AND end must shift,
    # otherwise overriding only start can produce start>end (see 2026-05-26
    # ETH/LTC silent-empty-range regression).
    dev_start, dev_end = get_dev_window(instrument)
    print(f"\n[4/8] Fetching data for timeframe [{timeframe}] [{dev_start} to {dev_end}]...")
    results = []
    try:
        dev_data = get_candles_date_range(instrument, dev_start, dev_end, granularity=timeframe)
        print(f"  [{timeframe}] {len(dev_data)} candles")
        if len(dev_data) >= 100:
            # Inject supplementary data based on archetype
            if archetype != 'standard':
                print(f"  Injecting supplementary data for archetype '{archetype}'...")
                dev_data = inject_supplementary_data(
                    dev_data, archetype, instrument, instrument2,
                    dev_start, dev_end, timeframe
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
    # Track the failing result of the last timeframe attempted so we can record
    # the *actual* IS/WF/HO scores in the DB instead of collapsing every fail
    # to (0, 0, 0). Without this, meta_review can't see the failure distribution.
    last_failing_result = None
    for tf_result in valid_timeframes:
        tf = tf_result['granularity']
        dev_data = tf_result['dev_data']

        try:
            # Fetch full data for walk-forward (also instrument-aware start)
            wf_end = datetime.strptime(HOLDOUT_START, '%Y-%m-%d').strftime('%Y-%m-%d')
            full_data = get_candles_date_range(instrument, dev_start, wf_end, granularity=tf)
            # Inject supplementary data for full_data if needed
            if archetype != 'standard':
                full_data = inject_supplementary_data(
                    full_data, archetype, instrument, instrument2,
                    dev_start, wf_end, tf
                )
            # The loop's holdout STOPS at LOCKED_HOLDOUT_START — the most recent
            # window is locked away (see final_holdout.py) so it can't be mined.
            ho_end = LOCKED_HOLDOUT_START
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
        else:
            last_failing_result = result

    # Step 6: Final decision
    print(f"\n[6/8] Validation result:")
    for r in results:
        status = 'OK' if not r['error'] else f'FAIL: {r.get("error", "")}'
        print(f"  [{r['granularity']}] {status}")

    if best_overall is None:
        # Preserve the gate-specific reason and the *actual* scores so meta_review
        # can see the failure distribution. Collapsing to (0,0,0,"did not pass")
        # made all failures look identical and broke pattern analysis.
        if last_failing_result is not None:
            r = last_failing_result
            msg = f'FAIL: {r.get("reason", "Validation did not pass all gates")}'
            record_validation(
                strategy_id,
                r.get('best_params') or {},
                float(r.get('is_score') or 0.0),
                float(r.get('wf_score') or 0.0),
                float(r.get('ho_score') or 0.0),
                msg,
            )
            # Log the loser's Sharpe — losers define the variance the DSR needs.
            _honesty_record(strategy_id, strategy_func, r.get('best_params') or {},
                            full_data, instrument, timeframe,
                            float(r.get('wf_score') or 0.0) >= MIN_WF_SCORE, False, msg)
        else:
            msg = 'FAIL: Validation did not pass all gates'
            record_validation(strategy_id, {}, 0.0, 0.0, 0.0, msg)
        print(f"  {msg}")
        return False, msg

    print(f"\n[7/8] Best result:")
    print(f"  Timeframe: {best_overall['granularity']}")
    print(f"  IS={best_overall['is_score']:.4f} | WF={best_overall['wf_score']:.4f} | MinWF={best_overall['min_wf_score']:.4f} | HO={best_overall.get('ho_score', 'N/A')}")
    print(f"  Best params: {best_overall['best_params']}")

    # Step 7b: Torture tests — post-PASS robustness battery
    print(f"\n[7b/8] Running torture tests...", flush=True)
    torture_flags = []
    try:
        # Reconstruct the SEARCH grid (original param_grid + injected stop sweep) so
        # the param-stability check can measure how far WF-chosen values scatter
        # across actual grid levels (see grid-step-span guard in run_torture_tests).
        torture_grid = dict(param_grid)
        torture_grid.setdefault('stop_mult', list(STOP_MULT_SWEEP))
        torture_flags = run_torture_tests(
            strategy_func=strategy_func,
            best_params=best_overall['best_params'],
            dev_data=dev_data,
            wf_result=best_overall['wf_result'],
            instrument=instrument,
            granularity=best_overall['granularity'],
            param_grid=torture_grid,
        )
    except Exception as e:
        print(f"  [Torture] Battery error (skipped): {e}", flush=True)
    # directional_bias is a hard rejection — not just fragile
    hard_reject = [f for f in torture_flags if f.startswith('directional_bias')]
    if hard_reject:
        msg = f'FAIL: {hard_reject[0]} — trend-riding, not an edge'
        print(f"  ✗ {msg}", flush=True)
        record_validation(strategy_id, best_overall['best_params'],
                          best_overall['is_score'], best_overall['wf_score'],
                          best_overall.get('ho_score') or 0.0, msg)
        return False, msg

    if torture_flags:
        print(f"  ⚠ Fragility flags: {torture_flags} → status will be 'passed_but_fragile'", flush=True)
    else:
        print(f"  ✓ All torture tests passed → status will be 'passed'", flush=True)

    # Step 8: Record result
    print(f"\n[8/8] Recording to DB...")
    ho_val = best_overall.get('ho_score') or 0.0

    # Honesty gate: deflate this HO-passer's Sharpe against the whole trial pool.
    # An edge that clears HO but scores DSR < 0.95 is the luckiest draw of the
    # search, not a real edge -> reject as ho_decay instead of promoting.
    cand_ret = _honesty_record(strategy_id, strategy_func, best_overall['best_params'],
                               full_data, instrument, timeframe, True, ho_val > 0,
                               f"PASS ({best_overall['granularity']})")
    promote_ok, dsr = _dsr_gate(cand_ret, strategy_id)
    if not promote_ok:
        msg = f'FAIL: ho_decay — deflated Sharpe {dsr:.2f} < {DSR_MIN} (overfit to the {len(best_overall.get("best_params") or {})}-knob search)'
        print(f"  ✗ {msg}", flush=True)
        record_validation(strategy_id, best_overall['best_params'],
                          best_overall['is_score'], best_overall['wf_score'],
                          ho_val, msg, torture_flags=torture_flags)
        return False, msg

    record_validation(
        strategy_id,
        best_overall['best_params'],
        best_overall['is_score'],
        best_overall['wf_score'],
        ho_val,
        f"PASS ({best_overall['granularity']})",
        torture_flags=torture_flags,
    )

    fragile_label = " (FRAGILE)" if torture_flags else ""
    print(f"\n{'='*70}")
    print(f"PASS{fragile_label}: Strategy passed all validation gates")
    print(f"  Timeframe: {timeframe}")
    print(f"  In-sample GT-Score:      {best_overall['is_score']:.4f}")
    print(f"  Walk-forward GT-Score:   {best_overall['wf_score']:.4f}")
    print(f"  Min window score:        {best_overall['min_wf_score']:.4f}")
    print(f"  Hold-out GT-Score:       {ho_val:.4f}")
    print(f"  Best Parameters:         {best_overall['best_params']}")
    if torture_flags:
        print(f"  Fragility flags:         {torture_flags}")
        print(f"  DB status:               passed_but_fragile")
    else:
        print(f"  DB status:               passed")
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
