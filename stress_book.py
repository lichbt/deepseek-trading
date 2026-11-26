#!/usr/bin/env python3
"""
stress_book.py — whole-book correlated-drawdown stress test.

Per-sleeve stats (Sharpe, maxDD) miss the risk that many sleeves open the SAME
direction at once, so their individually-small risks stack on a shock day. This
reconstructs every deployed daily sleeve at its live weight and reports, over the
full backtest history:

  - max sleeves aligned same-direction (long / short) on any one day
  - worst single book-DAY loss (as a fraction of equity), and its alignment
  - worst book-day CONDITIONAL on heavy long/short alignment (>=5/8/10 sleeves)
  - days breaching the prop-firm 3% DAILY threshold
  - Kelly-scaled figures and max drawdown

Run after every deploy as a one-line gate:
    ./venv/bin/python stress_book.py

THE PROP RULE IS 3% DAILY, NOT 5%. This file checked -0.05 until 2026-07-31 and
therefore printed PASS on books the real-sized path would flag. One daily breach
is an instant DQ, so the daily figure is the binding constraint.

KELLY IS MODELLED, NOT FUDGED. Until 2026-07-31 the "real-sized" line was a flat
x1.5 guess (STRESS_REAL_MULT) that did not even cover the Kelly overlay: measured
that day, 20 of 24 tradeable sleeves were running at 2.0x. Each sleeve is now
scaled by a rolling Kelly computed from its OWN PAST returns via kelly_policy —
the same policy object both live books use — so this tool can no longer understate
the book by ignoring its largest sizing lever.

STILL NOT THE SIZING AUTHORITY. This reconstructs weight x net-return, not real
risk-budgeted position sizing; oanda_book_simulator.py (real _compute_position_size,
ATR stops, min-lot clamps) remains the only trustworthy return curve, and it runs a
different window. Use this for the ALIGNMENT question it was written for — how many
sleeves point the same way on one day — and defer worst-day margin to the simulator.

CAVEATS (printed): figures are IN-SAMPLE (sleeves were fit on this data — live
tails run worse), close-to-close daily (understates intraday floating lows the
limit is often measured on), and no real crisis day exists in the sample at
the current composition. Treat "never breached 3%" as a floor, not a guarantee.

Weights: live own-weights from portfolio_state.json when present (what the book
actually trades); else inverse-vol from portfolio.py. Intraday (non-D) sleeves
are skipped — the daily book stress is the concern here.
"""
import os, sys, json, sqlite3, warnings
from datetime import datetime, timedelta

warnings.filterwarnings("ignore")
import numpy as np
import pandas as pd

sys.path.insert(0, os.path.dirname(__file__))
import portfolio as P
import kelly_policy
from validator import create_strategy_function, get_candles_date_range, get_dev_window
from supplementary_data import inject_supplementary_data
import pipeline_utils

# The5ers: 3% DAILY drawdown, 10% total. The daily wall is the binding one — a
# single breach is an instant DQ, whereas total DD has always had headroom.
DAILY_LIMIT = float(os.getenv("PROP_DAILY_LIMIT", "0.03"))
TOTAL_LIMIT = float(os.getenv("PROP_TOTAL_LIMIT", "0.10"))

# Residual sizing NOT captured by weights x Kelly: min-lot clips, equity drift,
# MAXRISK clamps. Deliberately 1.0 — it is NOT a stand-in for Kelly (which is now
# modelled explicitly below) and must not become one again. If you find yourself
# wanting to raise it, run oanda_book_simulator.py instead; that is the tool that
# actually models position sizing.
EXTRA_SIZING_MULT = float(os.getenv("STRESS_EXTRA_MULT", "1.0"))
STATE_FILE = os.path.join(os.path.dirname(__file__), "portfolio_state.json")
# Macro column tokens that mean a sleeve needs supplementary injection to run.
_MACRO_TOKENS = ("dxy", "us_real_yield", "fed_rate", "us10y", "us_cpi", "jp10y",
                 "eu10y", "uk10y", "au10y", "jp_cpi", "uk_cpi", "eu_cpi", "ecb_rate",
                 "boe_rate")


def _weights():
    """Live own-weights from portfolio_state.json, else inverse-vol fallback."""
    if os.path.exists(STATE_FILE):
        try:
            return json.load(open(STATE_FILE))["weights"], "portfolio_state.json (live)"
        except Exception:
            pass
    return None, "inverse-vol (no state file)"


def _intraday_mae(df, sig, stop_mult):
    """Per-bar worst-case ADVERSE intraday move for the position held into each
    bar, from the prior close to the bar's High/Low — the loss the prop DAILY
    limit is measured on, which close-to-close misses. Stop-capped at
    stop_mult*ATR (a single position can't bleed past its stop intraday)."""
    c = df["close"].values; hi = df["high"].values; lo = df["low"].values
    tr = pd.concat([(df["high"] - df["low"]),
                    (df["high"] - df["close"].shift(1)).abs(),
                    (df["low"] - df["close"].shift(1)).abs()], axis=1).max(axis=1)
    atr = tr.rolling(14).mean().values
    n = len(df); mae = np.zeros(n)
    for t in range(1, n):
        pos = sig[t - 1]
        if pos == 0:
            continue
        pc = c[t - 1]
        adv = (lo[t] - pc) / pc if pos > 0 else (pc - hi[t]) / pc   # neg = loss
        cap = -(stop_mult * atr[t - 1]) / pc if (atr[t - 1] == atr[t - 1] and atr[t - 1] > 0) else adv
        mae[t] = max(adv, cap)
    return pd.Series(mae, index=df["date"].iloc[:n])


def _rolling_kelly_series(returns):
    """Per-bar Kelly multiplier for one sleeve, from its own PAST position returns.

    Mirrors what both live books do at runtime (kelly_policy, recomputed every
    RECOMPUTE_EVERY bars) rather than applying today's multiplier to all history.

    LOOK-AHEAD: bar i is sized from returns[:i] — STRICTLY prior bars. Using
    returns[:i+1] would let a sleeve's own outcome on day i set its size for day i,
    which inflates every figure this tool exists to bound.
    """
    r = np.asarray(returns, dtype=float)
    out = np.empty(len(r))
    cur = kelly_policy.FLOOR          # no history yet -> floor, never boost
    every = max(1, kelly_policy.RECOMPUTE_EVERY)
    for i in range(len(r)):
        if i % every == 0:
            cur = kelly_policy.kelly_multiplier(r[:i])
        out[i] = cur
    return pd.Series(out, index=getattr(returns, "index", None))


def reconstruct():
    """Return (signed df, weighted close-to-close df, weighted intraday-MAE df,
    source, skipped) for deployed daily sleeves."""
    rows = {r["id"]: r for r in P.load_strategies()}
    weights, src = _weights()
    now = (datetime.now() - timedelta(days=3)).strftime("%Y-%m-%d")
    SIG, MAE = {}, {}
    skipped = []
    raw_rets, raw_mae = {}, {}
    for sid, row in rows.items():
        tf = row["timeframe"] or "D"
        if tf != "D":
            skipped.append((sid, f"intraday {tf}"))
            continue
        inst = P._infer_instrument(sid)
        try:
            ds, _ = get_dev_window(inst)
            df = get_candles_date_range(inst, ds, now, granularity="D").reset_index(drop=True)
            df["date"] = pd.to_datetime(df["date"])
            archetype = P._infer_archetype(row["code"],
                                           row.get("archetype") or "standard")
            if archetype != "standard":
                df = inject_supplementary_data(
                    df, archetype, inst, row.get("instrument2"),
                    ds, now, "D")
            f = create_strategy_function(row["code"])
            bp = json.loads(row["best_params"] or "{}")
            sig = np.asarray(f(df, bp)).astype(int)
            rr = np.asarray(pipeline_utils.compute_net_strategy_returns(
                df, pd.Series(sig, index=df.index), inst, "D"))
            idx = df["date"].iloc[:len(rr)]
            SIG[sid] = pd.Series(sig[:len(rr)], index=idx)
            raw_rets[sid] = pd.Series(rr, index=idx)
            raw_mae[sid] = _intraday_mae(df, sig, bp.get("stop_mult", 2.0)).iloc[:len(rr)]
        except Exception as e:
            skipped.append((sid, str(e)[:40]))
    if weights is None:
        weights = P.inverse_vol_weights(raw_rets)
    # Kelly scales the position, so it scales BOTH the realised return and the
    # intraday adverse excursion. Applying it to only one would understate the
    # very tail this tool is measuring.
    KEL = {sid: _rolling_kelly_series(r) for sid, r in raw_rets.items()}
    RET, MAE = {}, {}
    for sid, r in raw_rets.items():
        w = weights.get(sid, 0.0) * EXTRA_SIZING_MULT
        k = KEL[sid]
        RET[sid] = r * w * k
        MAE[sid] = raw_mae[sid] * w * k.reindex(raw_mae[sid].index).fillna(kelly_policy.FLOOR)
    return (pd.DataFrame(SIG).fillna(0), pd.DataFrame(RET).fillna(0),
            pd.DataFrame(MAE).fillna(0), src, skipped, pd.DataFrame(KEL))


def report():
    sg, rt, mae_df, src, skipped, kel = reconstruct()
    if rt.empty:
        print("stress_book: no daily sleeves reconstructed")
        return
    book = rt.sum(axis=1)                 # close-to-close book return (frac equity)
    intraday = mae_df.sum(axis=1)         # worst-case intraday adverse (all lows same day)
    nlong = (sg > 0).sum(axis=1)
    nshort = (sg < 0).sum(axis=1)
    eq = (1 + book).cumprod()
    maxdd = (eq / eq.cummax() - 1).min()
    dl, tl = DAILY_LIMIT, TOTAL_LIMIT

    print("=" * 64)
    print(f"BOOK STRESS TEST — {len(sg.columns)} daily sleeves | weights: {src}")
    kelly_state = (f"MODELLED per-bar from each sleeve's own past returns "
                   f"({kelly_policy.UP}x / {kelly_policy.FLOOR}x)"
                   if kelly_policy.ENABLED else
                   "DISABLED (kelly_policy.ENABLED=False) — every bar at 1.0x")
    print(f"Kelly: {kelly_state}"
          + (f" | extra sizing x{EXTRA_SIZING_MULT}" if EXTRA_SIZING_MULT != 1.0 else ""))
    print("=" * 64)
    print(f"max sleeves aligned:  {int(nlong.max())} LONG  /  {int(nshort.max())} SHORT (same day)")
    wd = book.idxmin()
    print(f"worst DAY close-close: {book.min()*100:+.2f}%  on {wd.date()} "
          f"({int(nlong[wd])}L/{int(nshort[wd])}S)")
    wi = intraday.idxmin()
    print(f"worst DAY INTRADAY:    {intraday.min()*100:+.2f}%  on {wi.date()}  "
          f"<- the number the {dl*100:.0f}% DAILY limit is measured on")
    print(f"max book drawdown:    {maxdd*100:+.2f}%")
    print(f"days intraday < -2%: {int((intraday < -0.02).sum())}   "
          f"< -{dl*100:.0f}% (LIMIT): {int((intraday < -dl).sum())}")
    if kel.size and kelly_policy.ENABLED:
        share_up = float((kel == kelly_policy.UP).to_numpy().mean())
        print(f"bar-share at {kelly_policy.UP}x Kelly: {share_up*100:.0f}%")
    print("-" * 64)
    print("worst intraday-day CONDITIONAL on alignment:")
    for thr in (5, 8, 10):
        for label, n in (("long", nlong), ("short", nshort)):
            m = n >= thr
            if m.sum():
                print(f"  >={thr:2} {label:5}: {int(m.sum()):4} days | worst intraday {intraday[m].min()*100:+.2f}%")
    print("-" * 64)
    daily_margin = (dl + intraday.min()) * 100      # +ve = headroom to the wall
    print(f"worst intraday {intraday.min()*100:+.2f}%  vs -{dl*100:.0f}% wall  "
          f"→ margin {daily_margin:+.2f} pp")
    print(f"maxDD {maxdd*100:+.2f}%  vs -{tl*100:.0f}% wall  "
          f"→ margin {(tl + maxdd)*100:+.2f} pp")
    daily_ok = intraday.min() > -dl
    total_ok = maxdd > -tl
    print(f"prop-firm daily {dl*100:.0f}% | static {tl*100:.0f}%  →  "
          f"{'PASS' if daily_ok and total_ok else 'REVIEW'}")
    # A worst-day margin resting on one or two observations is a single-day
    # artifact, not headroom — warn only when the margin is actually thin AND
    # backed by too few near-wall events to mean anything.
    n_near = int((intraday < -dl / 2).sum())
    if daily_ok and daily_margin < 1.0 and n_near <= 2:
        print(f"  ^ margin is {daily_margin:+.2f} pp and only {n_near} day(s) fall below "
              f"half the wall — a single-day artifact, not headroom.")
    print(f"  authority for worst-day margin is oanda_book_simulator.py "
          f"+ scripts/prop_realsim_mc.py, not this file.")
    if skipped:
        print("-" * 64)
        print(f"skipped {len(skipped)}: " + ", ".join(f"{s.split('_auto_')[0]}({why})" for s, why in skipped[:8])
              + (" ..." if len(skipped) > 8 else ""))
    print("=" * 64)
    print("CAVEATS: in-sample (live tails worse) · intraday is STOP-CAPPED (a "
          "gap through the stops fills worse) · daily bars miss sub-bar spikes · "
          "no crisis day in sample. NOT the sizing authority — this is weight x "
          "net-return x Kelly, not real position sizing; use oanda_book_simulator.py "
          f"for that. 'never breached {dl*100:.0f}%' = floor, not guarantee.")


if __name__ == "__main__":
    report()
