# Pre-registration — thesis chain-head A/B

**Committed before any experimental batch ran.** The commit sha of this file is the
tamper-evidence: `scripts/ab_analyse.py` reads the machine-readable block below rather
than hardcoding any of it, and refuses to run if the block is missing or unparseable.

This document states one claim, one way to measure it, and one rule for acting on it.
There are no alternatives listed on purpose — an analysis with options is an analysis
with a degree of freedom.

## The question

Does `byteplus:deepseek-v4-pro` beat `byteplus:deepseek-v4-flash-ga-260731` as the head
of `THESIS_MODELS`, measured by the rate at which its candidates survive walk-forward?

**Prior expectation is NO DIFFERENCE.** The same matchup ran at n=32/arm on 2026-08-04
(validity 30/32 vs 28/32, "inside noise"; latency 51s vs 123s). This is that question
properly powered, on a harder endpoint, against the newer GA build.

## Arms

| arm | model |
|---|---|
| control | `byteplus:deepseek-v4-flash-ga-260731` |
| challenger | `byteplus:deepseek-v4-pro` |

Batches alternate — even = control, odd = challenger — and consecutive batches form a
twin sharing one seed, so both arms see an identical instrument/constraint/timeframe
schedule and schedule variance cancels.

## Endpoints

**Primary — WF non-zero rate**: the fraction of an arm's candidates with
`validation_results.walk_forward_gt_score != 0`. Baseline 12.9% (measured over n=82,651
historical rows). A NULL walk-forward score is a FAILURE, not a missing value: it means
the candidate never produced one.

**Secondary — rank test**: Mann-Whitney U on the non-zero tail of
`walk_forward_gt_score`, two-sided. Reported for interpretation; it does not decide
adoption.

**Secondary — IS non-zero rate**: same construction on `is_gt_score`. `-inf` counts as a
FAILURE and is never dropped — dropping it would score an arm on the subset where it
already succeeded.

**Not an endpoint: pass-at-validation.** Ruled out on arithmetic — detecting a doubling
of a 0.224% rate needs 10,476/arm, about 34 days per arm at this throughput.

## Sample size, and why it is coupled to the decision rule

**n = 147 per arm.** α = 0.05 two-sided, 80% power, to detect a **+100% relative** change
in the primary (12.9% → 25.8%).

That n is only valid BECAUSE the decision rule is asymmetric. The challenger runs ~1.8×
slower per candidate, so it has to win decisively to be worth adopting; a rule of "adopt
on any improvement" would need to detect +50%, which is **510 per arm**. The analysis
script recomputes the required n from the effect size and rule in the block below and
**aborts if they disagree** — so the two cannot drift apart by editing one of them.

## Stopping rule

Evaluate **once**, at n = 147 per arm. **No interim looks.** An interim look at an
uncorrected α is how a null result becomes a positive one.

## Decision rule (asymmetric)

- Challenger wins the primary at α=0.05 → adopt `deepseek-v4-pro`, edit `.env`.
- Tie, or challenger loses → keep `deepseek-v4-flash-ga-260731`, on its throughput
  advantage.

A verdict is recorded in the Second Brain **either way**.

## Exclusions

- A candidate with no sidecar row is **excluded and its count reported**. Never inferred
  from `created_at`.
- Rows written under `failed_closed` (the batch fell back to control under error) are
  reported separately and excluded from the primary.
- If the git sha changed mid-run the script **refuses to emit a verdict** and reports the
  split by sha instead. The research loop runs the working tree, so an edit mid-run
  changes the code generating the sample.

## Machine-readable

`scripts/ab_analyse.py` parses exactly this block. Editing it changes the analysis.

```json
{
  "chain": "thesis",
  "control": "byteplus:deepseek-v4-flash-ga-260731",
  "challenger": "byteplus:deepseek-v4-pro",
  "primary": {
    "metric": "wf_nonzero_rate",
    "column": "walk_forward_gt_score",
    "null_is_failure": true
  },
  "secondary": [
    {"metric": "rank_test", "column": "walk_forward_gt_score", "tail": "nonzero"},
    {"metric": "is_nonzero_rate", "column": "is_gt_score", "null_is_failure": true,
     "neg_inf_is_failure": true}
  ],
  "baseline_rate": 0.129,
  "relative_effect": 1.00,
  "alpha": 0.05,
  "power": 0.80,
  "n_per_arm": 147,
  "stopping_rule": "single_look_at_n",
  "decision_rule": "asymmetric_challenger_must_win",
  "exclusions": {
    "missing_sidecar": "exclude_and_report",
    "failed_closed": "exclude_and_report",
    "sha_changed_mid_run": "refuse_verdict"
  }
}
```
