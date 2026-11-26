# Pre-registration — novelty-AVOID prompt block A/B

**Question.** Does injecting the recent-near-duplicate AVOID list into the batch thesis
prompt raise the fraction of candidates that survive walk-forward?

**Prior expectation: NO DIFFERENCE.** The block is a soft hint (about 90 prompt tokens,
six truncated rationales). It was added 2026-10-01 to attack a 74 percent near-duplicate
rate, and the near-duplicate rate did fall (74 to 40 percent), but that fall coincided with
the regenerate-on-block feature, so the AVOID block alone has never been measured. Both
rewrite arms (defect repair, novelty re-roll) have since failed their randomized controls,
which makes an unmeasured prompt hint a standing YAGNI violation.

**Arms.** Control = block withheld. Challenger = block injected (current production).
Batches alternate: even control, odd challenger; consecutive batches form a twin sharing
one seed, so both arms see the identical instrument/constraint/timeframe schedule.

**Primary — WF non-zero rate.** Fraction of candidates with
`validation_results.walk_forward_gt_score` non-zero. Baseline 0.155 (measured over n=6409
rows, 2026-10-05 onward; 0.164 prior month, 0.141 all history). A NULL WF score is a
FAILURE, not a missing value: the candidate never produced one.

**Secondary — IS non-zero rate.** Fraction with `is_gt_score` non-zero, non-finite and
-inf excluded. Measured descriptive, no decision weight.

**Sample size.** n = 408 per arm. alpha = 0.05 two-sided, 80 percent power, detect a
+50 percent relative change (0.155 to 0.232). The analysis script recomputes this from the
block below and aborts on disagreement, so the two cannot drift apart.

**Stopping rule.** Evaluate ONCE at n = 408 per arm. No interim looks.

**Decision rule.** Challenger wins primary at alpha=0.05: keep the AVOID block on
(`JEV_NOVELTY_FEEDBACK=1`). Tie or loss: set `JEV_NOVELTY_FEEDBACK=0`, because an
unproven prompt block should not persist. That is the asymmetry here: the challenger must
win, since the control is the simpler system.

**Exclusions.** Candidate with no sidecar row is excluded and counted, never inferred from
`created_at`. Rows from a failed-closed batch (fell back to control on error) are excluded
and reported. If `gen_sha` (the generation-path content hash) moves mid-run the script
refuses a verdict and reports the split instead. 

**Directive freeze.** The harness freezes meta-review directive rewrites while this A/B is
live, so `thesis.md` cannot be rewritten mid-run. That is the defect recorded in the
2026-08-11 amendment of the thesis chain-head pre-registration: a mid-run directive rewrite
changed the prompt for later batches while `git_sha` reported unchanged.

## Machine-readable

```json
{
  "chain": "novelty-avoid",
  "control": "avoid-off",
  "challenger": "avoid-on",
  "primary": {
    "metric": "wf_nonzero_rate",
    "column": "walk_forward_gt_score",
    "null_is_failure": true
  },
  "secondary": [
    {"metric": "is_nonzero_rate", "column": "is_gt_score", "null_is_failure": true,
     "neg_inf_is_failure": true}
  ],
  "baseline_rate": 0.155,
  "relative_effect": 0.50,
  "alpha": 0.05,
  "power": 0.80,
  "n_per_arm": 408,
  "stopping_rule": "single_look_at_n",
  "decision_rule": "asymmetric_challenger_must_win",
  "exclusions": {
    "missing_sidecar": "exclude_and_report",
    "failed_closed": "exclude_and_report",
    "sha_changed_mid_run": "refuse_if_generation_path_changed",
    "gen_sha_changed_mid_run": "refuse_verdict"
  },
  "generation_paths": [
    "auto_research.py", "validator.py", "thesis.md", "fingerprint.py", "steering.py",
    "reason_codes.py", "strategy_honesty.py", "data_fetcher.py", "macro_fetcher.py",
    "fred_events.py", "supplementary_data.py", "jev_client.py", "jev_candidates.py"
  ]
}
```
