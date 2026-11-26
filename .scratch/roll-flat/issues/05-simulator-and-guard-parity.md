# Simulator parity, and what the guard sees when the book is flat

Type: task
Status: resolved
Assignee: lich (claude session 2026-08-10)
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

## Answer

**Both halves land favourably, and the run turned up two things nobody asked about — one
of which says the policy's SCHEDULE cannot be written in UTC.**

### 1. Simulator parity — ported, and the port is exact

`--roll-flat {off,all,indices}` now exists in `scripts/risk_model_sim.py` with byte-identical
semantics to `oanda_book_simulator`: same `INDICES` set, same `elif` against the swap charge
(a bar pays the carry **or** the round trip, never both), same cost
(`2 x _half_spread + _commission` at the bar close), and — like swap — it never enters
`adverse`, so the intraday low it is being ported to measure is untouched by the policy's
own cost.

```
$ python3 scripts/risk_model_sim.py --check-baseline          # flag off
baseline bars      675
max abs equity diff 2.910383e-11  (tolerance 1e-06)
worst day  baseline -1.5898%   harness -1.5898%
end equity baseline 140530.45   harness 140530.45
REPRODUCTION: PASS
```

Cross-harness, on the chosen arm's cost model, one flag at a time
(`simulate()` vs `run(guard=off)`, max abs equity diff over 675 bars):

```
swap only            maxdiff 0.0000e+00  first-bar -   end 106885.79 / 106885.79
spread only          maxdiff 9.1757e+02  first-bar 1 (2024-01-02)
swap+spread          maxdiff 1.1174e+03  first-bar 1 (2024-01-02)
swap+spread+decay    maxdiff 1.1169e+03  first-bar 1 (2024-01-02)
+roll-flat           maxdiff 8.8709e+02  first-bar 1 (2024-01-02)
+weekend selective   maxdiff 7.5472e+02  first-bar 1 (2024-01-02)
+monday reentry      maxdiff 1.5331e+03  first-bar 0 (2024-01-01)
```

**Swap-only reproduces to 0.0 exactly, and adding `--roll-flat` makes the gap SMALLER, not
larger.** The port is faithful. The divergence is `--charge-spread`, it is PRE-EXISTING, and
it starts on the first bar — see §3.

### 2. The chosen arm on the intraday metric

`--risk 0.005 --venue ctrader --charge-swap --charge-spread --neutralise-decay
--end 2026-08-08`, 675 bars:

| | hold (today) | roll-flat indices + weekend selective + re-entry |
|---|---|---|
| total_return | +5.03% | **+19.33%** |
| sharpe | 0.391 | **1.619** |
| max_dd | −7.47% | **−4.94%** |
| worst_day_close | −1.70% | −0.92% |
| **worst_day_intraday** | **−2.07%** | **−1.44%** |
| worst_day_intraday_worst1 | −1.69% | −0.79% |
| **days_past_halt_intraday** (−2.40%) | **0** | **0** |
| **days_past_wall_intraday** (−3.00%) | **0** | **0** |
| entries | 1659 | 2340 |
| swap_paid | −28,434 | −10,888 |
| spread_paid | −1,359 | −3,179 |

The intraday measure does not change the verdict: the arm is better on the figure the firm
actually judges, by 0.63pp of worst day, and neither arm touches the halt line on this path.
Guard ON and guard OFF are **identical for both arms** (no halt ever fires), so the guard
neither helps nor costs here.

Two caveats, stated rather than buried:

- `worst_day_intraday` is the **cotimed bound** — every open sleeve at its worst tick at once.
  The `worst1` row is the floor. The truth is between them; quote both.
- The arm's return here is **+19.33%, not the map's +20.86%**. The difference is §3, not
  roll-flat.

### 3. NEW — the two harnesses do not charge the same round trip

`oanda_book_simulator` charges spread on **exits only** (stop exit, flip close, weekend flat)
and never on an **entry**; `risk_model_sim` charges the entry half-spread **plus commission**
(`risk_model_sim.py:232`). So the sanctioned simulator prices a round trip at half a spread
and no commission — it under-charges every entry in the book.

Consequence: **every `--charge-spread` number on this map is optimistic by roughly 1.5pp of
total return** over 2024-01-01..2026-08-08 (chosen arm 20.86% → 19.33%; hold 6.14% → 5.03%).
The ranking of the arms is unaffected — the cost scales with entry count, and the chosen arm
takes MORE entries (2340 vs 1659), so charging it correctly can only widen the gap in the
direction already chosen. Not fixed here: changing it restates every figure on the map, which
is a decision, not a parity fix. **`risk_model_sim` is the harness to quote from now.**

### 4. The guard — a pre-roll flatten can only LOWER the latched base, never raise it

Answered from a run of the production latch (`prop_guard.update()` with a stale `day` in a
scratch state file, so the real day-roll branch fires):

```
--- floating LOSS of 1,000 at the roll (round trip costs 50) ---
HOLD through the roll       balance 100000.00  equity  99000.00 -> base 100000.00  floor 97000.00  room 2.000%
ROLL-FLAT: closed at 20:50  balance  98950.00  equity  98950.00 -> base  98950.00  floor 95981.50  room 3.000%
   base moved -1050.00
--- floating PROFIT of 1,000 at the roll ---
HOLD through the roll       balance 100000.00  equity 101000.00 -> base 101000.00  floor 97970.00  room 3.000%
ROLL-FLAT: closed at 20:50  balance 100950.00  equity 100950.00 -> base 100950.00  floor 97921.50  room 3.000%
   base moved -50.00
--- partial flatten: indices only, 600 of the 1,000 loss realised ---
HOLD through the roll       balance 100000.00  equity  99000.00 -> base 100000.00  floor 97000.00  room 2.000%
ROLL-FLAT indices (30 cost) balance  99370.00  equity  98970.00 -> base  99370.00  floor 96388.90  room 2.597%
   base moved -630.00
```

The mechanism: `daily_base = max(balance, equity)` and closing converts unrealized into
realized, so `balance` collapses onto `equity`.

- **Floating loss** — held, the base is the HIGHER `balance`, and the day starts already 1%
  down its own 3%. Flattened first, the base is `equity` and the day starts with the **full
  3% of room**. Indices-only recovers the fraction it realises (2.00% → 2.60%).
- **Floating profit** — the base is `equity` either way; flattening moves it only by the
  round-trip cost.

So the interaction is **strictly favourable or neutral, never adverse**, and it is largest
exactly when it matters (a losing book into the roll). No guard change is needed. Note this
benefit is real only if the book is still flat AT the latch — see §5.

### 5. NEW, and it is the important one — the schedule cannot be written in UTC

The halt path and a policy flatten cannot fight, but the day boundary sits **between** the
close and the reopen for half the year. `prop_guard._trading_day` on the broker clock
(America/New_York + 7h), run for both regimes:

```
2026-08-11 (US DST, server UTC+3)      2026-12-15 (US standard, server UTC+2)
  20:50 UTC -> 2026-08-11                20:50 UTC -> 2026-12-15
  21:05 UTC -> 2026-08-12                21:05 UTC -> 2026-12-15
  daily halt latched at 20:50 still binding at 21:05?  False  |  True
  TOTAL halt latched at 20:50 still binding at 21:05?  True   |  True
```

Three things follow, none of which the map accounts for:

1. **The roll instant is 21:00 UTC in summer and 22:00 UTC in winter.** A policy pinned to
   fixed UTC clock times closes at 20:50 and reopens at 21:05 — which in winter puts the book
   back ON the books a full 55 minutes BEFORE the roll. **The policy silently pays the entire
   carry for ~4.5 months a year, and the §4 daily-base benefit vanishes with it.** The
   schedule must be expressed on the broker clock (17:00 New York), exactly as
   `prop_guard._trading_day` already is — this is ticket 03's real constraint.
2. **A daily halt does not survive the roll in summer** (it is keyed on the trading-day
   label), so a 21:05-style reopen is NOT blocked by a halt latched earlier that day. That is
   legitimate — the firm's daily loss resets at the same instant — but it means the reopen
   pass is also the halt's re-entry, at full size. A TOTAL halt blocks in both regimes.
3. **21:05 UTC is not the live trigger and has not been since 2026-07-28.** The pod fires
   **hourly at :15 and acts only in the 00:00 UTC hour** (`zeabur_interlock.sh cron-install`),
   precisely because 21:05 UTC sat 15 minutes inside the index session close and every index
   order was rejected. The map and the handoff both still say "21:05". Since a policy close
   just writes `FLAT(0)` (ticket 01), **the existing 00:15 UTC pass is already a working
   reopen** — 03 may only need to add the pre-roll CLOSE pass. The cost is that the reopen
   lands ~2h after the index session reopens, exposure the simulator prices at zero.

## Evidence

- `python3 scripts/risk_model_sim.py --check-baseline` — `REPRODUCTION: PASS`, 2.9e-11
- `tests/test_risk_model_sim_roll_flat.py` — 5 tests pinning the port as a cross-harness
  contract (equity parity with `simulate()`, the carry/round-trip `elif`, instrument
  scoping, off-by-default, and that the cost never enters the intraday low): `5 passed`
- Full suite — see the map's Decisions row
- Probes kept out of the repo (session scratchpad): `parity_bisect.py`,
  `daily_base_probe.py`
