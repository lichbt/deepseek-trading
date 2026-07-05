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
  - days breaching the prop-firm 3% / 5% daily thresholds
  - approx real-sized figures (x REAL_SIZED_MULT) and max drawdown

Run after every deploy as a one-line gate:
    ./venv/bin/python stress_book.py

CAVEATS (printed): figures are IN-SAMPLE (sleeves were fit on this data — live
tails run worse), close-to-close daily (understates intraday floating lows the
5% limit is often measured on), and no real crisis day exists in the sample at
the current composition. Treat "never breached 5%" as a floor, not a guarantee.

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
from validator import create_strategy_function, get_candles_date_range, get_dev_window
from supplementary_data import inject_supplementary_data
import pipeline_utils

# Real live sizing (0.5%-risk model with min-units clips) runs ~1.5x the
# risk-normalized weighted-return DD — measured 2026-06/07 (risk-norm maxDD
# -3.15% vs real-sized -4.4/-5.4%). Env-overridable as the estimate is refined.
REAL_SIZED_MULT = float(os.getenv("STRESS_REAL_MULT", "1.5"))
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


def reconstruct():
    """Return (signed-position df, weighted-return df) for deployed daily sleeves."""
    rows = {r["id"]: r for r in P.load_strategies()}
    weights, src = _weights()
    now = (datetime.now() - timedelta(days=3)).strftime("%Y-%m-%d")
    SIG, RET = {}, {}
    skipped = []
    # If no live weights, compute inverse-vol from the reconstructed returns after.
    raw_rets = {}
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
            if any(k in row["code"] for k in _MACRO_TOKENS):
                df = inject_supplementary_data(df, "macro", inst, None, ds, now, "D")
            f = create_strategy_function(row["code"])
            bp = json.loads(row["best_params"] or "{}")
            sig = np.asarray(f(df, bp)).astype(int)
            rr = np.asarray(pipeline_utils.compute_net_strategy_returns(
                df, pd.Series(sig, index=df.index), inst, "D"))
            idx = df["date"].iloc[:len(rr)]
            SIG[sid] = pd.Series(sig[:len(rr)], index=idx)
            raw_rets[sid] = pd.Series(rr, index=idx)
        except Exception as e:
            skipped.append((sid, str(e)[:40]))
    if weights is None:
        weights = P.inverse_vol_weights(raw_rets)
    for sid, r in raw_rets.items():
        RET[sid] = r * weights.get(sid, 0.0)
    return pd.DataFrame(SIG).fillna(0), pd.DataFrame(RET).fillna(0), src, skipped


def report():
    sg, rt, src, skipped = reconstruct()
    if rt.empty:
        print("stress_book: no daily sleeves reconstructed")
        return
    book = rt.sum(axis=1)                 # daily book return, fraction of equity
    nlong = (sg > 0).sum(axis=1)
    nshort = (sg < 0).sum(axis=1)
    eq = (1 + book).cumprod()
    maxdd = (eq / eq.cummax() - 1).min()

    print("=" * 64)
    print(f"BOOK STRESS TEST — {len(sg.columns)} daily sleeves | weights: {src}")
    print("=" * 64)
    print(f"max sleeves aligned:  {int(nlong.max())} LONG  /  {int(nshort.max())} SHORT (same day)")
    wd = book.idxmin()
    print(f"worst book DAY:       {book.min()*100:+.2f}%  on {wd.date()} "
          f"({int(nlong[wd])}L/{int(nshort[wd])}S that day)")
    print(f"max book drawdown:    {maxdd*100:+.2f}%")
    print(f"days < -3%: {int((book < -0.03).sum())}   days < -5% (LIMIT): {int((book < -0.05).sum())}")
    print("-" * 64)
    print("worst book-day CONDITIONAL on alignment:")
    for thr in (5, 8, 10):
        for label, n in (("long", nlong), ("short", nshort)):
            m = n >= thr
            if m.sum():
                print(f"  >={thr:2} {label:5}: {int(m.sum()):4} days | worst {book[m].min()*100:+.2f}%")
    print("-" * 64)
    rmult = REAL_SIZED_MULT
    print(f"approx REAL-sized (x{rmult}):  worst day {book.min()*rmult*100:+.2f}%  "
          f"maxDD {maxdd*rmult*100:+.2f}%  days<-5%: {int(((book*rmult) < -0.05).sum())}")
    print(f"prop-firm daily limit 5% | static 10%  →  "
          f"{'PASS' if (book.min()*rmult) > -0.05 and maxdd*rmult > -0.10 else 'REVIEW'}")
    if skipped:
        print("-" * 64)
        print(f"skipped {len(skipped)}: " + ", ".join(f"{s.split('_auto_')[0]}({why})" for s, why in skipped[:8])
              + (" ..." if len(skipped) > 8 else ""))
    print("=" * 64)
    print("CAVEATS: in-sample (live tails worse) · close-to-close (understates "
          "intraday) · no crisis day in sample. 'never breached 5%' = floor, not guarantee.")


if __name__ == "__main__":
    report()
