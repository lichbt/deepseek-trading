#!/usr/bin/env python3
"""Does a conviction trim predict what it claims to predict?

Most CONVICTION trims in portfolio.py fire on a trailing 6-month Sharpe
("REVIEW 2026-07-11: 6mo Sharpe -2.61 (12mo +1.01 = temporary)"). That is a
forecast: it asserts recent weakness will continue. This tests it directly, by
asking what a sleeve's trailing 6mo Sharpe says about its NEXT 3 months.

The answer is that it is ANTI-predictive on this book, so the trims cut sleeves
immediately before they recover. This is consistent with the standing record that
a single decay verdict is too noisy to decide retire-or-keep.

CAVEAT, and it is not small: the windows overlap (126-bar lookback, 63-bar
forward, sampled every 5 bars), so the effective sample size is far below the
raw observation count and the p-values are optimistic. Trust the monotonic
ordering across quartiles, not the exact significance.
"""
import sys
from pathlib import Path

import numpy as np
import pandas as pd
from scipy import stats as st

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent)); sys.path.insert(0, str(HERE))

import risk_alloc_lab as L
import risk_model_sim as RS

LOOKBACK, FORWARD, STRIDE = 126, 63, 5


def main():
    R, _ = L.sleeve_returns(Path(RS.DEFAULT_SLEEVES).read_bytes())
    rows = []
    for sid in R.columns:
        r = R[sid]
        for t in range(LOOKBACK, len(r) - FORWARD, STRIDE):
            p = r.iloc[t - LOOKBACK:t]; f = r.iloc[t:t + FORWARD]
            p, f = p[p != 0], f[f != 0]
            if len(p) < 20 or len(f) < 10 or not p.std() or not f.std():
                continue
            rows.append((sid,
                         p.mean() / p.std() * np.sqrt(252),
                         f.mean() / f.std() * np.sqrt(252)))
    d = pd.DataFrame(rows, columns=["sid", "past6mo", "fwd3mo"]).replace(
        [np.inf, -np.inf], np.nan).dropna()

    pr = st.pearsonr(d.past6mo, d.fwd3mo); sr = st.spearmanr(d.past6mo, d.fwd3mo)
    print("panel %d obs over %d sleeves (OVERLAPPING — see docstring)"
          % (len(d), d.sid.nunique()))
    print("corr(trailing 6mo Sharpe, forward 3mo Sharpe): pearson %+.3f (p=%.3f)"
          "  spearman %+.3f (p=%.3f)" % (pr[0], pr[1], sr[0], sr[1]))
    print("\nforward 3mo Sharpe by trailing-6mo quartile:")
    q = pd.qcut(d.past6mo, 4, labels=["Q1 worst", "Q2", "Q3", "Q4 best"])
    for name, g in d.groupby(q, observed=True):
        print("  %-9s n=%4d  trailing %+6.2f  ->  forward %+6.2f"
              % (name, len(g), g.past6mo.mean(), g.fwd3mo.mean()))
    lo, hi = d[d.past6mo < -1.0], d[d.past6mo >= -1.0]
    t = st.ttest_ind(lo.fwd3mo, hi.fwd3mo, equal_var=False)
    print("\ntrailing 6mo Sharpe < -1.0 (the trim trigger): n=%d forward %+.3f"
          "  vs others %+.3f  diff %+.3f (t=%+.2f, p=%.3f)"
          % (len(lo), lo.fwd3mo.mean(), hi.fwd3mo.mean(),
             lo.fwd3mo.mean() - hi.fwd3mo.mean(), t[0], t[1]))


if __name__ == "__main__":
    main()
