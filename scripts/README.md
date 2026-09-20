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

## Current config — do not scale up

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
