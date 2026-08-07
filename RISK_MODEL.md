# Prop risk model — measurement and recommendation

Built 2026-08-07 on branch `research/prop-risk-model`. Target: The5ers $100k two-step
(+10% then +5%), 3% daily DD, $10,000 static max loss re-basing each step, unlimited
time, 50% consistency. Objective: **minimum time to funded**, not survival.

Reproduce everything here with:

```bash
./venv/bin/python scripts/risk_model_sim.py --check-baseline      # C1
./venv/bin/python scripts/risk_model_sweep.py --out /tmp/frontier.csv
```

---

## The recommendation

**Set `BASE_RISK` 0.005 → 0.0080. Leave `PROP_HALT_FRACTION` at 0.80. Enable no
account-aware components.**

| | now | recommended |
|---|---|---|
| `BASE_RISK` | 0.005 | **0.0080** |
| worst day, close-to-close | −1.56% | −2.44% |
| **worst day, intraday** | **−1.85%** | **−2.88%** |
| median days to funded | 494 | **298** |
| DQ probability | 0.04% | 1.10% |
| depends on the breaker firing? | no | **no** |

That is **~196 days saved, about 40% faster**, and it is the *last rung that does not
depend on the guard*. Everything above it does, sharply.

### Why 0.0080 and not higher

0.0080 is the highest level whose **intraday** worst day (−2.883%) stays inside the
3% wall. One rung up, 0.0085 reaches −3.030% — a wall breach on the historical worst
day by itself, with no bad luck required.

This distinction only exists because the intraday low is now measured. Every prior
number in this repo was close-to-close, which reports 0.0085 as −2.563% and therefore
safe. It is not; the firm judges the floating low "at any point during the day".

The guard-mode sweep confirms where the cliff is:

| `BASE_RISK` | DQ, guard works | DQ, guard misses 25% | DQ, **no guard** |
|---|---|---|---|
| 0.0080 | 1.10% | 1.10% | **1.10%** |
| 0.0090 | 1.92% | 1.92% | **1.92%** |
| 0.0100 | 2.32% | 12.64% | **36.08%** |
| 0.0120 | 3.46% | 12.14% | **33.34%** |

At and below 0.0090 the guard is irrelevant — the tail never reaches the wall, so all
three columns agree. At 0.0100 survival becomes almost entirely a bet on the breaker.
That bet is currently unbacked: **the guard has never fired in this book's history**
(zero days past the halt line at current sizing), and the `halt-set` dry run proving
the flatten path end to end is still outstanding.

### If you want to go faster than 0.0080

0.0090–0.0095 buys another 30–44 days (268 / 254) at ~1.9–2.2% DQ, and is still
guard-independent. **Do this only after the `halt-set` dry run lands**, not because
the guard is needed at that level, but because 0.0095 sits one rung from the cliff and
you do not want the breaker's first ever real firing to be the one that matters.

Do **not** go to 0.010+ until the guard has demonstrated a real flatten.

### Do not raise `PROP_HALT_FRACTION`

0.90 is actively dangerous: halting at −2.70% plus the modelled 0.4pp slip means being
caught at −3.10%, *past* the wall. Measured DQ at 0.010 goes 2.32% → **36.08%**. The
guard fires too late to save anything. 0.80 is correct; 0.70 is marginally better only
above 0.010 (2.86% vs 3.46% at 0.012) and identical at every level below.

---

## What the components were worth

The account-aware model was the point of the exercise. Measured, almost all of it is
either untestable on this data or dominated by simply moving the scalar.

| | verdict | evidence |
|---|---|---|
| **M1 scalar** | **adopt** | the only real lever; see above |
| M2 realised-loss throttle | **untestable** | not rejected — see below |
| M3 cushion ramp | **reject** | dominated |
| M4 endgame de-risk | marginal | 389 days @ 0.06% DQ vs plain 0.006 at 402 days @ 0.04% |
| M5 consistency governor | conditional | the one keeper, see below |
| M6 cluster cap | **reject** | a substitute for the scalar, not a complement |

**M2 is unmeasurable, not wrong.** Sizing happens once per bar, at the open, when
`day_low == day_base` — so `daily_budget_used` is identically 0 at the only moment the
model is consulted, `m_throttle` is identically 1.0, and `admit_open` cannot refuse
anything because `MAXRISK = 0.02` is already below the 2.4% budget. Phase B confirms
it: `throttle` and `budget_gate` reproduce `none` to the digit. Testing an intraday
throttle needs intraday bars, which this repo does not have for the book. Shipped off,
flagged untested.

**M3 is dominated, cleanly.** Ramp at 0.0075 gives 233 days at 1.34% DQ and an
intraday worst of −4.30%. Plain `BASE_RISK=0.011` gives **218 days at 0.82% DQ** and
−4.03%. Faster, safer, better tail. The cushion ramp is just leverage with extra
steps — unsurprising in hindsight, because total DD was never the binding constraint
(maxDD −4.39% against a 10% floor), so scaling on cushion scales on a slack variable.

**M5 is the one component worth keeping.** At 0.0075 it costs 103 days but takes DQ to
0.00% *and improves the intraday tail* (−2.686% → −2.170%), because throttling after a
record day suppresses exactly the days that produce the tail. Against plain 0.005 it
is **57 days faster at equal DQ**. It is off in the recommendation because at 0.0080
the tail is already inside the wall, but it is the right lever if you later want a
lower-tail variant without giving back all the speed.

**M6 is a substitute, not a complement.** `cluster_cap=1.5 @ 0.010` gives 293 days /
−3.009% intraday; `cluster_cap=2.0 @ 0.0085` gives 288 days / −3.030%. Same book, same
place on the frontier. Tightening the cap buys no diversification benefit — it just
dials magnitude, slightly worse than the scalar does.

---

## Findings that contradict the record

Three standing claims did not survive measurement.

**1. "0.0075 sits BELOW the halt level, so it reaches 334 days with NO guard
dependency."** (`montecarlo.md`) This is a close-to-close artifact. On the intraday
low, 0.0075 is −2.686% and 0.0070 already crosses the −2.40% halt line once. The
guard-independence claim is right for a different reason than stated — the tail stays
inside the *wall*, not inside the *halt line*.

**2. "~20.7% of book days open after a >1-day calendar gap ... a Sunday open past the
halt is uncatchable."** The gap count reproduces exactly (139 days, 20.6%), but the
tail is not there:

| | worst day | mean |
|---|---|---|
| follows a calendar gap (n=139) | **−0.582%** | +0.017% |
| no gap (n=535) | **−1.558%** | +0.032% |

Every dangerous day in 2.5 years was an ordinary intraday move on a normal session —
precisely what a 5-minute sampled breaker can catch. The gap caveat is real in
principle and empirically weak on this book.

**3. Consistency does not bind.** Expected to be a raised bar that delays approval;
measured delay is **0 days at every risk level**. Required profit from the best-day
ratio runs $2.1k–$5.1k against a $10k target. The 50% cap is simply not close.

---

## Caveats that survive

**The MC breaches on close-to-close returns; the firm breaches on the intraday low.**
Every DQ figure in `frontier.csv` is therefore optimistic. The intraday column is the
honest tail measure and is what the recommendation was sized against — that is why the
recommendation is 0.0080 and not the 0.0095 the frontier's own DQ column would allow.

**The ceiling rests on one day.** The worst day (−1.558%, 2024-06-06) is 1.67× the
second worst (−0.931%); p0.1 is −1.136%. This repo's own rule says a worst-day figure
resting on 1 vs 2 events is a single-day artifact. Every number scaled from it inherits
that fragility, and a block bootstrap **cannot** produce a day worse than the one
observed. A 0.00% breach rate means "no resampled day exceeded the observed worst".

**Regime structure changes the risk, not the speed.** This book's returns are
non-stationary in the mean (+3.10% over the first 300 bars, +10.33% in the next 100)
with ~zero daily autocorrelation. Preserving more of that structure (`block=60` vs
`block=10`) barely moves median days (298 vs 292 at 0.0080) but **roughly triples DQ**
(1.10% vs 0.12%). All headline figures here use `block=60`, the conservative choice.
An earlier reading of this — that the bootstrap understates *time* — was wrong; it
understates *DQ risk*.

**The guard is not free, and now it is measured.** On the real path, `montecarlo.md`'s
"cost is not modelled anywhere" is now modelled:

| `BASE_RISK` | halts over 674 bars | terminal equity cost |
|---|---|---|
| 0.0075 | 1 | −0.39% |
| 0.0100 | 2 | −1.84% |
| 0.0120 | 6 | **−5.05%** |

This cost is *not* inside the frontier's day-counts, so real days at high risk are
worse than the table says — another reason the cliff above 0.010 is steeper than it
looks.

**Smaller ones.** `paths=5000` (not 20000), so a 1% DQ has SE ≈0.14pp — rank on these,
do not quote two decimals. `CLUSTER_CAP` upward (2.5, 3.0) was **not measured**: the
cap does not renormalise, so pre-cap weights are unrecoverable and rebuilding them
needs `portfolio.py main()`, which writes the live `portfolio_state.json`. Dropped
axis, not an omission.

---

## Cutover

The headline recommendation needs **no code change**. `prop_risk_model.py` is only
required if a component is enabled, and none are recommended.

```bash
# Zeabur pod env: BASE_RISK 0.005 -> 0.008
./scripts/zeabur_interlock.sh on          # confirm 0 pods before ANY push
# ... set BASE_RISK=0.008 in the Zeabur dashboard (a REAL BUILD is required —
#     dashboard env vars do not apply on a restart alone)
./scripts/zeabur_interlock.sh off
./scripts/zeabur_interlock.sh risk        # verify BASE_RISK=0.008 on the pod
```

Then re-run, because every figure here moves with the sleeve count:

```bash
./venv/bin/python stress_book.py
./venv/bin/python scripts/risk_model_sweep.py --out /tmp/frontier.csv
```

`FIX_MAXRISK` stays 0.02 — it is a per-trade ceiling that rarely binds and lowering it
is measured to do nothing. `CLUSTER_CAP` stays 2.

**This is a trading action.** Raising base risk 60% raises every position's size on the
next pass.
