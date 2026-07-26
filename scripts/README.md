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

`FIX_RISK` / `RISK_PER_TRADE` = **0.005**, `FIX_MAXRISK` = **0.02**,
`CLUSTER_CAP` = **2** (in `.env`). The book passes The5ers at base sizing; the
daily-DD margin is thin and a global multiplier is the fastest route to a DQ.

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
