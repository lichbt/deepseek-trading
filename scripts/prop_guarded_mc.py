#!/usr/bin/env python
"""Two-step prop MC that models the BREAKER and the CONSISTENCY BAR.

This is the sizing script. `prop_realsim_mc` and `prop_twostep_mc` answer "does the
book survive at the sizing it already runs"; this one answers "can we go faster",
which needs two things neither of them has:

1. THE GUARD. fix_runner halts and flattens at PROP_HALT_FRACTION of the daily limit
   (0.80 * 3% = -2.40%). A day that would have closed past that never does. Ignoring
   the breaker made 0.010 look like a 33% DQ; modelling it gives 99.4%.

2. CONSISTENCY AS A RAISED BAR. The5ers formula is
       (best single day profit) / cap = required total profit
   so missing it does NOT disqualify — you keep trading until profit catches up. The
   step is approved when `profit >= max(target, best_day / cap)`, with best_day a
   RUNNING MAX so a new record day pushes the bar out again. Treating it as pass/fail
   is wrong and understates pass rates badly (it produced a bogus 76% for the live
   config on 2026-08-06).

GUARD MODELS, in increasing honesty:
  none      the series as traded.
  perfect   any day past the halt closes exactly at it. Assumes the guard sees the
            move, fires, and all sleeves close with no slippage. NOT attainable.
  slipped   closes at halt minus --slip, for 5-minute sampling lag plus the serial
            cancel_stop/close_position of every open sleeve plus spread on each exit.
  leaky     as slipped, but with probability --p-miss the guard does nothing and the
            full day lands. Covers what a sampled breaker structurally cannot catch:
            a weekend/news gap opening past the level (~20% of book days follow a
            >1-day calendar gap), an equity fetch that throws -> "GUARD INACTIVE this
            tick" (what ALREADY_SUBSCRIBED did until 3c56168), and flatten_all
            aborting a sleeve on an unconfirmed stop cancel.

TWO KNOWN OPTIMISMS, neither fixable with daily bars:
  - Truncation only ever HELPS. The real guard also fires on days that dip past the
    level intraday and would have closed green, locking the halt loss and paying a
    full set of re-entries. Not modelled, so guarded figures beat reality.
  - The consistency numerator here is best NET daily equity gain. The5ers uses GROSS
    winning trades with same-day losers NOT deducted, a larger numerator. Every
    consistency figure is therefore a floor. Stress it by lowering --consistency.

The bootstrap caveat that applies to every MC in this repo is unchanged: resampling
historical days CANNOT produce a day worse than the worst that occurred. A 0.00% DQ
rate means "no resampled day exceeded the observed worst", never "impossible".

Usage:
    ./venv/bin/python scripts/prop_guarded_mc.py \
        --curve 0.005=/tmp/book.csv --curve 0.010=/tmp/book_r10.csv

Curves come from `oanda_book_simulator.py --venue ctrader` — the label is free text
(conventionally the RISK it was run at) and is only used for display.
"""
import argparse

import numpy as np
import pandas as pd

MODES = ("none", "perfect", "slipped", "leaky")


def load(path):
    equity = pd.read_csv(path)["equity"].astype(float).values
    if len(equity) < 50:
        raise SystemExit(f"{path}: only {len(equity)} bars — too short to bootstrap")
    return np.diff(equity) / equity[:-1]


class _Acct:
    """Account-level path statistics, carried ACROSS both steps.

    Separate from the step equity because the two answer different questions and
    were conflated once already. run_step resets `equity` to 1.0 at each step —
    correct for the profit target, useless for risk — so drawdown and profit
    factor accumulate here instead, over the whole challenge.

    TWO DIFFERENT DRAWDOWNS, and only one of them disqualifies:

      max_dd    peak-to-trough, the ordinary risk statistic. Does NOT end the
                challenge: shedding 16% off a high-water mark is survivable.
      low       the low-water mark against the INITIAL balance. THIS is the rule
                — The5ers' max loss is a static line 10% below where you started
                and never trails the peak. A path can post a fearsome max_dd and
                never come near it.

    Reporting max_dd as if it were the breach rate overstates failure several-fold.
    """

    __slots__ = ("equity", "peak", "max_dd", "low", "gain", "loss")

    def __init__(self):
        self.equity = self.peak = self.low = 1.0
        self.max_dd = 0.0
        self.gain = self.loss = 0.0

    def book(self, x):
        prev = self.equity
        self.equity *= 1 + x
        d = self.equity - prev
        if d >= 0:
            self.gain += d
        else:
            self.loss -= d
        self.peak = max(self.peak, self.equity)
        self.max_dd = min(self.max_dd, self.equity / self.peak - 1.0)
        self.low = min(self.low, self.equity)

    @property
    def profit_factor(self):
        """Gross daily gain / gross daily loss. inf when a path never lost a day."""
        return self.gain / self.loss if self.loss else float("inf")


def run_step(target, returns, rng, a, total=None, acct=None):
    """-> (outcome, days) for one challenge step, guard and consistency bar applied.

    `acct`, when given, accumulates the account-level path statistics; it is fed
    the SAME post-guard return the step books, so the two can never disagree.
    """
    n, equity, days, best = len(returns), 1.0, 0, 0.0
    caught_at = a.halt - a.slip
    flat_left = 0
    while days < a.max_days:
        i = rng.integers(0, n - a.block)
        for x in returns[i:i + a.block]:
            days += 1
            # WHAT THE HALT LEAVES BEHIND. `--post-halt-flat K` books K days at
            # zero after the breaker fires: the book is out of the market and
            # neither loses nor earns. K=0 is the PAUSE fix_runner actually runs
            # (FLAT(0), re-establishes on the next pass); K>0 prices the SURRENDER
            # counterfactual, where the signal is preserved and nothing re-enters
            # until it genuinely changes. Bootstrapped days are exchangeable, so
            # the flat window can only be a fixed length here — calibrate K from
            # risk_model_sim's --halt-resume wait arm on the real path.
            if flat_left > 0:
                flat_left -= 1
                x = 0.0
            # The guard fires BEFORE the day is booked: it cannot un-lose what the
            # book already gave up, it only stops the rest of the session.
            elif a.mode != "none" and x < caught_at:
                if a.mode == "perfect":
                    x = a.halt
                    flat_left = a.post_halt_flat
                elif a.mode == "slipped" or rng.random() >= a.p_miss:
                    x = caught_at
                    flat_left = a.post_halt_flat
            prev = equity
            equity *= 1 + x
            if acct is not None:
                acct.book(x)
            best = max(best, equity - prev)
            if x <= a.daily:
                return "daily", days
            if equity - 1 <= (a.total if total is None else total):
                return "total", days
            if equity - 1 >= max(target, best / a.consistency):
                return "pass", days
            if days >= a.max_days:
                break
    return "timeout", days


def run(label, returns, a):
    rng = np.random.default_rng(a.seed)
    res = {"pass": 0, "daily": 0, "total": 0, "timeout": 0}
    days, dds, pfs, lows = [], [], [], []
    for _ in range(a.paths):
        total_days, alive = 0, True
        acct = _Acct()
        # THE ACCOUNT FLOOR IS ONE LINE FOR THE WHOLE CHALLENGE. The5ers' max loss
        # is STATIC: $10,000 below the INITIAL balance, and it does not trail up
        # with profit. Step 2 therefore starts 10% above its own floor and has
        # ~18% of room, not 10% — measuring -10% from each step's start (the
        # --total-mode step default, kept for continuity) overstates step-2 risk.
        bal = 1.0
        for target in a.targets:
            floor = None
            if a.total_mode == "static":
                floor = (1.0 + a.total) / bal - 1.0
            outcome, d = run_step(target, returns, rng, a, total=floor, acct=acct)
            total_days += d
            if outcome != "pass":
                res[outcome] += 1
                alive = False
                break
            # A passed step ends AT LEAST `target` up (the consistency bar can
            # push it further), so carrying exactly the target is the pessimistic
            # reading of how much room the static floor leaves the next step.
            bal *= 1.0 + target
        if alive:
            res["pass"] += 1
            days.append(total_days)
        dds.append(acct.max_dd)
        lows.append(acct.low - 1.0)
        if np.isfinite(acct.profit_factor):
            pfs.append(acct.profit_factor)
    q = np.percentile(days, [50, 75, 95]) if days else [-1, -1, -1]
    print(f"  {label:>8s} | pass {100*res['pass']/a.paths:6.2f}%  "
          f"DQ-daily {100*res['daily']/a.paths:5.2f}%  "
          f"DQ-total {100*res['total']/a.paths:5.2f}%  "
          f"timeout {100*res['timeout']/a.paths:5.2f}%  "
          f"| days med {q[0]:4.0f}  p75 {q[1]:4.0f}  p95 {q[2]:4.0f}")
    if not a.risk_stats:
        return
    dd, low, pf = 100 * np.array(dds), 100 * np.array(lows), np.array(pfs)
    print(f"  {'':>8s} | maxDD  med {np.percentile(dd,50):6.2f}%  "
          f"p95 {np.percentile(dd,5):6.2f}%  p99 {np.percentile(dd,1):6.2f}%  "
          f"WORST {dd.min():6.2f}%   (peak-to-trough; does NOT disqualify)")
    print(f"  {'':>8s} | low vs INITIAL  med {np.percentile(low,50):6.2f}%  "
          f"p99 {np.percentile(low,1):6.2f}%  WORST {low.min():6.2f}%   "
          # `low` is in PERCENT here, a.total is a FRACTION — scale or this reads
          # 80%+ instead of a fraction of a percent.
          f"-> touched the static line: {100*(low <= 100*a.total).mean():.2f}%")
    print(f"  {'':>8s} | profit factor  med {np.percentile(pf,50):5.2f}  "
          f"p5 {np.percentile(pf,5):5.2f}  p95 {np.percentile(pf,95):5.2f}  "
          f"MAX {pf.max():5.2f}")


def main():
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--curve", action="append", required=True, metavar="LABEL=CSV",
                   help="repeatable; CSV from oanda_book_simulator.py --venue ctrader")
    p.add_argument("--mode", choices=MODES, default=None,
                   help="omit to sweep every model, which is the useful default")
    p.add_argument("--halt", type=float, default=-0.024,
                   help="PROP_DAILY_DD_LIMIT * PROP_HALT_FRACTION (default 3%% * 0.80)")
    p.add_argument("--slip", type=float, default=0.004,
                   help="lag + serial close + spread, as a fraction (default 0.4pp)")
    p.add_argument("--post-halt-flat", type=int, default=0, metavar="K",
                   help="days booked flat after the breaker fires. 0 = the PAUSE "
                        "live runs (re-establishes next pass); K>0 prices the "
                        "wait-for-a-new-signal SURRENDER")
    p.add_argument("--p-miss", type=float, default=0.10,
                   help="probability the guard does nothing, for --mode leaky")
    p.add_argument("--daily", type=float, default=-0.03)
    p.add_argument("--total", type=float, default=-0.10,
                   help="the account max-loss line, as a fraction of the INITIAL "
                        "balance")
    p.add_argument("--total-mode", choices=("step", "static"), default="step",
                   help="step = measure it from each step's start (harsher on "
                        "step 2, the historical default); static = one fixed line "
                        "for the whole challenge, which is The5ers' actual rule")
    p.add_argument("--consistency", type=float, default=0.50,
                   help="lower it to stress the gross-winners numerator")
    p.add_argument("--risk-stats", action="store_true",
                   help="also report per-path max drawdown, low-water mark against "
                        "the INITIAL balance, and profit factor. The two drawdowns "
                        "are different rules: peak-to-trough does NOT disqualify, "
                        "the static low-water line does")
    p.add_argument("--targets", default="0.10,0.05")
    p.add_argument("--paths", type=int, default=20000)
    p.add_argument("--block", type=int, default=10)
    p.add_argument("--max-days", type=int, default=2000)
    p.add_argument("--seed", type=int, default=7)
    a = p.parse_args()

    a.targets = [float(t) for t in a.targets.split(",")]
    curves = {}
    for spec in a.curve:
        if "=" not in spec:
            raise SystemExit(f"--curve wants LABEL=CSV, got {spec!r}")
        label, path = spec.split("=", 1)
        curves[label] = load(path)

    print(f"rules       daily {a.daily:.1%}  total {a.total:.1%} ({a.total_mode})  "
          f"consistency {a.consistency:.0%}  targets {a.targets}")
    print(f"guard       halt {a.halt:.2%}  slip {a.slip:.2%}  p_miss {a.p_miss:.0%}"
          f"  post-halt flat {a.post_halt_flat}d")
    print(f"paths       {a.paths}  block {a.block}")
    for label, r in curves.items():
        print(f"input       {label}: {len(r)} daily returns, worst {r.min()*100:.2f}%")

    for mode in ([a.mode] if a.mode else MODES):
        a.mode = mode
        tag = f"{mode} (p_miss {a.p_miss:.0%})" if mode == "leaky" else mode
        print(f"\n--- guard: {tag} ---")
        for label, r in curves.items():
            run(label, r, a)

    if a.mode != "none":
        print("\n  NOTE truncation only ever HELPS here. The real breaker also fires on\n"
              "  days that dip past the halt intraday and would have closed green,\n"
              "  locking the loss and paying a full set of re-entries. Not modelled —\n"
              "  every guarded figure above is better than reality.")


if __name__ == "__main__":
    main()
