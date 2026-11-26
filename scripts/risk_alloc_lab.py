#!/usr/bin/env python3
"""How much room is there in the ALLOCATION, before any scalar is touched?

The book's constraint is a QUANTILE (3% on one day), not a variance, and its worst
day is a co-movement event. Inverse-vol weighting uses only the diagonal of the
covariance matrix, so it cannot see co-movement at all; the one correlation control
in the runtime (the corr_scale peer haircut) is independently measured inert. So the
live book has no correlation-aware risk control anywhere.

This asks whether that costs anything, by the only comparison that matters:
NORMALISE EVERY WEIGHTING TO THE SAME TAIL, then compare mean return. Equal tail =
equal distance to the wall = equal DQ risk, so whichever earns more at that tail
reaches the target sooner. A weighting that earns 10% more at the same tail is worth
about 10% fewer days.

THE PROXY IS LINEAR AND THE REAL BOOK IS NOT. Per-sleeve returns are collected at
weight_scale = 1 and recombined as sum(w_i * r_i), which ignores the cTrader min-lot
step function and fix_runner's skip rule. That is fine for RANKING allocations and
wrong for quoting a figure, so any winner here must be re-run through
risk_model_sim.run (the full nonlinear path) before it means anything.
"""
import argparse
import pickle
import sys
from pathlib import Path

import numpy as np
import pandas as pd

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))
sys.path.insert(0, str(HERE))

import risk_model_sim as RS

TAIL_Q = 0.01          # CVaR level
TARGET_TAIL = None     # set from the current book, so comparisons are like-for-like


def sleeve_returns(blob, base_risk=0.005):
    """(DataFrame of per-sleeve daily returns at weight_scale=1, current ws vector)."""
    sleeves = pickle.loads(blob)
    current_ws = {s.sid: s.weight_scale for s in sleeves}
    unit = {s.sid: 1.0 for s in sleeves}
    cfg = RS.config_from(base_risk, 0.02, 0.80, components=())
    df, _ = RS.run(cfg, blob, guard=False, weight_scale_override=unit,
                   record_sleeve_pnl=True)
    cols = [c for c in df.columns if c.startswith("r__")]
    R = df[cols].copy()
    R.columns = [c[3:] for c in cols]
    return R, pd.Series(current_ws).reindex(R.columns).fillna(0.0)


def stats(r):
    r = np.asarray(r, float)
    k = max(1, int(np.ceil(TAIL_Q * len(r))))
    worst = np.sort(r)[:k]
    return {
        "mean": r.mean(),
        "std": r.std(),
        "worst": r.min(),
        "cvar": worst.mean(),
        "sharpe": r.mean() / r.std() * np.sqrt(252) if r.std() else np.nan,
    }


def shrunk_cov(R, lam=0.30):
    """Ledoit-Wolf-ish: pull the correlation matrix toward its average.

    Sample covariance on 23 sleeves and 674 days is estimable but the OFF-DIAGONAL
    is what the optimisers key on and it is the noisy part, so shrink it rather than
    trust it. lam=0.30 is deliberately heavy — an optimiser that only wins at lam=0
    is fitting noise, and this file exists to find out which.
    """
    S = np.cov(R.values, rowvar=False)
    d = np.sqrt(np.diag(S))
    C = S / np.outer(d, d)
    n = len(C)
    off = (C.sum() - n) / (n * (n - 1))
    Ctgt = np.full_like(C, off)
    np.fill_diagonal(Ctgt, 1.0)
    Cs = (1 - lam) * C + lam * Ctgt
    return Cs * np.outer(d, d)


def w_equal(R, **kw):
    return pd.Series(1.0, index=R.columns)


def w_invvol(R, **kw):
    v = R.std()
    w = 1.0 / v.replace(0, np.nan)
    return w.fillna(0.0)


def w_minvar(R, lam=0.30, **kw):
    S = shrunk_cov(R, lam)
    inv = np.linalg.pinv(S)
    w = inv @ np.ones(len(S))
    return pd.Series(np.clip(w, 0, None), index=R.columns)


def w_erc(R, lam=0.30, iters=3000, **kw):
    """Equal risk contribution by fixed-point iteration — no solver dependency."""
    S = shrunk_cov(R, lam)
    n = len(S)
    w = np.ones(n) / n
    for _ in range(iters):
        mrc = S @ w
        rc = w * mrc
        target = rc.mean()
        w = w * (target / np.maximum(rc, 1e-18)) ** 0.5
        w = np.clip(w, 1e-9, None)
        w /= w.sum()
    return pd.Series(w, index=R.columns)


def w_mincvar(R, iters=4000, seed=7, **kw):
    """Minimise CVaR directly, by projected coordinate search.

    CVaR is the quantity the 3% wall actually constrains, and unlike variance it is
    not a smooth function of the weights, so this is a search rather than a solve.
    Started from inverse-vol so it can only improve on the incumbent family.
    """
    rng = np.random.default_rng(seed)
    X = R.values
    w = (w_invvol(R) / w_invvol(R).sum()).values
    def obj(v):
        r = X @ v
        k = max(1, int(np.ceil(TAIL_Q * len(r))))
        tail = -np.sort(r)[:k].mean()
        return tail / max(r.mean(), 1e-12)      # tail per unit of return
    best = obj(w)
    for t in range(iters):
        step = 0.25 * (1 - t / iters) + 0.02
        i = rng.integers(0, len(w))
        cand = w.copy()
        cand[i] = max(0.0, cand[i] * (1 + step * rng.normal()))
        if cand.sum() <= 0:
            continue
        cand /= cand.sum()
        v = obj(cand)
        if v < best:
            best, w = v, cand
    return pd.Series(w, index=R.columns)


BUILDERS = [
    ("current (inv-vol x conviction, capped)", None),
    ("equal weight", w_equal),
    ("inverse vol (diagonal only)", w_invvol),
    ("min variance (shrunk)", w_minvar),
    ("ERC / risk parity (shrunk)", w_erc),
    ("min CVaR (tail-optimised)", w_mincvar),
]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--sleeves", default=str(RS.DEFAULT_SLEEVES))
    ap.add_argument("--lam", type=float, default=0.30)
    ap.add_argument("--out", default="/tmp/alloc_weights.csv")
    args = ap.parse_args()

    blob = Path(args.sleeves).read_bytes()
    print("collecting per-sleeve returns at weight_scale=1 ...", flush=True)
    R, current = sleeve_returns(blob)
    print("  %d bars x %d sleeves" % R.shape)

    corr = R.corr().values
    off = corr[np.triu_indices(len(corr), 1)]
    print("  pairwise corr: mean %+.3f  p95 %+.3f  max %+.3f"
          % (off.mean(), np.percentile(off, 95), off.max()))

    base = R.values @ current.values
    base_s = stats(base)
    global TARGET_TAIL
    TARGET_TAIL = abs(base_s["cvar"])
    print("  current book: mean %+.4f%%  worst %+.3f%%  CVaR1%% %+.3f%%"
          % (100 * base_s["mean"], 100 * base_s["worst"], 100 * base_s["cvar"]))

    print("\nEvery weighting RESCALED to the current book's CVaR — so the mean column")
    print("is a like-for-like comparison at equal distance to the wall.\n")
    print("%-40s %9s %9s %9s %8s %9s" % ("allocation", "mean%", "worst%",
                                         "CVaR1%", "Sharpe", "vs now"))
    rows = {}
    for name, fn in BUILDERS:
        w = current.copy() if fn is None else fn(R, lam=args.lam)
        w = w.reindex(R.columns).fillna(0.0)
        r = R.values @ w.values
        s = stats(r)
        if s["cvar"] == 0:
            continue
        k = TARGET_TAIL / abs(s["cvar"])          # rescale to equal tail
        w_scaled = w * k
        r2 = r * k
        s2 = stats(r2)
        rows[name] = w_scaled
        print("%-40s %9.4f %9.3f %9.3f %8.2f %+8.1f%%"
              % (name, 100 * s2["mean"], 100 * s2["worst"], 100 * s2["cvar"],
                 s2["sharpe"], 100 * (s2["mean"] / base_s["mean"] - 1)))

    pd.DataFrame(rows).to_csv(args.out)
    print("\nwrote %s" % args.out)
    print("\nPROXY IS LINEAR: min-lot clamping and fix_runner's skip rule are not")
    print("modelled here. Rank on this, then re-run the winner through")
    print("risk_model_sim.run before quoting anything.")


if __name__ == "__main__":
    main()
