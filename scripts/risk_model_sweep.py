#!/usr/bin/env python3
"""Score risk configurations and emit the days-to-funded vs DQ frontier.

THE ONE ARCHITECTURAL RULE: the guard is modelled in exactly ONE layer. Both
risk_model_sim (on the real path) and prop_guarded_mc (on resampled days) can
truncate at the halt line, and doing both double-counts it. So every series fed
to the MC is simulated with guard=off, and the MC applies the guard. The sim's
own guard is used only for the separate re-entry-cost table, which is a
correction term and is NOT part of the frontier.

BLOCK SIZE IS AN AXIS, NOT A SETTING. This book's returns are regime-clustered:
its first 300 bars produced +3.10% and bars 300-400 alone produced +10.33%, while
daily autocorrelation is ~0.00. That is nonstationarity of the MEAN, not serial
correlation, and a block bootstrap at block=10 cannot reproduce it — it hands
back the average regime every day and therefore reports a book that earns
steadily. Sweeping block=10 against block=60 measures how much of the "sizing up
buys days" conclusion is an artifact of that assumption.

DELIBERATE REDUCTIONS, stated rather than hidden:
  - paths=5000, not prop_guarded_mc's 20000 default. At 5000 a 1% DQ rate has a
    standard error near 0.14pp, which is ample for ranking but should not be
    quoted to two decimals.
  - The MC mode grid is restricted per phase (see PHASES below) rather than run
    fully crossed, because run_step is a per-day Python loop.
  - CLUSTER_CAP sweeps DOWNWARD only. The cap does not renormalise, so a bound
    cluster's pre-cap weight is destroyed and cannot be recovered; going up
    requires portfolio.py main(), which WRITES the live portfolio_state.json that
    both trading books read. Logged as a dropped axis.
"""
import argparse
import itertools
import pickle
import sys
import time
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pandas as pd

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))
sys.path.insert(0, str(HERE))

import portfolio as P
import prop_guarded_mc as G
import prop_risk_model as M
import risk_model_sim as RS

PATHS = 5000
SEED = 7
MC_MAX_DAYS = 2000

PHASE_A_RISKS = [0.005, 0.006, 0.007, 0.0075, 0.008, 0.0085, 0.009, 0.0095,
                 0.010, 0.011, 0.012]
PHASE_B_RISKS = [0.0075, 0.010]
PHASE_B_SETS = [(), ("throttle",), ("budget_gate",), ("ramp",), ("endgame",),
                ("consistency",), ("throttle", "ramp")]
PHASE_C = [(cap, r) for cap in (1.0, 1.5) for r in (0.0075, 0.010)]
GUARD_COST_RISKS = [0.0075, 0.010, 0.012]


# ---------------------------------------------------------------------------
# cluster cap, in memory, never on disk
# ---------------------------------------------------------------------------

def recapped_weight_scales(sleeves, cap):
    """Sleeve weight_scales re-derived at a LOWER CLUSTER_CAP.

    Sleeve.weight_scale == min(weight * n, 3.0), so weight == ws / n as long as
    the 3.0 clamp did not bind. Re-applying _apply_cluster_caps to already-capped
    weights is EXACT for a lower cap: a cluster bound at 2 has post-cap sum 2/n,
    and capping that at 1.5 gives 1.5/n — identical to capping the originals.
    """
    n = len(sleeves)
    weights = {s.sid: s.weight_scale / n for s in sleeves}
    old = P.CLUSTER_CAP
    try:
        P.CLUSTER_CAP = cap
        recapped = P._apply_cluster_caps(weights)
    finally:
        P.CLUSTER_CAP = old
    return {sid: min(w * n, 3.0) for sid, w in recapped.items()}


# ---------------------------------------------------------------------------
# MC layer
# ---------------------------------------------------------------------------

def mc(returns, halt_fraction, mode, p_miss, block, daily=-0.03, total=-0.10,
       consistency=0.50, targets=(0.10, 0.05), paths=PATHS):
    """-> dict. Reuses prop_guarded_mc.run_step so the prop rules live in one place."""
    a = SimpleNamespace(
        mode=mode, halt=daily * halt_fraction, slip=0.004, p_miss=p_miss,
        daily=daily, total=total, consistency=consistency, block=block,
        max_days=MC_MAX_DAYS, targets=list(targets), paths=paths, seed=SEED)
    rng = np.random.default_rng(SEED)
    res = {"pass": 0, "daily": 0, "total": 0, "timeout": 0}
    ok_days, fail_days = [], []
    for _ in range(paths):
        tot, alive = 0, True
        for target in a.targets:
            outcome, d = G.run_step(target, returns, rng, a)
            tot += d
            if outcome != "pass":
                res[outcome] += 1
                alive = False
                break
        (ok_days if alive else fail_days).append(tot)
        if alive:
            res["pass"] += 1
    pr = res["pass"] / paths
    q = np.percentile(ok_days, [25, 50, 75]) if ok_days else [np.nan] * 3
    mdf = float(np.mean(fail_days)) if fail_days else 0.0
    # Throughput objective: a blown account is a cheap re-buy, so what matters is
    # expected days PER FUNDED ACCOUNT including the failed attempts along the way.
    exp = (q[1] / pr + (1 - pr) / pr * mdf) if pr > 0 else float("inf")
    return {"pass_pct": 100 * pr, "daily_dq_pct": 100 * res["daily"] / paths,
            "total_dq_pct": 100 * res["total"] / paths,
            "timeout_pct": 100 * res["timeout"] / paths,
            "p25_days": q[0], "median_days": q[1], "p75_days": q[2],
            "mean_days_to_failure": mdf, "expected_days_per_funded": exp}


def returns_of(df, initial=100000.0):
    return (df.pnl / df.equity.shift(1).fillna(initial)).values


# ---------------------------------------------------------------------------

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default="/tmp/frontier.csv")
    ap.add_argument("--sleeves", default=str(RS.DEFAULT_SLEEVES))
    ap.add_argument("--cache", default="/tmp/rm_sweep_cache.pkl")
    args = ap.parse_args()

    state_path = Path(HERE.parent / "portfolio_state.json")
    state_mtime = state_path.stat().st_mtime

    blob = Path(args.sleeves).read_bytes()
    cache = {}
    if Path(args.cache).exists():
        cache = pickle.loads(Path(args.cache).read_bytes())

    def sim(key, cfg, guard=False, wso=None):
        if key in cache:
            return cache[key]
        t0 = time.time()
        df, s = RS.run(cfg, blob, guard=guard, weight_scale_override=wso)
        out = (returns_of(df), s)
        cache[key] = out
        Path(args.cache).write_bytes(pickle.dumps(cache))
        print("  sim %-44s %5.1fs  wdC %+.3f%%  wdI %+.3f%%"
              % (key, time.time() - t0, 100 * s["worst_day_close"],
                 100 * s["worst_day_intraday"]), flush=True)
        return out

    rows = []
    sleeves_probe = pickle.loads(blob)

    # ---- Phase A -----------------------------------------------------------
    print("\n=== Phase A: operating point (components off) ===", flush=True)
    phase_a = {}
    for r in PHASE_A_RISKS:
        cfg = RS.config_from(r, 0.02, 0.80, components=())
        phase_a[r] = sim("A:r%.4f" % r, cfg)

    for r in PHASE_A_RISKS:
        rets, s = phase_a[r]
        for hf, (mode, pm), block in itertools.product(
                (0.70, 0.80, 0.90), (("none", 0.0), ("slipped", 0.0),
                                     ("leaky", 0.25)), (10, 60)):
            rows.append(dict(phase="A", base_risk=r, halt_fraction=hf,
                             cluster_cap=2.0, components="none", block=block,
                             guard_mode=mode, p_miss=pm,
                             worst_day_close=s["worst_day_close"],
                             worst_day_intraday=s["worst_day_intraday"],
                             max_dd=s["max_dd"], sharpe=s["sharpe"],
                             total_return=s["total_return"],
                             path_step2_day=s["step_2_day"],
                             **mc(rets, hf, mode, pm, block)))

    # ---- Phase B -----------------------------------------------------------
    print("\n=== Phase B: component attribution ===", flush=True)
    for r, comps in itertools.product(PHASE_B_RISKS, PHASE_B_SETS):
        label = "+".join(comps) or "none"
        cfg = RS.config_from(r, 0.02, 0.80, components=comps)
        rets, s = sim("B:r%.4f:%s" % (r, label), cfg)
        rows.append(dict(phase="B", base_risk=r, halt_fraction=0.80,
                         cluster_cap=2.0, components=label, block=10,
                         guard_mode="slipped", p_miss=0.0,
                         worst_day_close=s["worst_day_close"],
                         worst_day_intraday=s["worst_day_intraday"],
                         max_dd=s["max_dd"], sharpe=s["sharpe"],
                         total_return=s["total_return"],
                         path_step2_day=s["step_2_day"],
                         **mc(rets, 0.80, "slipped", 0.0, 10)))

    # ---- Phase C -----------------------------------------------------------
    print("\n=== Phase C: cluster cap (downward only) ===", flush=True)
    for cap, r in PHASE_C:
        wso = recapped_weight_scales(sleeves_probe, cap)
        cfg = RS.config_from(r, 0.02, 0.80, components=())
        rets, s = sim("C:cap%.1f:r%.4f" % (cap, r), cfg, wso=wso)
        rows.append(dict(phase="C", base_risk=r, halt_fraction=0.80,
                         cluster_cap=cap, components="none", block=10,
                         guard_mode="slipped", p_miss=0.0,
                         worst_day_close=s["worst_day_close"],
                         worst_day_intraday=s["worst_day_intraday"],
                         max_dd=s["max_dd"], sharpe=s["sharpe"],
                         total_return=s["total_return"],
                         path_step2_day=s["step_2_day"],
                         **mc(rets, 0.80, "slipped", 0.0, 10)))

    df = pd.DataFrame(rows)
    df.to_csv(args.out, index=False)

    # ---- guard cost, reported separately -----------------------------------
    print("\n=== Guard cost on the REAL path (flatten + re-entries) ===", flush=True)
    print("%8s %12s %12s %8s %10s" % ("risk", "off_equity", "on_equity", "halts", "delta%"))
    guard_rows = []
    for r in GUARD_COST_RISKS:
        cfg = RS.config_from(r, 0.02, 0.80, components=())
        _, off = sim("A:r%.4f" % r, cfg)
        _, on = sim("G:r%.4f" % r, cfg, guard=True)
        d = 100 * (on["end_equity"] / off["end_equity"] - 1)
        guard_rows.append((r, off["end_equity"], on["end_equity"],
                           on["n_halts_daily"], d))
        print("%8.4f %12.0f %12.0f %8d %+10.2f" % guard_rows[-1])

    # ---- readouts ----------------------------------------------------------
    print("\n=== Phase A @ halt_fraction 0.80, guard 'slipped' ===")
    for block in (10, 60):
        print("\n  block=%d %s" % (block, "(regime structure largely destroyed)"
                                   if block == 10 else "(more regime structure kept)"))
        print("  %7s %9s %9s %7s %7s %8s %9s %12s"
              % ("risk", "wdClose%", "wdIntra%", "pass%", "DQ%", "median", "p75",
                 "E[days/fund]"))
        sub = df[(df.phase == "A") & (df.halt_fraction == 0.80)
                 & (df.guard_mode == "slipped") & (df.block == block)]
        for _, x in sub.sort_values("base_risk").iterrows():
            print("  %7.4f %9.3f %9.3f %7.2f %7.2f %8.0f %9.0f %12.0f"
                  % (x.base_risk, 100 * x.worst_day_close,
                     100 * x.worst_day_intraday, x.pass_pct,
                     x.daily_dq_pct + x.total_dq_pct, x.median_days, x.p75_days,
                     x.expected_days_per_funded))

    a80 = df[(df.phase == "A") & (df.halt_fraction == 0.80)
             & (df.guard_mode == "slipped") & (df.block == 10)]
    safe = a80[(a80.daily_dq_pct + a80.total_dq_pct) <= 2.0]
    print("\n=== The two operating points ===")
    if len(safe):
        b = safe.loc[safe.median_days.idxmin()]
        print("  DQ-averse  (DQ<=2%%): risk %.4f  median %.0f days  DQ %.2f%%  pass %.2f%%"
              % (b.base_risk, b.median_days, b.daily_dq_pct + b.total_dq_pct, b.pass_pct))
    else:
        print("  DQ-averse: NO config in Phase A holds DQ <= 2%")
    t = a80.loc[a80.expected_days_per_funded.idxmin()]
    print("  Throughput (re-buy cheap): risk %.4f  E[days/funded] %.0f  DQ %.2f%%"
          % (t.base_risk, t.expected_days_per_funded, t.daily_dq_pct + t.total_dq_pct))

    print("\n=== Phase B: component attribution (vs 'none' at same risk) ===")
    print("  %7s %-18s %9s %8s %7s %9s" % ("risk", "component", "wdIntra%",
                                           "median", "d_days", "DQ%"))
    for r in PHASE_B_RISKS:
        base = df[(df.phase == "B") & (df.base_risk == r)
                  & (df.components == "none")].iloc[0]
        sub = df[(df.phase == "B") & (df.base_risk == r)]
        for _, x in sub.iterrows():
            print("  %7.4f %-18s %9.3f %8.0f %+7.0f %9.2f"
                  % (r, x.components, 100 * x.worst_day_intraday, x.median_days,
                     x.median_days - base.median_days,
                     x.daily_dq_pct + x.total_dq_pct))

    print("\n=== Phase C: cluster cap ===")
    print("  %7s %6s %9s %8s %8s %9s" % ("risk", "cap", "wdIntra%", "ret%",
                                         "median", "DQ%"))
    for _, x in df[df.phase == "C"].iterrows():
        print("  %7.4f %6.1f %9.3f %8.2f %8.0f %9.2f"
              % (x.base_risk, x.cluster_cap, 100 * x.worst_day_intraday,
                 100 * x.total_return, x.median_days,
                 x.daily_dq_pct + x.total_dq_pct))

    assert state_path.stat().st_mtime == state_mtime, "portfolio_state.json was written!"
    print("\nportfolio_state.json mtime unchanged: OK")
    print("DROPPED AXIS: CLUSTER_CAP upward (2.5, 3.0) not measured — the cap does")
    print("  not renormalise, so pre-cap weights are unrecoverable; rebuilding them")
    print("  needs portfolio.py main(), which writes the LIVE portfolio_state.json.")
    print("REDUCED: paths=%d (not 20000); MC modes restricted per phase." % PATHS)
    print("\nwrote %s  (%d rows)" % (args.out, len(df)))


if __name__ == "__main__":
    main()
