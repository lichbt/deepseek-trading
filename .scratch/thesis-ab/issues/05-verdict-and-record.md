# Compute the verdict and record it

Type: task
Status: open
Blocked by: 04

## Question

Run the analysis script committed in 02 — unmodified. If it needs a change to run, that
change is itself a finding and must be recorded, not quietly made.

Apply the pre-registered asymmetric decision rule:

- **pro wins the primary** → promote pro to the `THESIS_MODELS` head in `.env`, and note
  the throughput consequence (~1.8x slower per candidate) for downstream cadence.
- **flash-ga wins, or inconclusive** → keep flash-ga. Record that v4-pro is retired from
  thesis contention, so it is not re-proposed a fourth time — it has already been promoted
  once (2026-08-03) and reverted once (2026-08-04), each time without a recorded
  comparison a later session could read.

Then, either way:

- Write the verdict to the Second Brain via `brain.py decision`, under 600 chars, stating
  the rule and its reason rather than pasting the run.
- Commit the sidecar as the evidence the conclusion rests on, alongside the result.
- Update the `.env` comment block, which currently carries the superseded 2026-08-04
  n=32 finding as the standing rationale for the head.

## Definition of done

Verdict computed once, decision applied, brain entry written, sidecar committed. The
report states the arm counts and the excluded count explicitly — a silent truncation reads
as full coverage.
