# Pre-registration and analysis script

Type: task
Status: resolved
Assignee: lich (claude session 2026-08-11)
Blocked by: 01

## Question

Write and commit — **in a single commit, before any experimental batch runs** — the
document that binds the claim, plus the script that computes it.

**The pre-registration** states, with no hedging or alternatives:

- Arms: control `byteplus:deepseek-v4-flash-ga-260731`, challenger `byteplus:deepseek-v4-pro`
- Primary endpoint: WF non-zero rate (`walk_forward_gt_score != 0`)
- Secondary: Mann-Whitney rank test on the non-zero tail; IS non-zero rate
  (with `-inf` counted as a FAILURE, never dropped)
- n = 147 per arm; α = .05 two-sided; 80% power to detect +100% relative
- Stopping rule: evaluate **once**, at n=147 per arm. No interim looks.
- Decision rule (asymmetric): pro must win the primary to be adopted; tie or loss keeps
  flash-ga on its ~1.8x throughput advantage.
- Exclusion policy: candidates missing a sidecar `strategy_id` are excluded and their
  count reported.

**The analysis script** reads the sidecar, joins to `validation_results` on
`strategy_id`, and emits the primary, the secondaries, the two arm counts, and the
excluded count.

Two properties that make the pre-registration binding rather than decorative:

- **The script READS the pre-registration** for n, endpoint and decision rule — it does
  not hardcode them. It aborts if the file is missing or unparseable. This couples the
  sample size to the decision rule mechanically: n=147 is only valid BECAUSE the rule is
  asymmetric (pro must win big to overcome its ~1.8x throughput penalty). Softening the
  rule to "adopt on any improvement" makes 147 underpowered — 510/arm would be needed for
  +50%. Editing one without the other must be impossible, not merely discouraged.
- **The script checks the provenance stamps** from 01 and refuses to emit a verdict if the
  git sha changed mid-run, reporting the split instead.

Blocked by 01 because the script reads the sidecar 01 defines.

## Definition of done

The script runs green against the **existing historical rows** — which predate the
experiment, so it must emit a well-formed and meaningless result. That proves the analysis
is fixed and executable without revealing anything about the arms. Record the commit sha
on the map; the sha is the tamper-evidence.

## Answer

**Pre-registration and analysis committed together in `37a325e`, before any experimental
batch ran. That sha is the tamper-evidence.**

- `.scratch/thesis-ab/preregistration.md` — the claim, with a machine-readable ```json
  block at the foot. No alternatives are listed, on purpose.
- `scripts/ab_analyse.py` — parses that block. It hardcodes nothing.

### The two properties that make it binding

**It aborts rather than defaults.** Missing file, more than one JSON block, unparseable
JSON, or a missing required key each stop the run. Verified:

```
D: pre-registration missing  -> ABORT: no pre-registration at ... The analysis is not
                                defined without one.
E: json block mangled        -> ABORT: pre-registration JSON is unparseable: Expecting
                                ':' delimiter: line 2 column 11
```

**The sample size is DERIVED, so it cannot drift from the rule.** The script recomputes
n from the pre-registered baseline, effect, alpha and power, and refuses if it disagrees:

```
A: n edited 147 -> 60   -> ABORT: says n=60 per arm, but baseline 0.129 +100% at
                           alpha=0.05/power=0.8 needs 147. One of them was edited
                           without the other.
B: rule softened        -> ABORT: n=147 was computed under the asymmetric rule.
                           decision_rule is 'adopt_on_any_improvement' — recompute n
                           before analysing, or the test is underpowered for the rule
                           being applied.
C: effect -> +50%       -> ABORT: needs 510.
```

C reproduces the map's 510/arm figure **independently**, from the script's own power
calculation rather than from the charting note.

**A mid-run sha change refuses the verdict**, reporting the split instead of pooling two
codebases:

```
F: VERDICT REFUSED — the git sha changed mid-run, so these rows come from more than one
   codebase:   aaaaaaaaaaaa 20 rows   bbbbbbbbbbbb 20 rows
```

### Definition of done — it runs green on historical rows

The real invocation, today, correctly finds nothing:

```
power check       OK — n=147/arm for +100% on a 12.9% baseline, alpha=0.05, power=0.8
No sidecar at .ab_test/tags-thesis.jsonl — the experiment has not produced any tagged
candidate yet. Nothing to analyse. This is the expected state before the run starts.
```

And `--smoke 400`, which alternates arms over the 400 most recent validated rows, runs
the whole path and labels its output meaningless:

```
*** SMOKE RUN — arms are synthetic (alternating over historical rows).
*** Every number below is MEANINGLESS. It proves the script runs.
counts            control 200   challenger 200   (target 147/arm)
PRIMARY  wf_nonzero_rate
  control      42/200 = 21.0%     challenger   39/200 = 19.5%     p = 0.7090
SECONDARY rank test on the non-zero tail (n 42 vs 39): U=890.0 p=0.5052
SECONDARY is_nonzero_rate: control 63.5%  challenger 74.0%  p=0.0235
VERDICT WITHHELD — smoke run, synthetic arms.
```

An interim look is also refused: below the pre-registered n the script withholds the
verdict and names the stopping rule.

### Evidence

- `tests/test_ab_analyse.py` — 15 tests, including that the SHIPPED pre-registration is
  self-consistent, and that a NULL walk-forward / an `-inf` in-sample score count as
  failures rather than dropped rows
- Full suite — **1228 passed**
