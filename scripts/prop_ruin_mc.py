#!/usr/bin/env python3
"""Risk of ruin measured the way the firm measures it — on the INTRADAY low.

WHY THIS EXISTS. Every other Monte Carlo in this repo (prop_realsim_mc,
prop_twostep_mc, prop_guarded_mc) bootstraps CLOSE-TO-CLOSE daily returns and
tests the prop limits against them. The firm does not. It breaches "if your
equity drops below your calculated daily threshold AT ANY POINT during the day",
which means a day that dips to -3.2% and closes at -1.1% is a DEAD ACCOUNT that
every close-to-close model scores as a mild loss.

So this resamples the PAIR (close_return, intraday_return) per day, keeping them
together so their relationship survives the bootstrap, and tests:

  daily breach   intraday_return <= -daily_limit          (floating low, not close)
  total breach   equity path low <= step_start * (1-total_limit)
  guard          intraday_return <= -daily_limit*fraction -> day ends at the halt
                 level plus slippage, UNLESS the guard misses (p_miss)

THE INTRADAY SERIES IS A BOUND, NOT A PATH. risk_model_sim emits two:
`intraday_low_cotimed` assumes every open sleeve hits its worst tick at the same
moment (conservative, over-reports breaches) and `intraday_low_worst1` assumes
only the single worst sleeve dips (optimistic). The truth is between them, so
this reports BOTH and the honest answer is the range. Quoting either alone is
the same class of error this script exists to correct.

STILL TRUE, AND UNFIXABLE HERE: a block bootstrap cannot produce a day worse than
the worst that occurred. These are conditional on the observed tail being
representative, and the observed tail rests on ONE day (2024-06-06).
"""
import argparse
import sys
from pathlib import Path

import numpy as np
import pandas as pd

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent)); sys.path.insert(0, str(HERE))


def run_paths(close, intra, rng, targets, daily, total, halt_frac, slip,
              p_miss, guard, block, paths, max_days, consistency=0.50):
    """-> dict of outcome shares and day quantiles. Ruin = daily OR total breach."""
    n = len(close)
    halt_level = -abs(daily) * halt_frac
    caught_at = halt_level - slip
    out = {"pass": 0, "ruin_daily": 0, "ruin_total": 0, "timeout": 0}
    ok_days, fail_days = [], []

    for _ in range(paths):
        total_days, alive, failed = 0, True, None
        for target in targets:
            equity, days, best = 1.0, 0, 0.0
            while days < max_days and alive:
                i = rng.integers(0, n - block)
                for j in range(i, i + block):
                    days += 1
                    c, lo = close[j], intra[j]
                    if guard and lo <= halt_level:
                        # Guard sees the floating low cross the line.
                        if p_miss and rng.random() < p_miss:
                            # Missed: the full day lands, and the FLOATING LOW is
                            # what the firm judges — not the close.
                            if lo <= -abs(daily):
                                failed, alive = "ruin_daily", False
                                break
                        else:
                            c = caught_at        # flattened at the halt, day over
                            lo = caught_at
                    elif lo <= -abs(daily):
                        failed, alive = "ruin_daily", False
                        break
                    prev = equity
                    equity_low = equity * (1 + lo)
                    equity = equity * (1 + c)
                    best = max(best, equity - prev)
                    if equity_low - 1 <= -abs(total):    # STATIC floor, on the low
                        failed, alive = "ruin_total", False
                        break
                    if equity - 1 >= max(target, best / consistency):
                        break
                    if days >= max_days:
                        break
                if not alive:
                    break
                if equity - 1 >= max(target, best / consistency):
                    break
                if days >= max_days:
                    break
            total_days += days
            if not alive:
                break
            if equity - 1 < max(target, best / consistency):
                failed, alive = "timeout", False
                break
        if alive:
            out["pass"] += 1
            ok_days.append(total_days)
        else:
            out[failed] += 1
            fail_days.append(total_days)

    pr = out["pass"] / paths
    q = np.percentile(ok_days, [50, 75]) if ok_days else [np.nan, np.nan]
    mdf = float(np.mean(fail_days)) if fail_days else 0.0
    return {
        "pass_pct": 100 * pr,
        "ruin_pct": 100 * (out["ruin_daily"] + out["ruin_total"]) / paths,
        "ruin_daily_pct": 100 * out["ruin_daily"] / paths,
        "ruin_total_pct": 100 * out["ruin_total"] / paths,
        "timeout_pct": 100 * out["timeout"] / paths,
        "median_days": q[0], "p75_days": q[1],
        "expected_days_per_funded": (q[0] / pr + (1 - pr) / pr * mdf) if pr else float("inf"),
    }


def series_from(df, initial=100000.0):
    """(close_return, intraday_return) per bar, both vs the DAY BASE."""
    base = df.day_base.values
    close = df.equity.values / base - 1.0
    cot = df.intraday_low_cotimed.values / base - 1.0
    w1 = df.intraday_low_worst1.values / base - 1.0
    return close, np.minimum(cot, close), np.minimum(w1, close)


def main():
    import risk_model_sim as RS
    import portfolio as P
    import pickle

    ap = argparse.ArgumentParser()
    ap.add_argument("--ws", type=float, nargs="+", default=[1.0, 1.15, 1.30, 1.60])
    ap.add_argument("--paths", type=int, default=20000)
    ap.add_argument("--block", type=int, default=60)
    ap.add_argument("--p-miss", type=float, nargs="+", default=[0.0, 0.25])
    ap.add_argument("--seed", type=int, default=7)
    a = ap.parse_args()

    blob = Path(RS.DEFAULT_SLEEVES).read_bytes()
    sids = [s.sid for s in pickle.loads(blob)]
    n = len(sids)

    def capped(k):
        old = P.CLUSTER_CAP
        try:
            P.CLUSTER_CAP = 3.0
            w = P._apply_cluster_caps({s: 1.0 / n for s in sids})
        finally:
            P.CLUSTER_CAP = old
        return {s: v * n * k for s, v in w.items()}

    cfg = RS.config_from(0.005, 0.02, 0.80, ())
    print("RISK OF RUIN on the INTRADAY low (block=%d, paths=%d, targets 10%%+5%%)"
          % (a.block, a.paths))
    print("cotimed = conservative bound (all sleeves worst-tick together)")
    print("worst1  = optimistic bound (only the worst sleeve dips)\n")
    print("%5s %8s | %-26s | %-26s"
          % ("ws", "measure", "NO GUARD", "GUARD (p_miss shown)"))
    print("%5s %8s | %8s %8s %8s | %8s %8s %8s"
          % ("", "", "ruin%", "median", "E[d/f]", "ruin%", "median", "E[d/f]"))
    for k in a.ws:
        df, s = RS.run(cfg, blob, guard=False, weight_scale_override=capped(k))
        close, cot, w1 = series_from(df)
        for label, lo in (("cotimed", cot), ("worst1", w1)):
            rng = np.random.default_rng(a.seed)
            ng = run_paths(close, lo, rng, (0.10, 0.05), 0.03, 0.10, 0.80,
                           0.004, 0.0, False, a.block, a.paths, 2000)
            row = "%5.2f %8s | %8.2f %8.0f %8.0f" % (
                k, label, ng["ruin_pct"], ng["median_days"],
                ng["expected_days_per_funded"])
            for pm in a.p_miss:
                rng = np.random.default_rng(a.seed)
                g = run_paths(close, lo, rng, (0.10, 0.05), 0.03, 0.10, 0.80,
                              0.004, pm, True, a.block, a.paths, 2000)
                row += " | %8.2f %8.0f %8.0f" % (
                    g["ruin_pct"], g["median_days"], g["expected_days_per_funded"])
            print(row, flush=True)
    print("\np_miss columns: %s" % ", ".join("%.0f%%" % (100 * p) for p in a.p_miss))
    print("A block bootstrap CANNOT produce a day worse than the observed worst,")
    print("and the observed worst rests on ONE day. These are conditional numbers.")


if __name__ == "__main__":
    main()
