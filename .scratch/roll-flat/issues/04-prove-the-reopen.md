# Does the existing 21:05 pass reopen correctly?

Type: task
Status: open
Blocked by: 03

## Question

Given 01, a covered sleeve that was closed at 20:50 should be reopened by the trading pass
that already runs after the roll — no second pass needed. That is the assumption the plan
was costed on, and it has not been checked.

Verify it, and if it is false, add the reopen pass.

- Confirm the 21:05 pass genuinely re-establishes the position at the expected size and
  with the stop ATTACHED. A reopen without a broker-side stop is worse than not reopening:
  under netting the stop is evaluated per-bar inside a loop that may not be running.
- Confirm it reopens on the CORRECT signal — the one from the bar that just closed, not a
  stale one.
- Confirm a sleeve that legitimately went flat overnight (real signal change) is NOT
  reopened by the policy path.
- Establish what the reopen costs in practice versus the modelled full spread. The plan
  accepted 5.44× headroom without measuring; these fills are the first real evidence.

## Definition of done

A written confirmation of each bullet from real artifacts — order receipts, reconcile
output, and the stop read back from the broker — not from reading the code. If a second
pass turns out to be required, it is built and exercised here.
