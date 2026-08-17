# montecarlo — will the book survive the prop rules?

Target: The5ers $100k — **TWO STEPS: +10% then +5%**, **3% daily DD**, **$10,000 static
max loss** (from the initial balance; it does not trail up with profit), unlimited time,
**50% consistency rule** = `(best day profit ÷ total accumulated profit) × 100 ≤ 50`.
One daily breach is an instant DQ, so the daily figure is the binding constraint.

Two steps roughly **doubles the exposure window**, which is what kills aggressive
sizing: measured 2026-08-04, RISK 0.010 passes a single step comfortably but reaches
only 68.9% over both, with a **30.9% daily-breach rate**. `scripts/prop_realsim_mc.py`
models ONE step — for an accept/reject on the real product, use the two-step run.

> ⚠ **EVERY TABLE BELOW PREDATES THE CARRY POLICY.** They were run 2026-08-04..08,
> before roll-flat, weekend-flat and swap costing existed, on curves generated with
> no `--charge-swap`, `--charge-spread`, `--roll-flat` or `--weekend-flat`. They
> describe a book the pod has not run since 2026-08-10. Treat them as the method and
> the shape of the sensitivity, **not as current numbers** — regenerate before
> quoting any of them. The correct command is in §3 below.
>
> **The deployed book, re-measured 2026-08-17** (20k paths, static max-loss, leaky
> guard, effective risk 0.0055 = BASE_RISK 0.005 × BOOK_SCALE 1.1):
>
> | | pass | DQ-total | median days | maxDD worst | PF median |
> |---|---|---|---|---|---|
> | live (weekend-flat **with** Monday re-entry) | 99.94% | 0.07% | 423 | -16.66% | 1.40 |
> | the sit-out policy it replaced | 100.00% | 0.00% | 492 | -12.04% | 1.42 |
>
> **TWO DRAWDOWNS, ONE RULE.** Peak-to-trough maxDD does NOT disqualify — The5ers'
> max loss is a static line below the INITIAL balance and never trails the peak. The
> deployed arm's worst peak-to-trough is -16.66% while its worst low-water mark is
> -10.53%, and only 0.07% of paths touch the line. Quote the low-water figure when
> the question is survival; the maxDD when the question is comfort.
>
> **SCALING IS DEARER UNDER RE-ENTRY THAN THE STANDING GUIDANCE ASSUMES.** DQ grows
> ~15x faster with size: at 0.0078 it is 0.62% against the sit-out's 0.04%, and at
> 0.0100 it is 3.56% against 0.75%. The 2026-08-14 "effective 0.007 sweet spot" was
> calibrated on the retired policy — re-derive it before any scale-up.

**Consistency is a RAISED BAR, not a failure mode**, and getting that wrong inverts
the answer. The5ers formula is `(best single day profit) / cap = required total
profit`, so missing it does not disqualify — you keep trading until profit catches
up. Model it as: step approved when `profit >= max(target, best_day / cap)`, with
`best_day` a RUNNING MAX so a new record day pushes the bar out again. The cost is
DAYS, not attempts, and this book barely notices: 478 -> 485 days at 0.005 under a
30% cap, 321 -> 334 at 0.0075. Diversification is why — no single day carries the
profit. Treating it as pass/fail produced a bogus "76% pass" for the live config
(measured 2026-08-06).

Second-order effect worth keeping: **a tighter cap erases the value of higher risk**,
because a faster run concentrates the same profit into fewer days. At an effective
10% cap 0.005 and 0.010 converge (881 vs 870 days) — the rule prices speed directly.
Note also that The5ers computes the numerator on GROSS winning trades with same-day
losers NOT deducted, so any net-based figure is a floor; stress-tested at effective
15% and 10% caps, pass rates hold at 0.005/0.0075 and only the timeline moves.

## Only one sanctioned path

```bash
source ~/.zshrc

# 1. Real-sized book sim — actual _compute_position_size, Kelly, decay, min-lot clamps
#    --venue ctrader is MANDATORY for any prop figure. See "Pick the venue" below.
./venv/bin/python oanda_book_simulator.py \
    --start 2024-01-01 --end "$(date +%F)" \
    --risk 0.005 --max-risk 0.02 --venue ctrader \
    --csv /tmp/book.csv 2>&1 | tail -30

# 2. Block-bootstrap those real daily returns against the prop rules
./venv/bin/python scripts/prop_realsim_mc.py /tmp/book.csv 2>&1 | tail -40

# 3. Sizing question ("can we go faster?") — models the breaker and the consistency
#    bar, which neither script above does. Needs one CSV per candidate RISK.
#
#    THE CURVE MUST BE COSTED AND CARRY-POLICIED or the answer describes a book
#    nobody runs. risk_model_sim is the harness to generate it (it charges the entry
#    half-spread AND commission, and is the only one measuring the intraday floating
#    low); the flags below mirror the pod exactly — check them against
#    `./scripts/zeabur_interlock.sh risk` rather than trusting this line.
./venv/bin/python scripts/risk_model_sim.py \
    --start 2024-01-01 --end <today> --risk 0.0055 --venue ctrader \
    --charge-swap --charge-spread --guard on \
    --roll-flat NAS100_USD,DE30_EUR,XAU_USD \
    --weekend-flat SPX500_USD,XAG_USD,XCU_USD --monday-reentry \
    --csv /tmp/book.csv

#    --monday-reentry models WEEKEND_FLAT_REENTRY=1, the DEPLOYED default since
#    2026-08-17. Omit it and you model the retired sit-out. It fills at the Sunday
#    21:00 open while the pod reopens 00:15 UTC Monday, so it reads ~3.25h optimistic.
#
#    --risk-stats adds per-path max drawdown, low-water mark and profit factor.
./venv/bin/python scripts/prop_guarded_mc.py \
    --curve 0.0055=/tmp/book.csv --curve 0.0078=/tmp/book_r78.csv \
    --total-mode static --risk-stats 2>&1 | tail -30
```

**`--total-mode static` is the real rule** and is NOT the default (the default is
`step`, kept for continuity with older runs). Static measures the $10,000 max loss
from the INITIAL balance for the whole challenge; `step` re-measures from each step's
start and overstates step-2 failure roughly 2x. Any account-DD figure quoted from
this repo must name which mode produced it.

**`prop_realsim_mc` and `prop_twostep_mc` are consistency-blind by design.** Both
count violations but neither fails or delays the path on one (`prop_twostep_mc.py:89-96`).
That is the right call — consistency is not a failure mode — but it means their
headline pass% carries no day-cost, so quoting it alone understates the timeline. The
99.69% in the 2026-08-05 record is that number.

## Pick the venue — this is not a detail

`--venue` selects which broker's **volume minimums** the book is sized against,
and the two disagree by up to three orders of magnitude **in both directions**:

| | OANDA min | cTrader min |
|---|---|---|
| NAS100 / SPX500 / DE30 | 1.0 | **0.01** (100× finer) |
| every FX pair | 1.0 | **1000** (1000× coarser) |
| XAG_USD / XCU_USD | 1.0 | 50 / 250 |

Sizing floors *up* to the minimum, so scoring the prop book with the OANDA table
fabricates index positions ~100× too large. Measured 2026-08-04, $100k @ 0.005:
OANDA specs report worst day −2.39% / maxDD −5.49%; cTrader specs −1.51% / −3.86%.
**Every prop number produced before that date is wrong** — roughly twice as risky
and twice as profitable as the live book. At $10k the error was far larger (a
fictitious −9.42% worst day against a real −2.93%).

- **`--venue ctrader`** — the prop book. Also switches to `fix_runner`'s **skip**
  semantics: an open is refused outright when one minimum lot already implies more
  than `MAXRISK`. Opt out with `--no-skip-min-lot` only to model `live_test`.
- **`--venue oanda`** (default) — the paper book, which *floors* and always trades.

The venue is printed on every run. If a figure is quoted without one, it is not
usable for a prop decision.

Step 1 reads `portfolio_state.json`, so it reflects the **current** book — regenerate
that file first if you have just deployed or retired anything (`deploy.md`).
Start at 2024-01-01 for an OOS-ish window; sleeves were fit on earlier data.

## Do NOT size from the reconstruction scripts

`scripts/prop_daily_breach_mc.py` and `scripts/prop_pass_curve_mc.py` rebuild the book
as `raw_return × portfolio_weight` rather than the real risk-budgeted position sizing.
That **understates the book roughly 8×**. They produced the retracted "safe 2.5× /
best 3.5×" recommendation; on the real book that same 3.5× turns a −2.79% worst day
into −9.8% — instant DQ. They remain in the repo for the correlation-structure
question they were written for; **never quote them for sizing.**

If a run of theirs is already in context, ignore its `scale` column entirely.

## Reading the output

- **Worst single day** is the number that matters. It sits close to the −3% wall and
  the margin is thin — a couple of tenths of a percent. Treat any change that widens
  the worst day as a serious regression.
- **Bootstrap daily-breach % of 0.00 does not mean safe.** The block bootstrap
  resamples historical days, so it *cannot* produce a day worse than the historical
  worst. A 0% breach rate means "no resampled day exceeded the observed worst",
  not "breach is impossible". State that caveat whenever you report the figure.
- **Pass odds are horizon-sensitive** — far higher with no time limit than inside 60
  days. Report the horizon alongside the number or it's meaningless.
- **Total DD** has historically had far more headroom than daily. Don't lead with it.

## Sizing rules

**Only base `RISK` sets magnitude.** Kelly (2× on winners, 0.5× on losers), the
`CONVICTION` multipliers, and cluster weights only *redistribute* a cap-bound pie.
So the levers are not equivalent:

- Raising `RISK` scales the worst day roughly linearly → the DQ risk.
- Lowering `RISK` barely helps: the worst day is driven by **co-movement across
  sleeves**, not by clip size. Dropping 0.005 → 0.003 moves it only a few tenths.
- Lowering `MAXRISK` does essentially nothing — it's a per-trade ceiling that rarely
  binds, and it is **per-trade, not per-account**: several positions stack toward the
  3% account wall independently of it.
- **Sleeve count N is a real lever** — see the SKILL.md note on `_apply_cluster_caps`
  not renormalising. Recompute deployed risk after any book change.

**Conclusion: 0.005 is the default. DO NOT SCALE UP on the current book — 0.0075
now BREACHES the daily wall.**

⚠ **RE-MEASURED 2026-08-08 and the earlier table is STALE. Every figure in it
understates the worst day by ~39%.**

| RISK | worst day (2026-08-06, 23 sleeves) | **worst day (2026-08-08, 24 sleeves)** |
|---|---|---|
| 0.005  | -1.51% | **-2.10%**  (0.90 pp of margin, not ~1.5) |
| 0.0075 | -2.32% | **-3.02%**  ← BREACHES the 3% wall. Instant DQ. |
| 0.010  | -3.13% | **-4.04%** |

NOT caused by the sleeve count: at 0.005 the worst day is -2.10% at BOTH 23 and 24
sleeves, measured back to back. The likely cause is the operating point itself — the
old table predates the equal-weight + `CLUSTER_CAP` 3.0 rebuild (commit 5827729,
2026-08-08), and `.env` now carries `WEIGHTING=equal`, `CLUSTER_CAP=3.0`. Those
figures were measured under a configuration that no longer exists.

**Whether equal weighting at cap 3.0 genuinely made the book riskier, or the old
numbers were simply measured on a different config, is UNRESOLVED** — and it should be
settled before any sizing decision, because the two readings imply opposite actions.

Everything below this line is from the superseded 2026-08-06 run. Its RATIOS and its
reasoning about the breaker still hold; its ABSOLUTE worst-day figures do not.

| RISK | worst day | pass, no guard | pass, breaker modelled | median days |
|---|---|---|---|---|
| 0.005  | -1.51% | 99.98% | — (never reaches the halt) | 485 |
| 0.0075 | -2.32% | 99.82% | — (below the -2.40% halt)  | 334 |
| 0.010  | -3.13% | 66.50% (33.3% DQ) | 99.39% | 265 |

At 0.010 the book has ONE realised sub-3% day: 2024-06-06, a Thursday after a normal
1-day gap — intraday, NOT a weekend gap, which is the case a 5-minute sampled breaker
catches. Modelling the halt (truncate at -2.4% plus 0.4pp lag/slippage) removes the DQ
entirely: 220 days saved for 0.6pp of pass probability. So 1% IS viable **if the
breaker fires**. Sensitivity to misses: 10% -> 95.2%, 25% -> 89.0%, 50% -> 80.2%.

⚠ **The "prefer 0.0075" advice below is SUPERSEDED by the 2026-08-08 re-measure.**
On the current book 0.0075 has a -3.02% worst day, i.e. it breaches the wall outright
rather than sitting below the halt. The reasoning is kept because it is sound for the
book it was measured on; the recommendation is not currently actionable.

**Prefer 0.0075 before 0.010.** Its worst day sits BELOW the halt level, so it reaches
334 days with NO guard dependency; 0.010 buys ~69 more days and pays with a hard
dependency on the breaker. Sequence: confirm the account's daily limit (3% vs 5%) and
consistency cap (30% vs 50%), finish the `halt-set` dry run that proves the flatten
path end to end, then 0.0075, then 0.010 only on real breaker history.

Two things the guard cannot cover, so do not read 99.39% as a bound: ~20.7% of book
days open after a >1-day calendar gap (a Sunday open past the halt is uncatchable),
and `_guard_equity` swallowing an exception yields "GUARD INACTIVE this tick" — which
is exactly what `ALREADY_SUBSCRIBED` did until 3c56168. The truncation model is also
optimistic by construction: it only ever helps, while the real guard also fires on
days that dip intraday and would have closed green, locking -2.4% and paying 23
re-entries. That cost is not modelled anywhere.

`scripts/prop_guarded_mc.py` is the run behind this table — re-run it after any book
change; every figure here moves with the sleeve count.

## Companion checks

```bash
./venv/bin/python stress_book.py 2>&1 | tail -40    # run after EVERY deploy
```

Per-sleeve Sharpe and maxDD miss the risk that many sleeves open the **same direction
on the same day**, stacking individually-small risks into one shock. `stress_book.py`
reports max same-direction alignment, worst book-day conditional on heavy alignment,
and days breaching 3%/5%. Its figures are **in-sample and close-to-close** — live tails
run worse and it understates intraday floating lows, which is what a prop firm
actually measures.

```bash
./venv/bin/python sleeve_health.py 2>&1 | tail -40   # HEALTHY / REVIEW / RETIRE
./venv/bin/python decay_scan.py 2>&1 | tail -40      # live+DECAYED / retired+OK
```

`decay_scan.py` ends its window at `evaluate_strategy.FULL_END` (last completed
session), never a pinned date — a hardcoded end date silently scores current runs on
stale data and has flipped a verdict from fail to marginal-pass.

## Account-size caveat

All of the above is for the **$100k challenge account**. The active FIX account is
roughly $2,500: most sleeves fail the min-volume / risk floor there and simply don't
trade. Never carry $100k settings onto the small account.
