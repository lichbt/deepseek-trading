# montecarlo — will the book survive the prop rules?

Target: The5ers $100k — **3% daily DD**, **10% total DD**, **+10% profit**. One daily
breach is an instant DQ, so the daily figure is the binding constraint, not the total.

## Only one sanctioned path

```bash
source ~/.zshrc

# 1. Real-sized book sim — actual _compute_position_size, Kelly, decay, min-lot clamps
./venv/bin/python oanda_book_simulator.py \
    --start 2024-01-01 --end "$(date +%F)" \
    --risk 0.005 --max-risk 0.02 \
    --csv /tmp/book.csv 2>&1 | tail -30

# 2. Block-bootstrap those real daily returns against the prop rules
./venv/bin/python scripts/prop_realsim_mc.py /tmp/book.csv 2>&1 | tail -40
```

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

**Conclusion, already decided: keep base 0.005. Do not scale up.** The book passes at
base sizing. If asked to go faster, the honest answer is that the daily margin does
not support it — not that a multiplier exists.

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
