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
from datetime import datetime
from typing import Dict, List, Optional, Tuple

import numpy as np
import pandas as pd

warnings.filterwarnings("ignore", category=FutureWarning)
warnings.filterwarnings("ignore", category=UserWarning)

sys.path.insert(0, os.path.dirname(__file__))

from data_fetcher import get_candles_date_range
from pipeline_utils import compute_net_strategy_returns, compute_gt_score
from supplementary_data import inject_supplementary_data

# ---------------------------------------------------------------------------
# Configuration defaults
# ---------------------------------------------------------------------------
DEFAULT_START      = "2015-01-01"
DEFAULT_END        = "2024-01-01"
CORR_THRESHOLD     = 0.50   # |correlation| above this = flag
PORTFOLIO_STATE_FILE = os.path.join(os.path.dirname(__file__), "portfolio_state.json")
MIN_BARS           = 50     # skip strategies with fewer daily bars of history
INITIAL_EQUITY     = 100_000
DB_PATH            = os.path.join(os.path.dirname(__file__), "pipeline.db")
LOG_DIR            = os.path.join(os.path.dirname(__file__), ".paper-trading-logs")

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
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    rows = conn.execute("""
        SELECT s.id, s.timeframe, s.code, s.status,
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
    refs = set(re.findall(r'df\[["\'](\w+)["\']\]', code or ""))
    if refs & ALL_MACRO_COLS:
        return "macro"
    if "session" in refs:
        return "session"
    if refs & {"event_impact", "event_surprise"}:
        return "news"
    if "close_leg2" in refs:
        return "pair"
    if "spread" in refs:
        return "spread"
    return declared or "standard"


def build_strategy_returns(
    row: Dict,
    start: str,
    end: str,
) -> Optional[pd.Series]:
    """
    Reconstruct daily strategy returns from code + best_params over [start, end].
    Returns a pd.Series indexed by date (daily, position-aware, cost-adjusted).
    Returns None if strategy can't be loaded or has no trades.
    """
    sid        = row["id"]
    tf         = row["timeframe"] or "D"
    code       = row["code"]
    best_params = json.loads(row["best_params"] or "{}")
    instrument  = _infer_instrument(sid)

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

    # Fetch candle data
    try:
        data = get_candles_date_range(instrument, start, end, granularity=tf)
        if len(data) < MIN_BARS:
            return None
    except Exception as e:
        print(f"  [build_strategy_returns] {sid}: data fetch failed: {e}", file=sys.stderr)
        return None

    # Inject supplementary columns for non-standard archetypes (macro dxy/yields,
    # session labels, news events, pair/spread). Recovered from the code because
    # the strategies table doesn't store archetype. Mirrors the validator, which
    # injects via inject_supplementary_data when archetype != 'standard'.
    archetype = _infer_archetype(code)
    if archetype != "standard":
        try:
            # instrument2 (pair archetype) isn't derivable from code — pair
            # injection will raise here and the strategy stays skipped, but
            # with a logged reason instead of a silent drop.
            data = inject_supplementary_data(
                data, archetype, instrument, None, start, end, tf
            )
        except Exception as e:
            print(f"  [build_strategy_returns] {sid}: supplementary injection "
                  f"failed for archetype '{archetype}': {e}", file=sys.stderr)
            return None

    # Generate signals and compute returns
    try:
        signals = strategy_func(data, best_params)
        returns = compute_net_strategy_returns(data, signals, instrument, tf)
    except Exception as e:
        print(f"  [build_strategy_returns] {sid}: signal/return computation "
              f"failed: {e}", file=sys.stderr)
        return None

    if returns is None or len(returns) == 0 or (signals != 0).sum() == 0:
        return None

    # Attach dates and resample to daily (sum intraday bars per calendar day)
    returns = returns.copy()
    returns.index = pd.to_datetime(data["date"].values[: len(returns)])

    # Normalise to UTC date (strip time/tz)
    returns.index = returns.index.normalize().tz_localize(None)

    # For intraday strategies (H4/H1) sum bars within the same day
    daily = returns.groupby(returns.index).sum()
    daily.name = sid
    return daily


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
}


def inverse_vol_weights(returns_dict: Dict[str, pd.Series]) -> Dict[str, float]:
    """
    Compute inverse-volatility weights (annualised daily vol) so each strategy
    contributes approximately equal risk to the portfolio.
    """
    vols = {}
    for sid, ret in returns_dict.items():
        r = ret.dropna()
        r = r[r != 0.0]          # exclude flat (no-trade) days
        if len(r) < 5:
            vols[sid] = np.nan
        else:
            vols[sid] = float(r.std() * np.sqrt(252))

    inv_vols = {sid: (1.0 / v) if (v and not np.isnan(v) and v > 0) else 0.0
                for sid, v in vols.items()}
    # Apply manual conviction multipliers (default 1.0) before normalising.
    inv_vols = {sid: iv * CONVICTION.get(sid, 1.0) for sid, iv in inv_vols.items()}
    total = sum(inv_vols.values())
    if total == 0:
        n = len(inv_vols)
        return {sid: 1.0 / n for sid in inv_vols}

    weights = {sid: v / total for sid, v in inv_vols.items()}
    return weights, vols


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
    corr_flags: List[Tuple],
    initial_equity: float = INITIAL_EQUITY,
) -> None:
    flagged = {a for a, b, c, w in corr_flags} | {b for a, b, c, w in corr_flags}
    fragile = {r["id"] for r in strategies if (r.get("torture_flags") or "[]") not in ("[]", "", None)
               and json.loads(r.get("torture_flags") or "[]")}

    print(f"\n{'─'*80}")
    print(f"{'Strategy':<38} {'Status':<20} {'WF':>6} {'Vol%':>7} {'Wt%':>6} {'$Alloc':>9} {'Flags'}")
    print(f"{'─'*80}")

    for r in strategies:
        sid    = r["id"]
        status = r["status"]
        wf     = r["walk_forward_gt_score"] or 0.0
        wt     = weights.get(sid, 0.0)
        vol    = vols.get(sid, np.nan)
        alloc  = wt * initial_equity

        vol_str   = f"{vol*100:.1f}" if (vol and not np.isnan(vol)) else "N/A"
        flags_str = ""
        if sid in fragile:
            flags_str += " [fragile]"
        if sid in flagged:
            flags_str += " [corr!]"

        print(f"{_short(sid, 38):<38} {status:<20} {wf:>6.3f} {vol_str:>7} {wt*100:>5.1f}% "
              f"${alloc:>8,.0f}{flags_str}")

    print(f"{'─'*80}")
    total_alloc = sum(weights.get(r["id"], 0) * initial_equity for r in strategies)
    print(f"{'TOTAL':<38} {'':<20} {'':>6} {'':>7} {'100.0%':>6} ${total_alloc:>8,.0f}")


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
    print(f"\nReconstructing historical returns ({args.start} → {args.end})...")
    returns_dict: Dict[str, pd.Series] = {}

    for r in strategies:
        sid = r["id"]
        sys.stdout.write(f"  {_short(sid, 48):<50} … ")
        sys.stdout.flush()

        ret = build_strategy_returns(r, args.start, args.end)

        if ret is None and args.use_logs:
            ret = parse_log_returns(sid)

        if ret is not None and len(ret) >= MIN_BARS:
            returns_dict[sid] = ret
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
    weights, vols = inverse_vol_weights(returns_dict)

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

    # 6. Portfolio stats
    combined_daily = sum(
        returns_dict[sid] * weights.get(sid, 0)
        for sid in returns_dict
    )
    combined_daily = combined_daily.fillna(0)
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
    print_allocation_table(active_strategies, weights, vols, corr_flags, args.equity)

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
            "n_strategies": len(weights),
            "weights": weights,
            "correlated_pairs": [
                {"a": a, "b": b, "corr": round(float(c), 4), "weaker": weaker}
                for a, b, c, weaker in corr_flags
            ],
        }
        with open(PORTFOLIO_STATE_FILE, "w") as fh:
            json.dump(state, fh, indent=2)
        print(f"✓ Wrote {PORTFOLIO_STATE_FILE}")
        print(f"  live_test.py will use these weights on next signal flip.\n")


if __name__ == "__main__":
    main()
