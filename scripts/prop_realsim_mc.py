"""Prop-challenge Monte Carlo on REAL-SIZED book daily returns.

Unlike prop_daily_breach_mc.py / prop_pass_curve_mc.py (which reconstruct the
book from portfolio weights and understate real sizing ~8x), this reads the
daily equity curve produced by oanda_book_simulator.py — actual live-like
`_compute_position_size` sizing, Kelly, decay, min-lot clamps — and block-
bootstraps it against The5ers rules (3% daily DD, 10% total DD, +10% target).

Usage:
  python oanda_book_simulator.py --start 2024-01-01 --end <today> \
      --risk 0.005 --max-risk 0.02 --csv /tmp/book.csv
  python scripts/prop_realsim_mc.py /tmp/book.csv
"""
import sys

import numpy as np
import pandas as pd

DAILY = -0.03
TOTAL = -0.10
PROFIT = 0.10
N_SIM = 50000
BLOCKS = [1, 5, 10, 20]
HORIZONS = [60, 120, 252, 504, 756]
SEED = 777

path_csv = sys.argv[1]
df = pd.read_csv(path_csv, index_col=0, parse_dates=True)
col = "equity" if "equity" in df.columns else df.columns[0]
eq = df[col].astype(float).dropna()
arr = eq.pct_change().dropna().values
arr = arr[np.isfinite(arr)]

peak = eq.cummax()
dd = (eq / peak - 1).min()
sharpe = arr.mean() / arr.std() * np.sqrt(252) if arr.std() else float("nan")

print(f"source           {path_csv}")
print(f"days             {len(arr)}  ({eq.index[0].date()} -> {eq.index[-1].date()})")
print(f"total_return_pct {(eq.iloc[-1] / eq.iloc[0] - 1) * 100:+.2f}")
print(f"sharpe           {sharpe:.2f}")
print(f"max_dd_pct       {dd * 100:.2f}")
print(f"worst_day_pct    {arr.min() * 100:.2f}   (margin to -3.00: {(arr.min() - DAILY) * -100:.2f} pp)")
print(f"days_below_-2pct {(arr <= -0.02).sum()}")
print()


def sample_block(rng, n, block):
    if block <= 1:
        return rng.choice(arr, size=n, replace=True)
    chunks = []
    total = 0
    while total < n:
        st = rng.integers(0, max(1, len(arr) - block + 1))
        c = arr[st:st + block]
        chunks.append(c)
        total += len(c)
    return np.concatenate(chunks)[:n]


def classify(p):
    e = 1.0
    peak_ = 1.0
    for i, r in enumerate(p, 1):
        if r <= DAILY:
            return "daily", i, e - 1
        e *= 1 + r
        peak_ = max(peak_, e)
        if e / peak_ - 1 <= TOTAL:
            return "total", i, e - 1
        if e - 1 >= PROFIT:
            return "pass", i, e - 1
    return "timeout", len(p), e - 1


print("horizon block pass_pct daily_breach_pct total_breach_pct timeout_pct "
      "median_pass_day p25 p75 median_end_pct")
for horizon in HORIZONS:
    for block in BLOCKS:
        rng = np.random.default_rng(SEED + horizon * 100 + block)
        outs = [classify(sample_block(rng, horizon, block)) for _ in range(N_SIM)]
        kinds = [o[0] for o in outs]
        pd_days = np.array([o[1] for o in outs if o[0] == "pass"])
        ends = np.array([o[2] for o in outs])
        if len(pd_days):
            med, p25, p75 = (int(np.median(pd_days)), int(np.quantile(pd_days, .25)),
                             int(np.quantile(pd_days, .75)))
        else:
            med = p25 = p75 = "na"
        print(horizon, block,
              round(100 * kinds.count("pass") / N_SIM, 2),
              round(100 * kinds.count("daily") / N_SIM, 2),
              round(100 * kinds.count("total") / N_SIM, 2),
              round(100 * kinds.count("timeout") / N_SIM, 2),
              med, p25, p75, round(100 * np.median(ends), 2))
