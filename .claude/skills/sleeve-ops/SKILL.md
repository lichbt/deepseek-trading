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

`FIX_RISK` / `RISK_PER_TRADE` = **0.005**, `FIX_MAXRISK` = **0.02**,
`CLUSTER_CAP` = **2**, in `.env`. **Do not scale up.** The book already passes The5ers
at base sizing; the daily-DD margin is thin and a multiplier is the fastest way to an
instant DQ. The retracted "3.5× / FIX_RISK=0.0175" recommendation came from a Monte
Carlo that understated the book ~8× — `references/montecarlo.md` explains which
scripts carry that flaw.

`CLUSTER_CAP` is read from `.env` at `portfolio.py` load, but `fix_runner` reads the
**baked weights** in `portfolio_state.json` — changing the cap does nothing until you
regenerate that file.

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
