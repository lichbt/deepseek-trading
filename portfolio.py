"""
portfolio.py — Simple portfolio combiner / safety net for trading strategies.

Queries all passed / paper_trading strategies, reconstructs their historical
daily returns (2015-2024), then:

  1. Computes a pairwise correlation matrix.
  2. Flags any pair with |corr| > CORR_THRESHOLD (default 0.5) and suggests
     halving allocation to the lower-WF-score strategy.
  3. Computes inverse-volatility weights so each strategy contributes
     roughly equal risk.
  4. Prints a concise allocation table.

Usage:
    python portfolio.py                   # analyse + print
    python portfolio.py --write           # write portfolio_state.json for live_test.py
    python portfolio.py --min-wf 0.15     # only strategies with WF >= 0.15
    python portfolio.py --corr-thresh 0.4 # stricter correlation gate
    python portfolio.py --start 2020-01-01 --end 2024-01-01
"""

import os
import re
import sys
import json
import argparse
import sqlite3
import warnings
from datetime import datetime, timedelta
from typing import Dict, List, Optional, Tuple

import numpy as np
import pandas as pd

warnings.filterwarnings("ignore", category=FutureWarning)


def _load_dotenv() -> None:
    """Load .env so `python portfolio.py --write` picks up CLUSTER_CAP (and any
    other config) without the caller having to export/source it first. Existing
    env vars (deploy shell, Zeabur) still win.

    Delegates to env_loader — this used to be a private copy of the same parser,
    and the copies drifted: neither stripped inline comments, so importing
    portfolio set FIX_START_EQUITY to '2500  # account size' and broke
    fix_adapter's float() at import. One parser, one place to fix it."""
    from env_loader import load_env
    load_env(os.path.join(os.path.dirname(os.path.abspath(__file__)), '.env'))


_load_dotenv()
warnings.filterwarnings("ignore", category=UserWarning)

sys.path.insert(0, os.path.dirname(__file__))

from data_fetcher import get_candles_date_range
from pipeline_utils import compute_net_strategy_returns, compute_gt_score, init_db
from supplementary_data import inject_supplementary_data
from validator import get_dev_window

# ---------------------------------------------------------------------------
# Configuration defaults
# ---------------------------------------------------------------------------
DEFAULT_START      = "2015-01-01"
DEFAULT_END        = datetime.utcnow().strftime("%Y-%m-%d")
CORR_THRESHOLD     = 0.50   # |correlation| above this = flag
PORTFOLIO_STATE_FILE = os.path.join(os.path.dirname(__file__), "portfolio_state.json")
MIN_BARS           = 50     # skip strategies with fewer daily bars of history
INITIAL_EQUITY     = 100_000
DB_PATH            = os.path.join(os.path.dirname(__file__), "pipeline.db")
LOG_DIR            = os.path.join(os.path.dirname(__file__), ".paper-trading-logs")
RECENT_DECAY_ENTRIES = 30
RECENT_DECAY_GT_FRACTION = 0.5
# Calendar ceiling on the decay window. Entry count ALONE silently stretches into
# a multi-year window on a selective sleeve — a 4-trades-a-year index sleeve
# reached back 7.4 years, so the "recent" verdict was really a lifetime-GT-vs-WF
# comparison. Measured 2026-07-25: 24 of 54 scored sleeves had windows over 3
# years. The window is now the shorter of the two bounds.
#
# 36 is a compromise forced by how slowly this book trades: on daily, mostly
# regime-gated sleeves the MEDIAN live sleeve needs 34 months to accumulate 30
# entries (p25 23mo, p75 52mo, max 89mo). Tightening to 18mo left only 5 of 33
# live sleeves scoreable and turned the check into a no-op; 36mo scores 20 of 33
# while still excluding the pathological multi-year windows this cap exists for.
# Statistical power vs recency is a real trade here — lower this only together
# with RECENT_DECAY_MIN_ENTRIES, or the check silently stops firing.
RECENT_DECAY_MAX_MONTHS = 36
# ...and the statistical bar is unchanged: still 30 entries to score a GT. So the
# floor moves from "30 entries EVER" to "30 entries INSIDE the window". A sleeve
# whose last 30 trades don't fit in 18 months is now INSUFFICIENT rather than
# being scored on a multi-year window — that is the honest answer for a selective
# sleeve, and INSUFFICIENT carries no haircut.
RECENT_DECAY_MIN_ENTRIES = RECENT_DECAY_ENTRIES
# NEAR MISS: a sleeve that is still PROFITABLE over the window and lands within
# this fraction of its GT threshold is not decaying — it is inside the noise of a
# threshold that scales with its own WF. The stricter a sleeve's validation, the
# higher its bar (min_gt = WF/2), so the best-validated sleeves fail by hairlines:
# nas100usd_011303_i9 read GT 0.500 against min 0.510 (98%) while returning +9.2%
# over the window and +4.4% for the trailing year. Such a sleeve is reported
# INSUFFICIENT (no haircut) with an explicit note, so it is never silently
# confused with a genuinely data-thin one.
RECENT_DECAY_NEAR_MISS_FRACTION = 0.95
DECAY_CONVICTION_SCALE = 0.5
DECAY_KELLY_SCALE = 0.5

# Instrument → OANDA symbol map (for inference from strategy ID)
_PMAP = {
    "EUR_USD": "EUR_USD", "GBP_USD": "GBP_USD", "USD_JPY": "USD_JPY",
    "USD_CHF": "USD_CHF", "AUD_USD": "AUD_USD", "NZD_USD": "NZD_USD",
    "GBP_JPY": "GBP_JPY", "EUR_JPY": "EUR_JPY", "EUR_GBP": "EUR_GBP",
    "XAU_USD": "XAU_USD", "XAG_USD": "XAG_USD", "BCO_USD": "BCO_USD",
    "BTC_USD": "BTC_USD", "ETH_USD": "ETH_USD", "WTICO_USD": "WTICO_USD",
    "NATGAS_USD": "NATGAS_USD", "CORN_USD": "CORN_USD",
    # Pool-expansion instruments — without these, _infer_instrument falls
    # back to EUR_USD and the portfolio reconstructs these sleeves' returns
    # (hence inverse-vol weights / correlation haircuts) on the WRONG data.
    "SOYBN_USD": "SOYBN_USD", "WHEAT_USD": "WHEAT_USD", "LTC_USD": "LTC_USD",
    "XCU_USD": "XCU_USD", "XPT_USD": "XPT_USD", "XPD_USD": "XPD_USD",
    "SPX500_USD": "SPX500_USD", "NAS100_USD": "NAS100_USD", "US30_USD": "US30_USD",
    "DE30_EUR": "DE30_EUR", "UK100_GBP": "UK100_GBP", "JP225_USD": "JP225_USD",
    "AU200_AUD": "AU200_AUD", "HK33_HKD": "HK33_HKD", "CN50_USD": "CN50_USD",
}
_IMAP = {
    "EURUSD": "EUR_USD", "GBPUSD": "GBP_USD", "USDJPY": "USD_JPY",
    "USDCHF": "USD_CHF", "AUDUSD": "AUD_USD", "NZDUSD": "NZD_USD",
    "GBPJPY": "GBP_JPY", "EURJPY": "EUR_JPY", "EURGBP": "EUR_GBP",
    "XAUUSD": "XAU_USD", "XAGUSD": "XAG_USD", "BCOUSD": "BCO_USD",
    "BTCUSD": "BTC_USD", "ETHUSD": "ETH_USD", "WTICOUSD": "WTICO_USD",
    "NATGASUSD": "NATGAS_USD", "CORNUSD": "CORN_USD",
    # Pool-expansion (concatenated form used by the strategy-id prefix)
    "SOYBNUSD": "SOYBN_USD", "WHEATUSD": "WHEAT_USD", "LTCUSD": "LTC_USD",
    "XCUUSD": "XCU_USD", "XPTUSD": "XPT_USD", "XPDUSD": "XPD_USD",
    "SPX500USD": "SPX500_USD", "NAS100USD": "NAS100_USD", "US30USD": "US30_USD",
    "DE30EUR": "DE30_EUR", "UK100GBP": "UK100_GBP", "JP225USD": "JP225_USD",
    "AU200AUD": "AU200_AUD", "HK33HKD": "HK33_HKD", "CN50USD": "CN50_USD",
}


def _infer_instrument(sid: str) -> str:
    upper = sid.upper()
    for prefix, inst in _PMAP.items():
        if upper.startswith(prefix.replace("_", "") + "_") or upper.startswith(prefix + "_"):
            return inst
    raw = sid.split("_auto_")[0].upper().replace("_", "")
    return _IMAP.get(raw, "EUR_USD")


# ---------------------------------------------------------------------------
# Data loading
# ---------------------------------------------------------------------------

def load_strategies(min_wf: float = 0.0) -> List[Dict]:
    """Load currently-trading strategies for portfolio sizing.

    Only status='paper_trading' — i.e. strategies actually placing orders.
    'passed'/'passed_but_fragile' are validated-but-not-deployed candidates;
    including them let non-trading strategies absorb weight budget and, worse,
    let a live strategy get correlation-haircut against a parked one it will
    never actually conflict with. Deployed strategies are always 'paper_trading'
    (start_live_trading sets it), and the fragility haircut keys on torture_flags
    rather than status, so nothing is lost by this filter.
    """
    init_db()
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    rows = conn.execute("""
        SELECT s.id, s.timeframe, s.code, s.status,
               s.instrument, s.archetype, s.instrument2,
               vr.best_params, vr.walk_forward_gt_score,
               vr.is_gt_score, vr.torture_flags
        FROM strategies s
        JOIN validation_results vr ON s.id = vr.strategy_id
        WHERE s.status = 'paper_trading'
          AND vr.walk_forward_gt_score >= ?
        ORDER BY vr.walk_forward_gt_score DESC
    """, (min_wf,)).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def _infer_archetype(code: str, declared: str = "standard") -> str:
    """Derive the strategy archetype from the columns the code actually
    references — mirrors auto_research._infer_archetype / live_test._infer_archetype.

    The strategies table doesn't persist archetype (it's only baked into the
    fingerprint), so the portfolio combiner has to recover it from the code.
    Without this, inject_supplementary_data is skipped and a macro strategy
    KeyErrors on df['dxy'] (etc.), the bare except swallows it, and the
    strategy is silently dropped from portfolio_state.json.
    """
    from macro_fetcher import ALL_MACRO_COLS
    from supplementary_data import CALENDAR_COLS
    refs = set(re.findall(r'df\[["\'](\w+)["\']\]', code or ""))
    if refs & ALL_MACRO_COLS:
        return "macro"
    if refs & CALENDAR_COLS:
        return "calendar"
    if "session" in refs:
        return "session"
    if refs & {"event_impact", "event_surprise", "days_to_event", "event_window"}:
        return "news"
    if "close_leg2" in refs:
        return "pair"
    if "spread" in refs:
        return "spread"
    return declared or "standard"


def _return_dates(data: pd.DataFrame, n_returns: int) -> pd.DatetimeIndex:
    dates = pd.DatetimeIndex(pd.to_datetime(data["date"]))
    if n_returns == len(dates):
        return dates
    if n_returns == len(dates) - 1:
        return dates[1:]
    raise ValueError(f"cannot align {n_returns} returns to {len(dates)} candles")


def build_strategy_returns(
    row: Dict,
    start: str,
    end: str,
) -> Optional[Tuple[pd.Series, pd.Series]]:
    """
    Reconstruct daily strategy returns + bar-aligned signals from code + best_params
    over [start, end]. Returns (daily_returns, bar_signals) or None.
    """
    sid        = row["id"]
    tf         = row["timeframe"] or "D"
    code       = row["code"]
    best_params = json.loads(row["best_params"] or "{}")
    instrument  = row.get("instrument") or _infer_instrument(sid)

    if not best_params:
        return None

    # Load strategy function
    try:
        ns: Dict = {}
        exec(compile(code, "<strategy>", "exec"), ns)
        strategy_func = ns.get("generate_signals")
        if strategy_func is None:
            return None
    except Exception as e:
        print(f"  [build_strategy_returns] {sid}: code load failed: {e}", file=sys.stderr)
        return None

    # Fetch candle data. OANDA history is instrument-dependent (crypto starts much
    # later than 2015), so clamp the requested start to the validator's instrument-
    # aware dev window start. Keep the caller's end so decay uses recent-through-now.
    fetch_start = max(start, get_dev_window(instrument)[0])
    safe_end = min(end, (datetime.utcnow() - timedelta(days=1)).strftime('%Y-%m-%d'))
    try:
        data = get_candles_date_range(instrument, fetch_start, safe_end, granularity=tf)
        if len(data) < MIN_BARS:
            return None
    except Exception as e:
        print(f"  [build_strategy_returns] {sid}: data fetch failed: {e}", file=sys.stderr)
        return None

    # Inject supplementary columns for non-standard archetypes (macro dxy/yields,
    # session labels, news events, pair/spread). Prefer persisted validator
    # metadata; fall back to code inference for legacy rows.
    inferred_archetype = _infer_archetype(code)
    stored_archetype = row.get("archetype")
    archetype = stored_archetype if stored_archetype and stored_archetype != "standard" else inferred_archetype
    if archetype != "standard":
        try:
            data = inject_supplementary_data(
                data, archetype, instrument, row.get("instrument2"), fetch_start, safe_end, tf
            )
        except Exception as e:
            print(f"  [build_strategy_returns] {sid}: supplementary injection "
                  f"failed for archetype '{archetype}': {e}", file=sys.stderr)
            return None

    # Generate signals and compute returns
    try:
        signals = strategy_func(data, best_params)
        returns = compute_net_strategy_returns(data, signals, instrument, tf, params=best_params)
    except Exception as e:
        print(f"  [build_strategy_returns] {sid}: signal/return computation "
              f"failed: {e}", file=sys.stderr)
        return None

    if returns is None or len(returns) == 0 or (signals != 0).sum() == 0:
        return None

    # Attach dates and resample to daily (sum intraday bars per calendar day)
    returns = returns.copy()
    returns.index = _return_dates(data, len(returns))

    # Normalise to UTC date (strip time/tz)
    returns.index = returns.index.normalize().tz_localize(None)

    # For intraday strategies (H4/H1) sum bars within the same day
    daily = returns.groupby(returns.index).sum()
    daily.name = sid

    sig = pd.Series(np.asarray(signals), index=pd.to_datetime(data["date"]).values).fillna(0)
    sig.index = pd.to_datetime(sig.index).normalize().tz_localize(None)
    return daily, sig


# ---------------------------------------------------------------------------
# Log-based returns (supplement / cross-check)
# ---------------------------------------------------------------------------

def parse_log_returns(sid: str) -> Optional[pd.Series]:
    """
    Parse the STRATEGY's per-bar returns from a paper-trading log.

    New-format lines carry both the raw market move and the position-weighted
    result:  '[ts] [D] Bar return: -0.0222, Position: +0, P&L: -0.0000'
    The strategy's realized return is the P&L field (position x bar move) —
    'Bar return' is the raw instrument move and is nonzero even when the
    trader is FLAT, so parsing it conflated market noise with strategy
    performance (incubation's wheat false-negation, 2026-06-10).
    Old-format lines ('Daily return: ...') already carried the strategy
    return and keep their original parse.

    Returns a daily pd.Series or None if the log has < 3 daily entries.
    """
    log_path = os.path.join(LOG_DIR, f"{sid}.log")
    if not os.path.exists(log_path):
        return None

    records = []
    with open(log_path) as fh:
        for line in fh:
            # New format: [ts] [H4] Bar return: -0.0070, Position: +1, P&L: -0.0070
            # Old format: [2026-05-11] Daily return: -0.0042, ...
            if "Bar return:" in line or "Daily return:" in line:
                try:
                    ts_raw = line.split("]")[0].lstrip("[").strip()
                    ts = pd.to_datetime(ts_raw, utc=True).normalize().tz_localize(None)
                    if "P&L:" in line:
                        ret_str = line.split("P&L:")[-1]
                    else:
                        ret_str = line.split("Bar return:")[-1].split("Daily return:")[-1]
                    ret = float(ret_str.split(",")[0].strip())
                    records.append((ts, ret))
                except Exception:
                    pass

    if not records:
        return None

    df = pd.DataFrame(records, columns=["date", "ret"])
    daily = df.groupby("date")["ret"].sum()
    daily.name = sid
    return daily if len(daily) >= 3 else None


# ---------------------------------------------------------------------------
# Correlation analysis
# ---------------------------------------------------------------------------

def compute_correlation_matrix(returns_dict: Dict[str, pd.Series]) -> pd.DataFrame:
    """Align all series to a common daily index and compute Pearson correlation."""
    combined = pd.DataFrame(returns_dict)
    # Drop rows where ALL strategies are NaN (gaps in data)
    combined = combined.dropna(how="all")
    # Fill remaining NaN with 0 (strategy was flat / no signal that day)
    combined = combined.fillna(0.0)
    return combined.corr()


def _distinct_entry_indices(sig: pd.Series) -> np.ndarray:
    s = pd.Series(np.asarray(sig), index=sig.index).fillna(0)
    prev = s.shift(1).fillna(0)
    return np.flatnonzero((s != 0) & (s != prev))


def recent_decay_window(sig: pd.Series, asof=None) -> Dict[str, object]:
    """Positional start of the decay window, bounded by BOTH entry count and time.

    The window is the last RECENT_DECAY_ENTRIES entries OR the last
    RECENT_DECAY_MAX_MONTHS of calendar time — whichever is SHORTER. Bounding by
    entries alone is what let "recent" mean 7.4 years on a selective sleeve
    (see RECENT_DECAY_MAX_MONTHS).

    Returns start (positional, None when unscoreable), total entries, how many of
    them fall inside the window, and which bound is binding.
    """
    entries = _distinct_entry_indices(sig)
    total = int(len(entries))
    empty = {'start': None, 'entries': total, 'in_window': 0, 'capped_by': None}
    if total == 0:
        return empty
    idx = sig.index
    if asof is not None:                      # point-in-time: ignore later entries
        entries = entries[pd.DatetimeIndex(idx[entries]) <= asof]
        if len(entries) == 0:
            return empty
    start = int(entries[-RECENT_DECAY_ENTRIES]) if len(entries) >= RECENT_DECAY_ENTRIES \
        else int(entries[0])
    capped_by = 'entries'
    if isinstance(idx, pd.DatetimeIndex) and len(idx):
        end = pd.Timestamp(asof if asof is not None else idx[-1])
        cutoff = end - pd.DateOffset(months=RECENT_DECAY_MAX_MONTHS)
        start_cal = int(idx.searchsorted(cutoff, side='left'))
        if start_cal > start:                 # calendar bound is the tighter one
            start, capped_by = start_cal, 'calendar'
    return {'start': start, 'entries': total,
            'in_window': int((entries >= start).sum()), 'capped_by': capped_by}


def recent_decay_status(ret: pd.Series, sig: pd.Series, wf_score: float) -> Dict[str, object]:
    w = recent_decay_window(sig)
    if w['start'] is None or w['in_window'] < RECENT_DECAY_MIN_ENTRIES:
        return {'status': 'INSUFFICIENT', 'entries': w['entries'],
                'in_window': w['in_window'], 'kelly_scale': 1.0}
    recent = ret.iloc[w['start']:].fillna(0.0)
    recent_gt = compute_gt_score(recent)
    recent_ret = float((1 + recent).prod() - 1) if len(recent) else 0.0
    min_gt = max(0.0, float(wf_score or 0.0) * RECENT_DECAY_GT_FRACTION)
    decayed = recent_ret <= 0 or recent_gt < min_gt
    status, note = ('DECAYED' if decayed else 'OK'), None
    if decayed and recent_ret > 0 and min_gt > 0 \
            and recent_gt >= min_gt * RECENT_DECAY_NEAR_MISS_FRACTION:
        status = 'INSUFFICIENT'
        note = (f'near-miss decay: GT {recent_gt:.3f} vs min {min_gt:.3f} '
                f'({recent_gt / min_gt:.0%} of threshold), window return '
                f'{recent_ret * 100:+.1f}% — inside threshold noise, not decay')
        decayed = False
    return {
        'status': status,
        'note': note,
        'entries': w['entries'],
        'in_window': w['in_window'],
        'capped_by': w['capped_by'],
        'recent_ret': recent_ret,
        'recent_gt': float(recent_gt),
        'min_gt': float(min_gt),
        'kelly_scale': DECAY_KELLY_SCALE if decayed else 1.0,
    }


def kelly_scale(ret: pd.Series) -> pd.Series:
    index = ret.index
    scale = pd.Series(1.0, index=index)
    active_seen = []
    mult = 1.0
    for i, dt in enumerate(index):
        if i == 0 or i % 21 == 0:
            active = pd.Series(active_seen[-60:])
            if len(active) < 30:
                mult = 0.5
            else:
                wins = active[active > 0]
                losses = active[active < 0]
                if len(wins) == 0 or len(losses) == 0:
                    mult = 1.0 if len(wins) else 0.5
                else:
                    wr = len(wins) / len(active)
                    avg_w = wins.mean()
                    avg_l = abs(losses.mean())
                    b = avg_w / avg_l if avg_l > 0 else 0
                    mult = 2.0 if (wr - (1 - wr) / b if b > 0 else 0) > 0 else 0.5
        scale.loc[dt] = mult
        r = float(ret.loc[dt])
        if r != 0.0:
            active_seen.append(r)
    return scale


def scheduled_decay_scale(ret: pd.Series, sig: pd.Series, wf_score: float) -> pd.Series:
    index = ret.index
    scale = pd.Series(1.0, index=index)
    entries = _distinct_entry_indices(sig)
    if len(entries) < RECENT_DECAY_MIN_ENTRIES:
        return scale
    entry_dates = pd.DatetimeIndex(sig.index[entries])
    check_date = entry_dates[RECENT_DECAY_MIN_ENTRIES - 1]
    end = index.max()
    while check_date <= end:
        # Same calendar+entry bound as the live verdict, evaluated point-in-time so
        # the historical conviction schedule reflects the rule actually in force.
        w = recent_decay_window(sig, asof=check_date)
        if w['start'] is not None and w['in_window'] >= RECENT_DECAY_MIN_ENTRIES:
            start = w['start']
            recent = ret.iloc[start:].loc[:check_date].fillna(0.0)
            recent_gt = compute_gt_score(recent)
            recent_ret = float((1 + recent).prod() - 1) if len(recent) else 0.0
            min_gt = max(0.0, float(wf_score or 0.0) * RECENT_DECAY_GT_FRACTION)
            if recent_ret <= 0 or recent_gt < min_gt:
                scale.loc[(index >= check_date) & (index < check_date + pd.Timedelta(days=21))] = DECAY_CONVICTION_SCALE
        check_date += pd.Timedelta(days=21)
    return scale


def decay_conviction_scale(status: str) -> float:
    return DECAY_CONVICTION_SCALE if status == 'DECAYED' else 1.0


def decay_kelly_scale(status: str) -> float:
    return DECAY_KELLY_SCALE if status == 'DECAYED' else 1.0


def flag_correlated_pairs(
    corr: pd.DataFrame,
    wf_scores: Dict[str, float],
    threshold: float = CORR_THRESHOLD,
) -> List[Tuple[str, str, float, str]]:
    """
    Find pairs with |correlation| > threshold.
    Returns list of (sid_a, sid_b, corr_value, suggestion).
    """
    sids  = list(corr.columns)
    flags = []
    for i in range(len(sids)):
        for j in range(i + 1, len(sids)):
            a, b = sids[i], sids[j]
            c    = corr.loc[a, b]
            if abs(c) > threshold:
                weaker = a if wf_scores.get(a, 0) < wf_scores.get(b, 0) else b
                flags.append((a, b, c, weaker))
    return flags


# ---------------------------------------------------------------------------
# Inverse-volatility weights
# ---------------------------------------------------------------------------

# Manual per-sleeve conviction multipliers applied to the inverse-vol weights.
# A value < 1.0 shrinks that sleeve's allocation (and its live risk-per-trade)
# and PERSISTS across rebalances. Use for low-conviction diversifiers we want in
# the book but small.
CONVICTION = {
    'eurjpy_auto_20260606_081416_i8': 0.18,  # low-conviction diversifier, deployed small 2026-06-22 (~0.4x weight_scale; it's the lowest-vol sleeve so inverse-vol would otherwise over-weight it)
    'xagusd_auto_20260605_092503_i11': 0.7,  # re-deployed 2026-06-22 as a reasonable bet — marginal WF 0.47 but strong HO 0.81; ~0.5x weight_scale to give the edge a real live test now that execution works
    # nas100usd_auto_20260622_223358_i3 RETIRED 2026-07-08 — swapped out for the 20260708 i9 sleeve, which dominates it on total return in EVERY period (full +136% vs +114%, recent 2021-26 +34% vs +25%) AND is two-sided (won the 2018/2022/2024 down years i3 lost), adding the NAS cluster's only short-hedge. Clean look-ahead. i3 was flat at retirement.
    'gbpjpy_auto_20260622_173325_i5': 0.25,  # non-equity diversifier, Sharpe 0.88, uncorrelated (+0.03 w/ existing GBP/JPY); ~0.5x weight_scale — discounted for HO-at-floor decay + spurious rationale; low-vol so needs a small multiplier
    'de30eur_auto_20260623_163152_i1': 0.4,  # strong skew-reversion (Sharpe 1.24, HO 1.23), uncorrelated (+0.14 w/ existing DE30 i27) despite being equity; ~0.5x weight_scale
    'wheatusd_auto_20260630_155412_i15': 0.5,  # deploy-small 2026-06-30: best diversifier in book (+0.06 max corr, only agricultural), GT-pick generalized best-in-grid (HO Sharpe 1.04). Discounted for thin standalone edge (Sharpe ~0.48, long-only, regime-flattered HO). Targets ~0.4x weight_scale (~0.2%/trade); high 25.9% vol so inverse-vol trims it from 0.80x already. DSR=0.04 was an axis-mismatch artifact (real Sharpe-selected grid DSR 0.72), NOT a red flag.
    'xptusd_auto_20260709_235647_i25': 0.25,  # DEPLOYED small 2026-07-09: the book's FIRST event/news sleeve (family revived this session via the forced event slot). Platinum pre-release compression mean-reversion: Sharpe ~2.0, maxDD -6%, 11/12 +yrs, uncorrelated (max |corr| 0.04 vs book), clean look-ahead (4%). SMALL because: very LOW activity (4% in-mkt ~= ~40 trades over 11y, noisy stats) + UNPROVEN-LIVE new family. NOTE: headline HO 17.02 is a few-trade Sortino-cap artifact, NOT a real 17x edge — sized on the honest Sharpe ~2.0, not the HO. Conviction only 0.25 because it's flat 96% of the time → tiny realized vol → inverse-vol massively over-weights it (0.5 landed 0.79x); 0.25 targets ~0.4x. Size up once it proves out live.
    'xpdusd_auto_20260708_090851_i16': 0.7,  # 2026-07-08: the book's FIRST cross-market/pair sleeve + first palladium. Pd/Pt autocatalyst-substitution spread mean-reversion: two-sided (301 long/298 short), selective (20% in-mkt), Sharpe 1.50, conc 10%, 10/12 +yrs, +25% 12mo, clean look-ahead (3%). BEST diversifier in book: max |corr| 0.07 vs all 22 sleeves. Capped 1.0->0.7 (~0.6x) because it's an UNPROVEN-LIVE new family on a THIN/high-vol instrument (2-leg spread = double cost) — watch live fills during incubation; size up once it proves out.
    'eurusd_auto_20260722_043021_i25': 0.25,
    'gbpusd_auto_20260722_174842_i25': 0.5,
    'nas100usd_auto_20260701_011303_i9': 0.17,  # REVIEW 2026-07-11: 6mo Sharpe -2.61 (12mo +1.01 = temporary). Halved from 0.33.
    'xcuusd_auto_20260704_222307_i17': 0.08,  # LOW_DATA 2026-07-11: only 21 active days in 12mo, 6mo Sharpe -2.62. Halved from 0.15 — not enough trades to evaluate.
    # xcuusd_auto_20260701_072807_i17 RETIRED 2026-07-08 — look-ahead gate FAIL (30% truncation flips, causal edge collapses ~100%). Its WF 1.33/HO 1.44 were inflated by a non-causal signal; live trades the collapsed (≈0) causal edge. Scan-and-fill/future-peek, unsalvageable without a code rewrite.
    'xcuusd_auto_20260709_134032_i5': 0.25,  # LOW_DATA 2026-07-11: only 22 active days in 12mo, 6mo Sharpe -4.00. Halved from 0.5.
    'xcuusd_auto_20260606_222204_i27': 0.2,  # REVIEW 2026-07-11: 6mo Sharpe -1.21 (12mo +1.46 = temporary). Halved from 0.4.
    # usdchf_auto_20260706_133908_i21 RETIRED 2026-07-11 — sleeve_health RETIRE: 6mo Sharpe -0.32 AND 12mo Sharpe -0.61 (both windows negative = structural decay). Was the momentum leg of the paired USD/CHF; reversion leg i31 kept (12mo +1.48). REDEPLOYED since (live again as of 2026-08-04) — see the conviction entry below.
    'usdchf_auto_20260706_133908_i21': 0.5,  # REVIEW 2026-08-04: halved from default 1.0 on a RECENT30 NEAR-MISS (GT 0.389 vs min 0.403, window ret +2.9%, 12mo -3.6%, 26wk -0.7%). NOT retired, deliberately: causal_audit collapse 0% (25 trades, full -5.4% = causal -5.4%), look-ahead flip 0/120, BOTH legs standalone positive (long Sharpe 0.29 / short 0.66), and it is the ONLY USD_CHF momentum sleeve at max |corr| 0.20 vs the whole book — retiring would cost real diversification. Trim expresses the marginal decay read without losing the slot. CAVEAT: fx is PINNED at cap_frac (0.08 at n=25), so this does NOT reduce book risk — it moves weight to the other 8 fx sleeves. Revisit if the next scan reads DECAYED outright rather than near-miss.
    'usdchf_auto_20260706_000155_i31': 0.10,  # PAIRED with i21 (was 0.13, trimmed): the RSI-reversion leg. Flat over 3y (Sharpe 0.02) but positive RIGHT NOW (12mo +4.1%) and uncorrelated w/ i21 (-0.01) — kept as the ~40% recency hedge / ranging-regime complement to i21's momentum. Total USD/CHF nominal capped ~0.8x across the pair (SNB-shock tail hits both).
    'eurusd_auto_20260703_212244_i29': 0.13,  # DEPLOYED small 2026-07-04: clean (0/92 truncation look-ahead), uncorrelated (max |corr| 0.06), NEW instrument (EUR/USD, most liquid pair). Selective RSI-dip reversion, no HO decay (Sharpe 2.05), 10/12 +yrs, beats drift (+2.8% vs -3.1% 12mo). Discounted: 86% long-biased dip-buyer (OK — EUR/USD non-drifting, not equity beta) + instrument_transfer flag (generic edge) + Nth reversion idea. Low-vol pair -> inverse-vol over-weights, so 0.13 like eurgbp targets ~0.4x.
    'eurgbp_auto_20260702_205405_i11': 0.13,  # DEPLOYED small 2026-07-02: most uncorrelated candidate yet (max |corr| 0.07 vs whole book), first EUR_GBP, genuinely two-sided extreme-fade (long +0.32/short +0.07 both profitable), 10/12 +years, HO Sharpe 1.60 no decay. Small patient edge (WF 0.51 floor, ~4.6 entries/yr) — diversification slot, not firepower. Low-vol pair -> inverse-vol over-weights (eurjpy i8 trap), 0.2 landed 0.636x so trimmed to 0.13 -> ~0.41x (~0.21%/trade).
    'au200aud_auto_20260702_103247_i15': 0.3,  # DEPLOYED small 2026-07-02: best equity profile in book — 12/12 +years, top2yr 31%, HO Sharpe 1.91 (no decay), beats AU200 4x in a flat index year (alpha not drift despite LONG-ONLY). Uncorrelated with all 6 equity incumbents (max +0.28 de30 i27). Capped at equity-norm (conviction 0.3 -> ~0.55x; 0.5 landed 0.91x because its low strategy-vol is inverse-vol over-weighted, same as nas i9): 7th equity sleeve (book equity ~37%) + long-only + the Bollinger-band code bug (bb_std^2 constant offset — real rule is SMA20-dip in range regime; edge belongs to the buggy rule, live runs same code).
    'btcusd_auto_20260708_115225_i9': 0.75,  # DEPLOYED small 2026-07-08: first BTC macro sleeve, two-sided (long PF 1.53 / short PF 1.29 by active day) and near-zero corr to current book. Kept small because conc=80%, DSR=0.11(desc), WF only modestly above floor, and standalone path has ugly raw DD; role is uncorrelated incubation, not firepower.
    'nas100usd_auto_20260708_103624_i9': 0.24,  # REVIEW 2026-07-11: 6mo Sharpe -0.38 (12mo +0.86 = temporary). Halved from 0.48.
    # soybnusd_auto_20260701_142143_i17 RETIRED 2026-07-08 — look-ahead gate FAIL (91% truncation flips, causal edge collapses 87%). WF 0.88/HO 0.82 were inflated by a scan-and-fill exit (pos[i:exit]=dir where exit is found by scanning FORWARD); live trades the collapsed causal edge. Unsalvageable without a code rewrite.
    'spx500usd_auto_20260605_235436_i21': 0.5,  # REVIEW 2026-07-11: 6mo Sharpe -3.16 (12mo +0.08 barely positive). Halved from default 1.0.
    'de30eur_auto_20260619_182518_i27': 0.5,  # REVIEW 2026-07-11: 6mo Sharpe -1.84 (12mo +0.15 barely positive). Halved from default 1.0.
    'gbpjpy_auto_20260611_010859_i8': 0.2,  # REVIEW 2026-07-27: still RECENT30 DECAYED — recent GT 0.105 vs min 0.346, 12mo -0.87%. Kept (flat, not bleeding) but cut 0.5 -> 0.2; DECAY_CONVICTION_SCALE halves it again to 0.1 effective, and decay_kelly_scale 0.5 applies at runtime. Retire if the next scan still shows recent GT below floor. (Was 0.5 from the 2026-07-11 review: 6mo Sharpe -1.51.)
    'soybnusd_auto_20260708_071712_i31': 0.5,  # DEPLOYED 2026-07-11 via sleeve_health --rank: #1 candidate, MargSh +0.49, diversity 0.97 (max |corr| 0.03), WF 3.12/HO 3.39 (robust), 6mo Sharpe +6.48. First soybean. Start small — unproven live.
    'natgasusd_auto_20260706_225309_i15': 0.5,  # DEPLOYED 2026-07-11 via sleeve_health --rank: #2 candidate, MargSh +0.33, diversity 0.96 (max |corr| 0.04), WF 0.77/HO 1.55, 6mo Sharpe +4.87. First nat gas. Start small — unproven live.
    'eurusd_auto_20260711_211818_i9': 0.15,  # LOW_DATA deploy: macro yield-diff, 12 trades/yr — not enough to judge decay. Low conviction, let Kelly sort it.
    'de30eur_auto_20260711_193840_i19': 0.7,  # Pair (DE30/SPX z-score MR): no decay, rolling GT 1.5–1.9, 55 trades/yr. Strong but unproven live.
    # RETIRED 2026-07-27 — RECENT30 recent GT 0.000 vs min 0.331, 12mo -7.20%; flattened -1.8070 @ 25182.1 (pl -105.74).
    # Entry kept (inert once retired) so a future redeploy doesn't silently start at full conviction.
    'hk33hkd_auto_20260711_211002_i27': 0.2,  # Macro (real yield+DXY): 6mo GT flat, 3mo recovering. First HK33. Low conviction, let Kelly sort it.
    'gbpusd_auto_20260713_170703_i5': 0.11,  # DEPLOYED small 2026-07-14: first GBP/USD, news-overreaction MR, genuinely two-sided (long +13%/Sh 0.59, short +12%/Sh 0.54 — both legs profitable), Sharpe 0.80, maxDD -5%, clean look-ahead (0%), max |corr| 0.11 (new instrument diversifier). Low-vol (8.3%) + very selective (8% in-mkt, hold_bars=2) so inverse-vol over-weights it (eurgbp/eurusd trap): 0.2 landed 0.93x, trimmed to 0.11 → ~0.5x (~0.25%/trade). Quiet edge (12mo +1.2%, thin firepower) — diversification slot, size up if it proves live.
    'ethusd_auto_20260717_195112_i23': 0.4,  # DEPLOYED small 2026-07-18: 2nd crypto, GENUINELY two-sided (long Sh 1.27/7-7 yrs, short Sh 0.66/+110% — both legs positive, +52% in the 2022 ETH crash = real short edge not beta), selective 16% in-mkt (clean exit), conc 20%, WF 0.81/HO 0.64, look-ahead 0%. Best diversifier: max |corr| 0.06, only 0.03 vs the BTC sleeve. Kept small (like btc i9) for the ugly -30% RAW standalone DD; targets ~0.25x. Size up if it proves live.
    'gbpusd_auto_20260720_151739_i5': 0.4,  # DEPLOYED small 2026-07-20: GBP/USD post-US-event fade, clean look-ahead, 111 trades, maxDD -5%, low book corr. Small because WF 0.57 floor-pass, DSR 0.07(desc), and only 4 trades in trailing 12mo.
    'xauusd_auto_20260720_214522_i21': 0.2,  # DEPLOYED small 2026-07-21: XAU macro real-yield/DXY trend, WF 0.61/HO 1.24, recent30 OK, max |corr| 0.30. Small because DSR 0.00(desc), conc 77%, raw DD -25%.
}


def inverse_vol_weights(returns_dict: Dict[str, pd.Series], signals_dict: Dict[str, pd.Series] = None, wf_scores: Dict[str, float] = None, apply_decay: bool = True) -> Dict[str, float]:
    """
    Compute inverse-volatility weights (annualised daily vol) so each strategy
    contributes approximately equal risk to the portfolio.
    """
    signals_dict = signals_dict or {sid: pd.Series(0, index=ret.index) for sid, ret in returns_dict.items()}
    wf_scores = wf_scores or {}
    vols = {}
    decay = {}
    for sid, ret in returns_dict.items():
        r = ret.dropna()
        r = r[r != 0.0]          # exclude flat (no-trade) days
        if len(r) < 5:
            vols[sid] = np.nan
        else:
            vols[sid] = float(r.std() * np.sqrt(252))
        decay[sid] = recent_decay_status(ret.fillna(0.0), signals_dict[sid], wf_scores.get(sid, 0.0))

    # THE NaN GUARD, shared by both schemes and load-bearing in both. vols[sid] is
    # NaN when a sleeve has under 5 in-position days — no measurable track record —
    # and the guard turns that into zero weight rather than a division. It is not an
    # artefact of the inverse-vol maths: under equal weight it is the ONLY thing
    # stopping a sleeve that has never traded from taking a full share on no
    # evidence. wheatusd_auto_20260630_155412_i15 has zero in-position days over the
    # current window and is exactly this case.
    _has_history = lambda v: bool(v and not np.isnan(v) and v > 0)

    if WEIGHTING == 'equal':
        # EQUAL WEIGHT. Measured out of sample (fit 2024-01..2025-04, scored on the
        # next sixteen months) this returns ~45% more per unit of tail than the
        # inverse-vol x conviction vector below, and it has ZERO fitted parameters,
        # which is why it survives the split test when ERC and min-CVaR do not.
        #
        # The defect it removes is anti-selection, not double-counted volatility:
        # the pre-conviction layer is vol-neutral (corr -0.007 with sleeve vol) but
        # gives LESS weight to higher-Sharpe sleeves (-0.382), conviction pushes back
        # (+0.248) while tilting toward higher-VOL ones (+0.358), and what survives
        # is a 25x spread correlated with sleeve quality at -0.058. Mean pairwise
        # sleeve correlation is +0.021 — near-independence is this book's largest
        # asset and the weight stack was spending it.
        #
        # CONVICTION and decay_conviction_scale are deliberately BYPASSED here.
        # The conviction trims are either corrections for inverse-vol over-weighting
        # (whose comments back-solve a target weight_scale — "0.2 landed 0.636x so
        # trimmed to 0.13") or reactions to trailing 6mo Sharpe, which is measurably
        # ANTI-predictive of forward 3mo Sharpe. Neither applies to a flat vector.
        # decay_kelly_scale is UNAFFECTED and still sizes at runtime.
        inv_vols = {sid: (1.0 if _has_history(v) else 0.0) for sid, v in vols.items()}
    elif WEIGHTING == 'invvol':
        inv_vols = {sid: (1.0 / v) if _has_history(v) else 0.0
                    for sid, v in vols.items()}
        # Apply manual conviction multipliers (default 1.0) before normalising.
        inv_vols = {
            sid: iv * CONVICTION.get(sid, 1.0) * decay_conviction_scale(decay[sid]['status'])
            for sid, iv in inv_vols.items()
        }
    else:
        # Never fall back silently: an unrecognised value would otherwise pick a
        # book magnitude nobody chose, which is the same class of failure as the
        # FIX_RISK alias that sized a local book at 0.2% while the pod ran 0.5%.
        raise ValueError(
            f"WEIGHTING={WEIGHTING!r} is not a known scheme — use 'invvol' or 'equal'")
    total = sum(inv_vols.values())
    if total == 0:
        n = len(inv_vols)
        return ({sid: 1.0 / n for sid in inv_vols}, vols, decay)

    weights = {sid: v / total for sid, v in inv_vols.items()}
    return weights, vols, decay


# Per-instrument-cluster weight_scale cap. Pairwise correlation stays low (~0.07)
# but the equity and precious-metals clusters align in tails and drove the book's
# drawdown; capping aggregate cluster weight_scale at 3 keeps worst-day intraday
# under the 5% prop limit (backtested: uncapped -5.6%/day -> cap3 -4.1%/day, DD
# -6.6%->-3.6%, Sharpe up). ponytail: static instrument->cluster map; extend the
# dict when a new instrument class is added.
CLUSTER_CAP = float(os.getenv('CLUSTER_CAP', '3.0'))   # per-cluster weight_scale cap; .env override (The5ers deploy uses 2.0)

# Allocation scheme. 'invvol' (default) is the incumbent inverse-vol x conviction
# vector; 'equal' gives every sleeve with a track record the same weight. Defaults
# to the incumbent so this code change is INERT until an env says otherwise, and so
# rollback is unsetting a variable rather than reverting a commit — the same
# discipline VENUE uses. See inverse_vol_weights for the measurement behind it.
#
# It is read where the STATE FILE is generated, not on the pod: the weight vector is
# baked into portfolio_state.json at write time and shipped, so this belongs wherever
# `portfolio.py --write` runs. The scheme is recorded in the state file so a given
# vector can always be traced to the rule that produced it.
WEIGHTING = os.getenv('WEIGHTING', 'invvol').strip().lower()
_CLUSTER = {
    'XAU_USD': 'metals', 'XAG_USD': 'metals', 'XPT_USD': 'metals', 'XPD_USD': 'metals',
    'XCU_USD': 'copper',
    'NAS100_USD': 'equity', 'SPX500_USD': 'equity', 'DE30_EUR': 'equity', 'HK33_HKD': 'equity',
    'AU200_AUD': 'equity', 'JP225_USD': 'equity', 'UK100_GBP': 'equity', 'CN50_USD': 'equity',
    'EUR_USD': 'fx', 'GBP_USD': 'fx', 'EUR_GBP': 'fx', 'EUR_JPY': 'fx', 'GBP_JPY': 'fx',
    'USD_CHF': 'fx', 'USD_JPY': 'fx', 'AUD_USD': 'fx', 'NZD_USD': 'fx',
    'WTICO_USD': 'energy', 'NATGAS_USD': 'energy', 'BCO_USD': 'energy',
    'WHEAT_USD': 'ags', 'SOYBN_USD': 'ags', 'CORN_USD': 'ags',
    'BTC_USD': 'crypto', 'ETH_USD': 'crypto', 'LTC_USD': 'crypto',
}


def _apply_cluster_caps(weights: Dict[str, float]) -> Dict[str, float]:
    """Scale down any instrument cluster whose summed weight_scale (own_weight x n)
    exceeds CLUSTER_CAP. No renormalise — the freed risk is dropped, not reshuffled
    into other clusters (that's the point: less concentrated tail exposure)."""
    n = len(weights)
    if n == 0:
        return weights
    cap_frac = CLUSTER_CAP / n
    csum: Dict[str, float] = {}
    for sid, w in weights.items():
        c = _CLUSTER.get(_infer_instrument(sid), 'other')
        csum[c] = csum.get(c, 0.0) + w
    return {sid: w * min(1.0, cap_frac / csum[_CLUSTER.get(_infer_instrument(sid), 'other')])
            for sid, w in weights.items()}


# ---------------------------------------------------------------------------
# Display helpers
# ---------------------------------------------------------------------------

def _short(sid: str, max_len: int = 36) -> str:
    return (sid[:max_len - 1] + "…") if len(sid) > max_len else sid


def print_correlation_matrix(corr: pd.DataFrame) -> None:
    sids = list(corr.columns)
    col_w = 10
    label_w = 38

    # Header
    print(f"\n{'Correlation Matrix':^{label_w + col_w * len(sids)}}")
    print("-" * (label_w + col_w * len(sids)))
    header = " " * label_w + "".join(f"{_short(s, col_w - 1):>{col_w}}" for s in sids)
    print(header)
    for a in sids:
        row = f"{_short(a, label_w - 1):<{label_w}}"
        for b in sids:
            c = corr.loc[a, b]
            flag = " !" if (a != b and abs(c) > CORR_THRESHOLD) else ""
            row += f"{c:>{col_w - len(flag)}.2f}{flag}"
        print(row)


def print_allocation_table(
    strategies: List[Dict],
    weights:    Dict[str, float],
    vols:       Dict[str, float],
    decay:      Dict[str, Dict[str, object]],
    corr_flags: List[Tuple],
    initial_equity: float = INITIAL_EQUITY,
) -> None:
    flagged = {a for a, b, c, w in corr_flags} | {b for a, b, c, w in corr_flags}
    fragile = {r["id"] for r in strategies if (r.get("torture_flags") or "[]") not in ("[]", "", None)
               and json.loads(r.get("torture_flags") or "[]")}

    print(f"\n{'─'*98}")
    print(f"{'Strategy':<38} {'Status':<20} {'WF':>6} {'Vol%':>7} {'Wt%':>6} {'Decay':<10} {'$Alloc':>9} {'Flags'}")
    print(f"{'─'*98}")

    for r in strategies:
        sid    = r["id"]
        status = r["status"]
        wf     = r["walk_forward_gt_score"] or 0.0
        wt     = weights.get(sid, 0.0)
        vol    = vols.get(sid, np.nan)
        alloc  = wt * initial_equity
        decay_st = decay.get(sid, {}).get('status', 'n/a')

        vol_str   = f"{vol*100:.1f}" if (vol and not np.isnan(vol)) else "N/A"
        flags_str = ""
        if sid in fragile:
            flags_str += " [fragile]"
        if sid in flagged:
            flags_str += " [corr!]"

        print(f"{_short(sid, 38):<38} {status:<20} {wf:>6.3f} {vol_str:>7} {wt*100:>5.1f}% "
              f"{decay_st:<10} ${alloc:>8,.0f}{flags_str}")

    print(f"{'─'*98}")
    total_alloc = sum(weights.get(r["id"], 0) * initial_equity for r in strategies)
    print(f"{'TOTAL':<38} {'':<20} {'':>6} {'':>7} {'100.0%':>6} {'':<10} ${total_alloc:>8,.0f}")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def missing_active_strategies(strategies: List[Dict], returns_dict: Dict[str, pd.Series]) -> List[str]:
    """Active (paper_trading) strategy ids that failed to reconstruct returns.

    These would be silently dropped from the portfolio weighting, so writing a
    portfolio_state.json without them zeroes out their live weight. The --write
    path aborts when this is non-empty (e.g. the rebalance ran without OANDA
    creds, a transient fetch error, or a code error), preserving the last-good
    weights instead of clobbering the live book with a partial one.
    """
    return [r["id"] for r in strategies if r["id"] not in returns_dict]


def main() -> None:
    parser = argparse.ArgumentParser(description="Portfolio combiner — correlation & allocation safety net")
    parser.add_argument("--start",        default=DEFAULT_START, help="Backtest start date (YYYY-MM-DD)")
    parser.add_argument("--end",          default=DEFAULT_END,   help="Backtest end date (YYYY-MM-DD)")
    parser.add_argument("--min-wf",       type=float, default=0.0, help="Minimum WF score to include (default: 0)")
    parser.add_argument("--corr-thresh",  type=float, default=CORR_THRESHOLD, help="Correlation flag threshold (default: 0.5)")
    parser.add_argument("--equity",       type=float, default=INITIAL_EQUITY, help="Portfolio equity for allocation (default: 100000)")
    parser.add_argument("--use-logs",     action="store_true", help="Supplement with live paper-trading log returns")
    parser.add_argument("--write",        action="store_true", help="Write portfolio_state.json for live_test.py to consume")
    parser.add_argument("--scheduled-decay", action="store_true", help="Backtest 30-entry decay with 21-day rechecks")
    parser.add_argument("--kelly", action="store_true", help="Apply live 60-trade Kelly multiplier, recomputed every 21 days")
    parser.add_argument("--warmup-days", type=int, default=730, help="Pre-start days used only to seed Kelly/decay state")
    args = parser.parse_args()

    print(f"\n{'='*80}")
    print(f"  Portfolio Combiner")
    print(f"  Period: {args.start} → {args.end}  |  Corr threshold: {args.corr_thresh}  |  Equity: ${args.equity:,.0f}")
    print(f"{'='*80}\n")

    # 1. Load strategies
    strategies = load_strategies(min_wf=args.min_wf)
    if not strategies:
        print("No strategies found. Run some validations first.")
        return

    print(f"Found {len(strategies)} strateg{'y' if len(strategies)==1 else 'ies'}:")
    for r in strategies:
        bp_ok  = bool(json.loads(r.get("best_params") or "{}"))
        bp_str = "✓ params" if bp_ok else "✗ no params"
        print(f"  {r['id'][:52]:<52} [{r['timeframe']:>2}]  WF={r['walk_forward_gt_score']:.3f}  {bp_str}")

    # 2. Build returns
    data_start = (pd.Timestamp(args.start) - pd.Timedelta(days=args.warmup_days)).strftime("%Y-%m-%d") if args.kelly else args.start
    print(f"\nReconstructing historical returns ({data_start} → {args.end})...")
    returns_dict: Dict[str, pd.Series] = {}
    signals_dict: Dict[str, pd.Series] = {}

    for r in strategies:
        sid = r["id"]
        sys.stdout.write(f"  {_short(sid, 48):<50} … ")
        sys.stdout.flush()

        built = build_strategy_returns(r, data_start, args.end)

        if built is None and args.use_logs:
            ret = parse_log_returns(sid)
            sig = None
        elif built is None:
            ret = None
            sig = None
        else:
            ret, sig = built

        if ret is not None and len(ret) >= MIN_BARS:
            returns_dict[sid] = ret
            if sig is not None:
                signals_dict[sid] = sig
            n_trades = int((ret != 0).sum())
            print(f"{len(ret)} daily bars  {n_trades} active days  "
                  f"ann-ret={ret.mean()*252*100:.1f}%")
        else:
            reason = "no params" if not json.loads(r.get("best_params") or "{}") else \
                     "< {} bars".format(MIN_BARS) if ret is not None else "load error"
            print(f"SKIP ({reason})")

    # Guard: never overwrite the live weight file with a PARTIAL book. If any
    # deployed strategy failed to reconstruct, dropping it would silently zero
    # its live weight (this happened when the rebalance ran without OANDA env
    # vars and the H4 sleeves couldn't fetch). Keep the last-good
    # portfolio_state.json and alert instead of clobbering it.
    missing = missing_active_strategies(strategies, returns_dict)
    if missing and args.write:
        msg = (f"{len(missing)}/{len(strategies)} live strategies failed to reconstruct "
               f"— refusing to overwrite portfolio_state.json with a partial book "
               f"(kept last-good weights). Missing: "
               f"{', '.join(_short(m, 28) for m in missing)}")
        print(f"\n🚨 ABORT: {msg}")
        try:
            from telegram_bot import notify_html
            notify_html(f"🚨 <b>Portfolio rebalance ABORTED</b>\n{msg}")
        except Exception as e:
            print(f"  (alert send failed: {e})")
        sys.exit(1)

    if len(returns_dict) < 2:
        print(f"\nNeed at least 2 strategies with returns to analyse correlation. "
              f"Got {len(returns_dict)}.")
        if len(returns_dict) == 1:
            sid = next(iter(returns_dict))
            ret = returns_dict[sid]
            vol = float(ret[ret != 0].std() * np.sqrt(252)) if len(ret[ret != 0]) > 5 else np.nan
            print(f"\nSingle strategy summary: {sid}")
            print(f"  Allocation: 100% = ${args.equity:,.0f}")
            vol_str = f"{vol*100:.1f}%" if not np.isnan(vol) else "N/A"
            print(f"  Annualised daily vol: {vol_str}")
        return

    wf_scores = {r["id"]: r["walk_forward_gt_score"] for r in strategies}

    # 3. Correlation matrix
    active_strategies = [r for r in strategies if r["id"] in returns_dict]
    corr = compute_correlation_matrix({sid: returns_dict[sid] for sid in returns_dict})
    print_correlation_matrix(corr)

    # 4. Flag correlated pairs
    corr_flags = flag_correlated_pairs(corr, wf_scores, threshold=args.corr_thresh)
    if corr_flags:
        print(f"\n⚠  High-correlation pairs (|corr| > {args.corr_thresh}):")
        for a, b, c, weaker in corr_flags:
            direction = "positively" if c > 0 else "negatively"
            print(f"  {_short(a, 38)}  ↔  {_short(b, 38)}")
            print(f"    corr={c:+.3f} ({direction} correlated)")
            print(f"    → Consider halving allocation to '{_short(weaker, 40)}' (lower WF score)")
    else:
        print(f"\n✓ No high-correlation pairs found (threshold: {args.corr_thresh})")

    # 5. Inverse-volatility weights
    weights, vols, decay = inverse_vol_weights(returns_dict, signals_dict, wf_scores)

    # Apply a 50% haircut to fragile and correlated strategies
    fragile_ids  = {r["id"] for r in strategies
                    if json.loads(r.get("torture_flags") or "[]")}
    flagged_ids  = {weaker for _, _, _, weaker in corr_flags}
    haircut_ids  = fragile_ids | flagged_ids

    if haircut_ids:
        for sid in haircut_ids:
            if sid in weights:
                weights[sid] *= 0.5
        total = sum(weights.values())
        if total > 0:
            weights = {sid: w / total for sid, w in weights.items()}

    # Cluster cap LAST (after any renormalise) so nothing re-inflates it.
    weights = _apply_cluster_caps(weights)

    # 6. Portfolio stats
    combined_daily = sum(
        returns_dict[sid] * weights.get(sid, 0)
        * (scheduled_decay_scale(returns_dict[sid], signals_dict[sid], wf_scores.get(sid, 0.0))
           if args.scheduled_decay else 1.0)
        * (kelly_scale(returns_dict[sid]) if args.kelly else 1.0)
        for sid in returns_dict
    )
    combined_daily = combined_daily.fillna(0)
    combined_daily = combined_daily.loc[pd.Timestamp(args.start):pd.Timestamp(args.end)]
    port_ann_ret   = combined_daily.mean() * 252
    port_ann_vol   = combined_daily[combined_daily != 0].std() * np.sqrt(252) \
                     if (combined_daily != 0).sum() > 5 else np.nan
    port_sharpe    = port_ann_ret / port_ann_vol if (port_ann_vol and not np.isnan(port_ann_vol)) else np.nan

    # Equity curve for max drawdown
    equity         = (1 + combined_daily).cumprod() * args.equity
    peak           = equity.cummax()
    drawdown       = (equity - peak) / peak
    max_dd         = float(drawdown.min())

    # 7. Print allocation table
    print_allocation_table(active_strategies, weights, vols, decay, corr_flags, args.equity)

    # 8. Portfolio summary
    print(f"\n{'─'*50}")
    print(f"  Portfolio Summary  ({args.start} → {args.end})")
    print(f"{'─'*50}")
    print(f"  Annualised return : {port_ann_ret*100:+.2f}%")
    vol_str = f"{port_ann_vol*100:.2f}%" if not np.isnan(port_ann_vol) else "N/A"
    print(f"  Annualised vol    : {vol_str}")
    sharpe_str = f"{port_sharpe:.3f}" if not np.isnan(port_sharpe) else "N/A"
    print(f"  Portfolio Sharpe  : {sharpe_str}")
    print(f"  Max drawdown      : {max_dd*100:.2f}%")
    if haircut_ids:
        print(f"\n  ⚠  50% haircut applied to: {', '.join(_short(s, 30) for s in sorted(haircut_ids))}")
        print(f"     (fragile torture flags or high correlation)")
    print(f"{'─'*50}\n")

    # 9. Write portfolio_state.json for live_test.py (if --write)
    if args.write:
        state = {
            "generated_at": datetime.utcnow().isoformat(),
            # Which rule produced this vector. Without it, an equal-weight and an
            # inverse-vol state file are indistinguishable once written, and the
            # book's magnitude would be unattributable after the fact.
            "weighting": WEIGHTING,
            "n_strategies": len(weights),
            "weights": weights,
            "decay_status": {sid: d.get('status') for sid, d in decay.items()},
            # Why a status was reached when it isn't self-evident (e.g. a
            # near-miss reported INSUFFICIENT rather than DECAYED).
            "decay_note": {sid: d['note'] for sid, d in decay.items() if d.get('note')},
            "decay_kelly_scale": {sid: float(d.get('kelly_scale', 1.0)) for sid, d in decay.items()},
            "correlated_pairs": [
                {"a": a, "b": b, "corr": round(float(c), 4), "weaker": weaker}
                for a, b, c, weaker in corr_flags
            ],
        }
        # ATOMIC. live_test re-reads this file on every evaluated bar, and this
        # writer runs at 00:05 while the book is live — a plain truncate-and-write
        # leaves a window where a reader sees half a JSON document. Write to a
        # temp file in the same directory (so os.replace stays on one filesystem)
        # and rename, which is atomic: a reader sees either the old file or the
        # new one, never a partial one.
        tmp = PORTFOLIO_STATE_FILE + ".tmp"
        with open(tmp, "w") as fh:
            json.dump(state, fh, indent=2)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, PORTFOLIO_STATE_FILE)
        print(f"✓ Wrote {PORTFOLIO_STATE_FILE}")
        print(f"  live_test.py picks these up on its next evaluated bar.\n")


if __name__ == "__main__":
    main()
