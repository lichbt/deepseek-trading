#!/usr/bin/env python3
"""Two-step prop-challenge Monte Carlo with the consistency rule.

scripts/prop_realsim_mc.py models ONE step. The5ers' real product is two
(+10% then +5%), which roughly doubles the exposure window — and that is what
kills aggressive sizing, since the daily-breach hazard is per-day. Measured
2026-08-04 on the corrected (--venue ctrader) book: RISK 0.010 clears a single
step easily but reaches only 68.9% over both, at a 30.9% daily-breach rate.

Consumes the daily equity CSV written by
    oanda_book_simulator.py --venue ctrader --csv <file>
Sizing must come from the PROP venue or the whole run is meaningless; see
.claude/skills/sleeve-ops/references/montecarlo.md.

RULES MODELLED
  - max loss is STATIC, measured from the step's starting balance. It does NOT
    trail up with profit (confirmed with the user 2026-08-04). A trailing floor
    would be materially harsher and this script does not model it.
  - consistency = (best day profit / total accumulated profit) * 100 <= 50,
    evaluated at the moment the step's target is reached.

CAVEAT, the same one that applies to every bootstrap here: resampling historical
days CANNOT produce a day worse than the worst that occurred. A daily_breach of
0.00% means "no resampled day exceeded the observed worst", never "impossible".
"""
import argparse

import numpy as np
import pandas as pd


def load_returns(path):
    equity = pd.read_csv(path)["equity"].astype(float).values
    if len(equity) < 50:
        raise SystemExit(f"{path}: only {len(equity)} bars — too short to bootstrap")
    return np.diff(equity) / equity[:-1]


def run_step(target, returns, rng, daily, total, consistency, block, max_days):
    """-> (outcome, days, consistency_ok) for one challenge step."""
    n = len(returns)
    equity, days, best_day = 1.0, 0, 0.0
    while days < max_days:
        i = rng.integers(0, n - block)
        for x in returns[i:i + block]:
            days += 1
            prev = equity
            equity *= 1 + x
            best_day = max(best_day, equity - prev)
            if x <= daily:
                return "daily", days, False
            if equity - 1 <= total:          # STATIC floor, not trailing
                return "total", days, False
            if equity - 1 >= target:
                return "pass", days, best_day <= consistency * (equity - 1)
            if days >= max_days:
                break
    return "timeout", days, False


def main():
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("csv", help="equity CSV from oanda_book_simulator.py --venue ctrader")
    p.add_argument("--targets", default="0.10,0.05", help="per-step profit targets")
    p.add_argument("--daily", type=float, default=-0.03)
    p.add_argument("--total", type=float, default=-0.10)
    p.add_argument("--consistency", type=float, default=0.50,
                   help="max share of total profit one day may contribute (0 = off)")
    p.add_argument("--paths", type=int, default=20000)
    p.add_argument("--block", type=int, default=10)
    p.add_argument("--max-days", type=int, default=1000)
    p.add_argument("--seed", type=int, default=7)
    args = p.parse_args()

    returns = load_returns(args.csv)
    targets = [float(t) for t in args.targets.split(",")]
    rng = np.random.default_rng(args.seed)
    consistency = args.consistency or 1e9

    res = {"pass": 0, "daily": 0, "total": 0, "timeout": 0}
    days_all, steps_done, consist_fail = [], 0, 0
    for _ in range(args.paths):
        total_days, survived = 0, True
        for target in targets:
            outcome, days, ok = run_step(target, returns, rng, args.daily, args.total,
                                         consistency, args.block, args.max_days)
            total_days += days
            if outcome != "pass":
                res[outcome] += 1
                survived = False
                break
            steps_done += 1
            consist_fail += not ok
        if survived:
            res["pass"] += 1
            days_all.append(total_days)

    print(f"source        {args.csv}")
    print(f"paths         {args.paths}  steps {targets}  block {args.block}")
    print(f"rules         daily {args.daily:.1%}  static total {args.total:.1%}  "
          f"consistency {args.consistency:.0%}")
    print(f"input         {len(returns)} daily returns, worst {returns.min()*100:.2f}%")
    print()
    for key in ("pass", "daily", "total", "timeout"):
        print(f"  {key:8s} {100 * res[key] / args.paths:6.2f}%")
    if days_all:
        q = np.percentile(days_all, [25, 50, 75])
        print(f"  days to FUNDED (all steps): median {q[1]:.0f}  p25 {q[0]:.0f}  p75 {q[2]:.0f}")
    if steps_done:
        print(f"  consistency violations: {consist_fail} of {steps_done} completed steps "
              f"({100 * consist_fail / steps_done:.2f}%)")
    if res["daily"] == 0:
        print("\n  NOTE daily breach 0.00% is STRUCTURAL, not safety: a block bootstrap "
              "cannot\n  resample a day worse than the historical worst. Never quote it "
              "as a bound.")


if __name__ == "__main__":
    main()
