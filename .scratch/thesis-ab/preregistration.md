# Pre-registration — thesis chain-head A/B

**AMENDED 2026-08-11, before any experimental batch ran** — see *Amendment* at the foot.
The original sha was `37a325e`; this supersedes it.

**Committed before any experimental batch ran.** The commit sha of this file is the
tamper-evidence: `scripts/ab_analyse.py` reads the machine-readable block below rather
than hardcoding any of it, and refuses to run if the block is missing or unparseable.

This document states one claim, one way to measure it, and one rule for acting on it.
There are no alternatives listed on purpose — an analysis with options is an analysis
with a degree of freedom.

## The question

Does `byteplus:deepseek-v4-pro` beat `byteplus:deepseek-v4-flash` as the head
of `THESIS_MODELS`, measured by the rate at which its candidates survive walk-forward?

**Prior expectation is NO DIFFERENCE.** The same matchup ran at n=32/arm on 2026-08-04
(validity 30/32 vs 28/32, "inside noise"; latency 51s vs 123s). This is that question
properly powered, on a harder endpoint. (The charting note said "against the newer GA
build" — that is withdrawn by the Amendment below: the coding endpoint does not honour the
dated GA id.)

## Arms

| arm | model |
|---|---|
| control | `byteplus:deepseek-v4-flash` |
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
- Tie, or challenger loses → keep `deepseek-v4-flash`, on its throughput
  advantage.

A verdict is recorded in the Second Brain **either way**.

## Exclusions

- A candidate with no sidecar row is **excluded and its count reported**. Never inferred
  from `created_at`.
- Rows written under `failed_closed` (the batch fell back to control under error) are
  reported separately and excluded from the primary.
- If the **generation-path content hash** (`gen_sha`, over `generation_paths`) changed
  mid-run the script **refuses to emit a verdict** and reports the split instead. The
  research loop runs the working tree — and rewrites part of it, see the second
  Amendment — so an edit mid-run changes the code generating the sample.
- If `git_sha` changed but the diff touches **no** `generation_paths` file, the split is
  reported and the analysis proceeds: the trading side of this repo commits constantly and
  cannot reach a candidate.

## Machine-readable

`scripts/ab_analyse.py` parses exactly this block. Editing it changes the analysis.

```json
{
  "chain": "thesis",
  "control": "byteplus:deepseek-v4-flash",
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
    "sha_changed_mid_run": "refuse_if_generation_path_changed",
    "gen_sha_changed_mid_run": "refuse_verdict"
  },
  "generation_paths": [
    "auto_research.py", "validator.py", "thesis.md", "fingerprint.py", "steering.py",
    "reason_codes.py", "strategy_honesty.py", "data_fetcher.py", "macro_fetcher.py",
    "fred_events.py", "supplementary_data.py"
  ]
}
```

## Amendment — 2026-08-11, before any experimental data existed

**The control arm was renamed from `deepseek-v4-flash-ga-260731` to `deepseek-v4-flash`,
because that is what the endpoint actually serves.** Nothing else changed: same endpoint,
same n, same rule, same endpoints.

The dry run (ticket 03) added a wire-level check — log the model the gateway *echoes*, not
the one the config *requests* — and it fired on the first call:

```
[A/B] served 'deepseek-v4-flash' for requested 'deepseek-v4-flash-ga-260731'
```

Probed directly, the two byteplus endpoints disagree:

```
base https://ark.ap-southeast.bytepluses.com/api/v3          (NOT the one the loop uses)
  deepseek-v4-flash-ga-260731   200  echoed 'deepseek-v4-flash-ga-260731'
  deepseek-v4-pro               404
base https://ark.ap-southeast.bytepluses.com/api/coding/v3   (the loop's BYTEPLUS_BASE)
  deepseek-v4-pro               200  echoed 'deepseek-v4-pro'
  deepseek-v4-flash-ga-260731   200  echoed 'deepseek-v4-flash'
  deepseek-v4-flash             200  echoed 'deepseek-v4-flash'
```

The gateway echoes a dated id verbatim when it honours it, so the coding endpoint is not
honouring `-ga-260731` — it is an alias for generic flash there. The challenger id is
honoured exactly, and only exists on the coding endpoint.

**Consequence beyond this experiment:** any claim that the research loop runs the "GA
build" via `BYTEPLUS_BASE_URL=.../api/coding/v3` is unfounded. Whether the coding
endpoint's `deepseek-v4-flash` is the same weights as `/api/v3`'s dated deployment is NOT
established here — only that the id is not being honoured. `CLAUDE.md`'s model-rank row
and the 2026-08-10 decision that recorded the GA build as activated both need re-checking.

This is an amendment rather than a silent edit because the data does not exist yet: no
experimental batch has been analysed, the dry-run sidecar is excluded by construction, and
the change makes the label match the wire rather than changing what is being compared.

## Amendment — 2026-08-11, AFTER an interim look. Read the honesty note.

**Disclosure first: this amendment was written after seeing interim results.** At the time
of writing, 64 control / 50 challenger rows had been analysed and the challenger was
LOSING the primary (31.2% vs 10.0%, p=0.0065). That is a peek at an uncorrected alpha,
it was not authorised by the stopping rule, and it happened. Nothing below changes the
primary metric, the arms, the alpha, the n, or the decision rule — the comparison is
untouched. What changes is only the PROVENANCE guard, in a direction that makes it
STRICTER overall, and the reasoning is stated here so it can be judged rather than
trusted.

**What went wrong with the old guard.** It refused a verdict whenever `git_sha` moved
mid-run. Two failures, in opposite directions:

1. **Too strict.** The sha moved three times during the run (`912143a`, `dc91dc7`,
   `61ad993`) and every one of those commits was carry-policy work — `fix_runner.py`,
   `oanda_book_simulator.py`, `scripts/risk_model_sim.py`, `zeabur_interlock.sh` and two
   test files. None of it can touch a candidate. Under the old rule this run could never
   have produced a verdict at any n, because the trading side of the repo is under active
   development and will keep committing.
2. **Too loose, and this is the serious one.** `meta_review.run_meta_review()` is called
   BY the research loop and REWRITES the `<!-- RESEARCH_PHASE -->` directive block inside
   `thesis.md` — uncommitted. It fired at 13:07 on 2026-08-11, between batch 6 and batch
   7, changing the directives from "generate more cross-market/event, less volatility;
   tighter risk controls for DD-blocked; max 2 indicators" to "...; prioritize
   macro-family designs; convert DD-blocked edge via ATR stops and vol-scaled sizing".
   Every batch from 7 on is prompted differently from batches 0-6. `git_sha` reported
   this as unchanged, because an uncommitted edit does not move HEAD. **The guard was
   blind to the only mid-run change that actually altered the experiment.**

**The replacement.** Provenance is now a content hash (`gen_sha`) over the generation
path listed in `generation_paths` above — the loop, the prompt it splices, the steering
that picks the slot, and the validator that produces the endpoint. `auto_research.py`
stamps the digest on every sidecar row and the per-file map on every ledger row, so a
split is localisable to the file that moved. `gen_sha` changing mid-run refuses a verdict.
`git_sha` changing now refuses ONLY if the diff between the shas touches a
`generation_paths` file; otherwise it is reported and the analysis proceeds.

**Consequence for the current run, decided now rather than at the end.** Batches 0-6
(114 rows) carry no `gen_sha` and were generated under directive text that batch 7 onward
does not share. They cannot be repaired retroactively. The choice is therefore between
analysing a sample that is a mixture of two prompt regimes, or restarting the counter and
paying for a clean one. **This pre-registration does not decide that** — it records that
the defect is known, and the analysis will report unverifiable rows as a distinct class
rather than silently folding them into the arms.
