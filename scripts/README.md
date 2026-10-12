# scripts/ — prop-challenge risk sizing + deployment

Tooling for sizing the book into a prop-firm challenge ($100k account, 3% daily DD,
10% total DD, +10% target) and for building the deployed database.

Full procedure lives in the **`sleeve-ops`** skill
(`.claude/skills/sleeve-ops/references/montecarlo.md` and `deploy.md`).

## Sizing — use the REAL-SIZED path

```bash
./venv/bin/python oanda_book_simulator.py --start 2024-01-01 --end "$(date +%F)" \
    --risk 0.005 --max-risk 0.02 --csv /tmp/book.csv
./venv/bin/python scripts/prop_realsim_mc.py /tmp/book.csv
```

`oanda_book_simulator.py` reproduces the live sizing model — `_compute_position_size`,
Kelly, decay, min-lot clamps — and `prop_realsim_mc.py` block-bootstraps that daily
equity curve against the prop rules.

### ⚠ `prop_daily_breach_mc.py` / `prop_pass_curve_mc.py` — NOT for sizing

Both reconstruct the book as `raw_return × portfolio_weight` instead of real
risk-budgeted position sizing, which **understates the book roughly 8×**. They
produced a "safe 2.5× / best 3.5×" recommendation that was **retracted on
2026-07-25**: on the real book that 3.5× turns the worst day into ≈−9.8%, an instant
DQ. Kept for the correlation-structure question they were written for. The `scale`
column in their output is meaningless for sizing decisions.

## Current config — pod BASE_RISK 0.0145 (sized 2026-10-06)

**SUPERSEDES the "do not scale up" block below.** Pod `BASE_RISK` = **0.0145**
(0.007 until 2026-10-06), `BOOK_SCALE` = **1.0**, `FIX_MAXRISK` = **0.02**, `CLUSTER_CAP` = **3**,
`WEIGHTING` = invvol. Sized on the 2020-08-01 -> 2026-10-06 real-sized cTrader curve
with the pod's carry policy, `scripts/prop_guarded_mc.py --total-mode static` (20k
paths):

| BASE_RISK | pass | DQ-total | median d | p75 d | p95 d | low-water med | low-water p99 |
|---|---|---|---|---|---|---|---|
| 0.007 | 99.70% | 0.20% | 624 | 863 | 1348 | -0.97% | -7.45% |
| 0.009 | 99.27% | 0.71% | 475 | 678 | 1106 | -1.23% | -9.32% |
| 0.010 | 98.80% | 1.20% | 424 | 621 | 1032 | -1.41% | -10.04% |
| 0.0115 | 98.00% | 2.00% | 364 | 539 | 913 | -1.59% | -10.20% |
| 0.0125 | 97.12% | 2.88% | 334 | 501 | 861 | -1.74% | -10.31% |
| 0.013 | 96.74% | 3.25% | 317 | 479 | 820 | -1.80% | -10.36% |
| **0.0145 (now)** | **93.47%** | **4.50%** | **274** | **417** | **733** | **-1.97%** | **-10.52%** |
| 0.015 | 95.08% | 4.92% | 266 | 410 | 718 | -2.03% | -10.57% |
| 0.016 | 94.31% | 5.70% | 247 | 380 | 678 | -2.19% | -10.68% |

0.0145 is the knee of the flat ladder: 0.0125 -> 0.0145 buys 58 median days for
3.7pp of pass (15.7 d/pp), while 0.0145 -> 0.0150 buys only 8 days for 0.19pp and is
worse on BOTH pass and DQ-total (93.28% / 4.77%), so 0.0150 is dominated. From 0.0145
up the daily tail is past the 3% wall, so the 2.40% halt is load-bearing (guard-miss
model: 2.0% DQ-daily) and the breaker has never fired in production. Tightening
PROP_HALT_FRACTION to 0.75/0.70 buys <=0.2pp of pass and <=2 days — DQ is driven by
total DD and by guard MISSES, not by halt timing, so 0.80 stays. Last rung that does
not need the breaker at all is 0.013 (worst close -2.98%). P95 days is the honest
"max time" column, not median.

Measured policy arms (risk_model_sim, 2020-08+ curve, same harness, leaky guard):
cushion ramp max 1.5 -> 234d median / 92.2% pass / 3.1% DQ-daily (needs live code:
fix_runner sizes statelessly today); ramp 1.5 + consistency -> 355d / 98.3% / 0.00%
(safety, not speed); realised-loss throttle and budget_gate are STRUCTURALLY INERT on
daily bars (entries fire at bar open, before the day's loss exists) — they cannot be
priced or operated at this granularity.

WHERE THE VALUE LIVES: it was applied with `kubectl set env` on the k3s deployment,
NOT saved in the Zeabur dashboard. A Zeabur redeploy re-applies the dashboard env
and silently reverts the book to the dashboard's value — re-check `interlock risk`
after every push.


## (historical) previous config — do not scale up

`BASE_RISK` = **0.005** (prop book) / `RISK_PER_TRADE` = **0.002** (paper book),
`BOOK_SCALE` = **1.0**, `FIX_MAXRISK` = **0.02**, `CLUSTER_CAP` = **2**. The book
passes The5ers at base sizing; the daily-DD margin is thin and a global multiplier
is the fastest route to a DQ.

`FIX_RISK` is RETIRED (2026-08-08) — `fix_runner` reads `BASE_RISK` only. Book
magnitude moves via `BOOK_SCALE`, which multiplies `BASE_RISK`; setting both
compounds, so read the `EFFECTIVE` figure the runner prints at startup.

Only base `RISK` sets magnitude — Kelly, conviction, and cluster weights merely
redistribute a cap-bound pie. Sleeve **count** is also a magnitude lever, because
`portfolio._apply_cluster_caps` does not renormalise the risk a binding cap frees.
After any book change, recompute deployed risk:

```bash
./venv/bin/python -c "import json;print(sum(json.load(open('portfolio_state.json'))['weights'].values()))"
```

Re-run the real-sized sim rather than quoting a stored worst-day number — every
figure here goes stale as the book changes.

## Deployment

```bash
./venv/bin/python scripts/build_deploy_db.py --out /tmp/deploy.db
```

Builds the compact deployment `pipeline.db` (paper_trading rows only, ~170 KB vs the
273 MB research DB) into a temp file, leaving the working DB untouched. Staging it
into the git index is a separate step — see `deploy.md`.

| Script | Answers |
|---|---|
| `prop_realsim_mc.py` | pass / daily-breach / total-breach odds on the **real-sized** curve |
| `build_deploy_db.py` | compact DB for Zeabur |
| `prop_daily_breach_mc.py` | (flawed sizing) daily-breach curve by scale |
| `prop_pass_curve_mc.py` | (flawed sizing) pass-odds curve by scale |

## Monitoring the prop pod

| Job | Cadence | Behaviour |
|---|---|---|
| `scripts/book_watch.py` (`com.lich.bookwatch`) | 4h | the book: bad days, stalled sleeves, guard + broker auth — **alert-only** |
| `scripts/prop_health.py` (`com.lich.prophealth`) | 4h | the POD: running, one runner, pass receipt, guard, cTrader auth, OANDA feed, DD vs limits — **messages every run, all-clear included** |

`prop_health.py` exists because a green pod is not a trading pod: the runner is
`RUNNER_MODE=cron` and places nothing on boot, so liveness has to come from the
`last_pass.json` receipt (`fix_runner._write_receipt`) and its age. Silence from
`com.lich.prophealth` for ~8h is itself the alarm — it means the checker stopped,
not that the pod is healthy.

**Execution and data are different dependencies and are reported separately.**
Only execution is cTrader; every signal, *and the live prices behind the software
stop check*, come from OANDA (`fix_runner.py:30`). So `ctrader (exec)` (no
rejection in the pod log) and `oanda (data)` (a candles request made INSIDE the
pod, with the pod's own token) are distinct lines — correct broker auth with a
dead feed still means no entries and blind stops. A Mac-side OANDA probe would
not be evidence: the pod has its own egress and credentials.

Guard and broker-auth verdicts are imported from `book_watch`, not re-derived, so
the two jobs cannot drift. All the pod reads go through one read-only
`zeabur_interlock.sh health-probe` round trip.

```bash
./venv/bin/python scripts/prop_health.py --dry-run    # print, send nothing
launchctl print gui/$(id -u)/com.lich.prophealth      # confirm it is loaded
tail -20 .paper-trading-logs/prop_health.log
```

## Keeping the books honest

| Job | Cadence | Behaviour |
|---|---|---|
| `scripts/revalidate_passed.py` (`com.lich.revalidate`) | 7d | re-scores every `passed` / `passed_but_fragile` row on CURRENT data, IS + walk-forward; **stops before the holdout**. Read-only by default; `--write-status` demotes stale passes to `failed_deployed` |
| `sleeve_health.py` (`com.lich.sleevehealth`) | 7d | rolling 3 / 6 / 12-month Sharpe for every live sleeve -> HEALTHY / REVIEW / RETIRE / LOW_DATA. Read-only, alert-only |

Different questions. `revalidate_passed` asks whether a stored VERDICT still holds; `sleeve_health` asks whether the sleeve is still making money, graded on ACTIVE BARS, so it also covers the 12 of 21 live sleeves that can never arm the RECENT30 decay check (30 entries inside 36 months). A RETIRE grade is a review trigger, not an order - retiring needs a fresh reconstruction and a human decision (see .claude/skills/sleeve-ops).

A pass is a claim about the candles AND the code that produced it, and both move
underneath it: the 2026-07-31 `data_fetcher` dedup fix left ~31% of the FAILED
pool no longer failing, and nothing re-ran the passed pool at all. The sweep is
what turns "still passing" into a measurement. A `CLEARS_WF` verdict is still
NOT a pass - IS, holdout, torture, drawdown and look-ahead gates all apply - so
anything it flags goes through `validator.py` once, by hand.

```bash
cp launchd/com.lich.revalidate.plist launchd/com.lich.sleevehealth.plist ~/Library/LaunchAgents/
launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.lich.revalidate.plist
launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.lich.sleevehealth.plist
launchctl print gui/$(id -u)/com.lich.revalidate      # confirm it loaded
launchctl print gui/$(id -u)/com.lich.sleevehealth    # confirm it loaded
```

Output lands in `.paper-trading-logs/revalidate_passed.log` and `.paper-trading-logs/sleeve_health.log`. The revalidate job also writes its full per-row results to `.paper-trading-logs/revalidate_passed.json`.
