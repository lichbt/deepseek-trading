# Pre-registration and analysis script

Type: task
Status: open
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
