# scripts/ — prop-challenge risk sizing

Monte Carlo tools for sizing the book into a prop-firm challenge ($100k account,
3% daily DD, 10% total DD). Both reconstruct the **current live book** via
`portfolio.load_strategies()`, so re-running picks up any strategies added since.

`scale` in every table is a **global multiplier on current position sizing**
(1.0× = live sizing; apply it as `FIX_RISK × scale`, or multiply the
`portfolio.CONVICTION` map, subject to cluster caps).

| Script | Answers | Output |
|---|---|---|
| `prop_daily_breach_mc.py` | Probability a scaled book breaches the **3% daily** limit | breach % + worst-day percentiles per scale/horizon |
| `prop_pass_curve_mc.py` | Odds of clearing **+10%** before any breach, and how fast | pass/daily/total/timeout % + days-to-pass per scale |

## Run

```bash
python scripts/prop_daily_breach_mc.py
python scripts/prop_pass_curve_mc.py
```

(They self-insert the repo root on `sys.path`, so cwd doesn't matter.)

## Last result — 2026-07-24, 32-strategy book

Base worst 1-day loss at 1.0× = **−0.708% (−$708)**. Daily-3% breach is **0.00%
at every scale up to 4.0×**; first appears at 4.5×.

- **Safe: 2.5×** (worst day −1.77%) — passes ~98.5% with no time limit
- **Best 60-day attempt: 3.5×** (~31% pass in 60d, 0% daily breach)
- **Hard cap: 4.24×** — do not exceed 4.0× in practice

Recompute whenever the book changes. Interpreted result also lives in the
`prop-challenge-dd-sizing` memory and the Second Brain.
