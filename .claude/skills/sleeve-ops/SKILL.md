---
name: sleeve-ops
description: Evaluate a candidate strategy, Monte-Carlo the book against prop-firm rules, or deploy/retire a sleeve in deepseek-oanda-trading. Use when the user pastes a strategy id ("check this strategy <id>"), asks whether a sleeve should be deployed/retired/swapped, asks about prop-challenge sizing or drawdown odds, or asks to publish the book to Zeabur. Covers evaluate / montecarlo / deploy modes.
---

# sleeve-ops

Three modes over one pipeline: **evaluate → montecarlo → deploy**. Read only the
reference for the mode you're in.

| Ask | Mode | Reference |
|---|---|---|
| "check this strategy `<id>`", swap-or-add, retire-or-keep | evaluate | `references/evaluate.md` |
| prop-challenge sizing, DD odds, "can we scale up", pass probability | montecarlo | `references/montecarlo.md` |
| "deploy it", "retire `<id>`", "push to Zeabur" | deploy | `references/deploy.md` |
| change broker/venue, move hosts, "switch to FIX/cTrader" | migrate | `references/deploy.md` §D |

A full candidate review normally runs all three: evaluate the candidate → if it
passes, re-run montecarlo because the sleeve count changed → then deploy.

## Invariants (all modes)

- **`./venv/bin/python`**, never bare `python3`. Run from the repo root.
- **`source ~/.zshrc` first** for anything that fetches candles — OANDA creds live
  there, not in the environment. Without them `get_candles_date_range` silently
  returns partial data and sleeves drop out of the rebalance.
- **Cap output**: `... 2>&1 | tail -60`, or redirect to a temp file and grep.
  These scripts print hundreds of lines and every one is re-billed on later turns.
  Strip library noise with `grep -viE 'NotOpenSSL|Injected|Fetching|SettingWith'`.
- **Never deploy or retire without an explicit "yes"** naming the sleeve. Evaluation
  and Monte Carlo are read-only and need no confirmation; deploy risks real money.
- **Never commit the working `pipeline.db`** (273 MB). See `references/deploy.md`.
- **Reconstruct, don't assert.** Every claim about a strategy's behaviour comes from
  a run you did in this session. Two reconstruction bugs have each produced a wrong
  deploy decision — `references/evaluate.md` has both.

## Numbers that go stale — recompute, never quote

These change whenever the book changes. If you need one, run the command; do not
carry the value in from memory or from this file.

| Quantity | Command |
|---|---|
| Deployed risk (the book's true magnitude) | `./venv/bin/python -c "import json;print(sum(json.load(open('portfolio_state.json'))['weights'].values()))"` |
| Worst day / total DD / daily margin | montecarlo mode, real-sized path |
| Sleeve count and cluster caps | `sqlite3 pipeline.db "select count(*) from strategies where status='paper_trading'"` |

**Sleeve count N is a magnitude lever, not just a redistribution knob.**
`portfolio._apply_cluster_caps` uses `cap_frac = CLUSTER_CAP/n` and does **not**
renormalise — risk freed by a binding cap is dropped, not redistributed. So adding
a sleeve to a capped cluster *shrinks the whole book*, and removing sleeves inflates
every remaining position. Any deploy or retire must re-check deployed risk above;
if it moved, rescale base `RISK` to hold it constant. (Measured 2026-07-25: going
from 25 sleeves to 7 "best per cluster" would have silently multiplied position
sizes ~2.6× with `RISK` untouched.)

## Binding config — do not relitigate

**Magnitude is `BASE_RISK × BOOK_SCALE`** — `fix_runner.py:51` reads
`os.getenv('BASE_RISK')`, `:69` reads `os.getenv('BOOK_SCALE')` (default 1.0), and
`:70` sizes every trade at `EFF_RISK = RISK * BOOK_SCALE`; the pod prints the pair on
startup as `RISK/trade=… x BOOK_SCALE … = EFFECTIVE …`. `BOOK_SCALE` is the book's heat
multiplier (`tests/test_book_scale.py`), and the sanctioned use is compensating a
weight-vector change without touching the measured `BASE_RISK` ladder — used
2026-10-12 at **0.9511** to hold magnitude after the hk33hkd_i26 retire raised deployed
risk 0.4524 -> 0.4756. Two names that look like the knob are not it:
`FIX_RISK` is RETIRED (the alias was removed once the pod env moved to `BASE_RISK`;
see `fix_runner.py:38-46`), and `RISK_PER_TRADE` is read only by
`scripts/book_watch.py`. This block used to name both as the risk setting; it was
wrong, and the mistake is the kind that sizes a book at a magnitude nobody chose.

**`.env` says `BASE_RISK=0.005`, the pod says `BASE_RISK=0.007`, and that gap is
DELIBERATE — do not sync them** (user-confirmed 2026-09-07, after the divergence was
flagged). The OANDA live test is retired, so nothing trades off `.env`'s value any
more; the pod's **0.007** is the only base risk that reaches real money. `.env` is
vestigial here and syncing it would change nothing except to hide that.

The one place it still bites: anything run LOCALLY that reads `.env` rather than
taking `--risk` on the command line models a book 40% smaller than the pod. The sim
harnesses all take `--risk` explicitly — pass the pod's 0.007 for any prop figure,
and say which value you used. `FIX_MAXRISK` = **0.02** agrees everywhere. `CLUSTER_CAP` = **3.0** in `.env` and **3** on the pod (`portfolio.py:648`
defaults to 3.0); the **2** this block used to state was an older The5ers deploy value
and matches neither today.

**Do not scale up.** The daily-DD margin is thin and a multiplier is the fastest way
to an instant DQ. The retracted "3.5×" recommendation came from a Monte Carlo that
understated the book ~8× — `references/montecarlo.md` explains which scripts carry
that flaw. Measured at the pod's live 0.007 (2026-09-07, 22 sleeves, full carry
policy, guard on): worst day close **-1.38%**, intraday co-timed **-2.17%**, 0 days
past the wall. The -3% wall holds, but the intraday margin is **0.83 pp**.

`CLUSTER_CAP` is read from `.env` at `portfolio.py` load, but `fix_runner` reads the
**baked weights** in `portfolio_state.json` — changing the cap does nothing until you
regenerate that file.

**POD BASE_RISK IS 0.0145 AS OF 2026-10-06** (was 0.007; notes above this line
predate the change). Applied via `kubectl set env` on the k3s deployment, so it is
NOT in the Zeabur dashboard — a redeploy reverts it to the dashboard value. Evidence,
same curve/harness as `scripts/README.md`: 0.007 -> 0.0145 moves median days to
funded 624 -> 274, p95 1348 -> 733, total-DQ 0.20% -> 4.50%, pass 99.70% -> 93.47%.
0.0145 is the knee of the flat ladder (0.0150 is dominated: 8 days faster, worse pass
AND worse DQ). From 0.0145 the daily tail is past the 3% wall, so the 2.40% halt is
load-bearing under guard misses and it has never fired in production. 0.013 is the
last breaker-independent rung (worst close -2.98%).

**BOTH sizing knobs live ONLY in the k3s deployment spec** (`kubectl set env`), not in
the Zeabur dashboard and not in `.env` — and a rollout reconciles the spec back to the
dashboard value, so a plain redeploy can silently halve `BASE_RISK` (0.0145 -> 0.007)
or drop `BOOK_SCALE` to 1.0 (+5% on every position, 2026-10-12). Current pair:
**`BASE_RISK=0.0145`, `BOOK_SCALE=0.9511`**. `scripts/deploy_book.sh` snapshots both
before the push and **FAILS the run if either changed across it**; check by hand with
`./scripts/zeabur_interlock.sh risk | grep -E 'BASE_RISK|BOOK_SCALE'`. Any env-only
change: `interlock on` -> 0 pods -> `kubectl set env` -> `interlock off`, which is what
resumes trading (it deletes the quota **and** scales the deployment back to 1). It did
not always: until 2026-10-12 `off` only released the quota, and because a push — not
`off` — was what reset `spec.replicas=1`, an env-only change left the prop book at 0
pods for ~40 minutes with nothing reporting it. Confirm `FIX_RUNNER_PROCS=1` and a
fresh `ok:true` receipt after any release.

## Repo map

| File | Role |
|---|---|
| `evaluate_strategy.py` | the one-command review lens |
| `causal_audit.py` | causal-return-collapse (severity ranking for look-ahead) |
| `final_holdout.py` | the locked holdout — run once, for one winner |
| `sleeve_health.py` | live-sleeve grading (`--rank` ranks candidates vs book) |
| `decay_scan.py` | book-wide RECENT30 decay: retire and restore candidates |
| `stress_book.py` | correlated-drawdown / same-direction alignment gate |
| `oanda_book_simulator.py` | real-sized book sim — the only trustworthy return curve |
| `scripts/prop_realsim_mc.py` | prop-rule Monte Carlo over that curve |
| `scripts/build_deploy_db.py` | compact deployment DB builder |
| `portfolio.py --write` / `run_portfolio.sh` | regenerate `portfolio_state.json` |
| `pipeline_utils.py` | `start_live_trading`, `retire_strategy`, `get_strategy_by_id` |
