# Simulator parity, and what the guard sees when the book is flat

Type: task
Status: open
Blocked by: —

## Question

Two things must agree with the live policy before it ships, and neither is about the runner.

**Simulator parity.** `--roll-flat {all,indices}` exists in `oanda_book_simulator` but not
in `scripts/risk_model_sim.py`, which is the only harness that measures the INTRADAY low —
the figure The5ers actually judges, and which runs ~19% worse than close-to-close. Every
number on the map is close-to-close. Carry it across, keeping the semantics identical, or
`--check-baseline` stops meaning anything.

**The guard.** The5ers anchors daily loss on `max(balance, equity)` latched at the day
roll. Closing every index position at 20:50 crystallises floating P&L into balance
immediately before that latch — which is exactly when `prop_guard` snapshots `daily_base`.
Does the latched base change? Does a flat book at the roll interact with the halt path,
which already sets `prev_target = 0`?

This is a behaviour question, not a code-style one: answer it from a run, not from reading
`prop_guard.py`.

## Definition of done

`risk_model_sim --check-baseline` still reproduces `simulate()` to 1e−6 with the flag off,
and the chosen arm re-measured WITH the intraday metric — `worst_day_intraday`,
`days_past_halt_intraday`, `days_past_wall_intraday`. A written answer on the `daily_base`
interaction with the evidence that produced it. Paste the numbers.
