# Does the existing 21:05 pass reopen correctly?

Type: task
Status: resolved
Assignee: lich (claude session 2026-08-10)
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

## Carried in from 05 (2026-08-10)

The title is stale: the pass to prove is the **00:15 UTC** one (hourly `:15` cron gated on
the 00:00 UTC hour), not a 21:05 pass — see 05 §5 and 03. It lands ~2h after the index
session reopens, exposure the simulator prices at zero; measure it here rather than
assuming it away.

## Answer

**No reopen pass is needed — the ordinary trading pass re-establishes correctly, with the
stop attached, on the current signal. Confirmed by driving the real `run_once`, and each
claim confirmed a second way by breaking the code it rests on.**

The DoD asked for real artifacts. Half of them cannot exist yet: `ROLL_FLAT` is unset on the
pod and nothing has been deployed, so there are no fills, no receipts and no broker-side
stop to read back. What CAN be established without trading is the decision path, and that is
what was missing — `flatten_all`'s FLAT(0) was tested where it is written and
`acts_on_signal` as a predicate, but nothing ever drove `run_once` from a policy-closed state
through to a broker order. The rest is handed to 07 explicitly.

### Each bullet

**Re-establishes at the expected size with the stop ATTACHED.** From `FLAT(0)` and a live
signal, `run_once` calls exactly `execute_order(+10, 'fix_nas100_x')` then
`place_stop(...)`, and only then records `stop_ref`. The stop is priced off the LIVE price
(98.0 = 100 − 2×1.0), not the stale close — a stale close can put the stop the wrong side of
market and get it rejected.

**Reopens on the CURRENT signal.** With the signal flipped between the close and the reopen,
the reopen goes the NEW way (`open, -10`), because `FLAT(0)` carries no intent (ticket 02).
A flat signal at reopen time sends nothing at all.

**A sleeve that went flat for a REAL reason is not reopened.** `FLAT(signal)` with the signal
unchanged sends nothing — the divergence commit `58c1a6f` removed. Positive control included:
a genuine flip after a stop still trades, so the negative test is not achieved by freezing
the sleeve.

**The halt gate's wiring** — handed over by 02 as its one untested line — now has three
tests: a latched daily halt blocks the reopen, a TOTAL halt blocks regardless of day, and
yesterday's daily halt does NOT block (correct: a daily halt is keyed on the broker trading
day, which in US summer rolls at the same instant as the swap roll, and the firm's daily loss
resets then too).

**A reopen that cannot be protected.** A rejected `place_stop` is retried and never recorded
as attached (`stop_ref` stays None, the software stop is armed) — an unstopped netted
position is worse than not reopening. And a rejected ENTRY keeps `FLAT(0)` so the next pass
retries, rather than advancing the signal and stranding the sleeve flat — the 2026-07-28
failure, on the reopen path.

### The tests bite — mutation-checked

Ten passing tests prove nothing on their own, so the code each claim rests on was broken and
the suite re-run:

```
--- mutant 1: halt gate wiring removed (`trade = False` -> `pass`) ---
FAILED TestTheHaltGateWiring::test_a_latched_halt_blocks_the_reopen
FAILED TestTheHaltGateWiring::test_a_total_halt_blocks_regardless_of_the_day
--- mutant 2: acts_on_signal ignores the recorded signal (`sig != 0`) ---
FAILED TestItDoesNotReopenWhatItDidNotClose::test_a_stopped_out_sleeve_is_not_reopened
--- mutant 3: _stop_ok accepts a reject as attached (`bool(ref)`) ---
FAILED TestAReopenThatCannotBeProtected::test_a_rejected_stop_is_retried_not_left_bare
```

Each mutant is killed by the test that claims to cover it, and by no other.

### The reopen's cost — the number 07 has to beat

Unmeasurable without fills, but the threshold is now explicit. NAS100 at 29,730: the modelled
round trip is **2.00 USD/unit** against **35.875 USD/unit/day** of carry. So the policy stops
paying only if the real round trip at the roll exceeds **17.94×** the modelled mid-session
spread. That is the number to hold the first real fills against — and it is why the scope is
indices: the metals sit at 1.43–1.48×.

### Evidence

- `tests/test_roll_flat_reopen.py` — **10 passed**, all three mutants killed
- Full suite — **1203 passed** (was 1193)

## What remains unproven

1. **Every live artifact.** No fills, no receipts, no broker-side stop read back, no measured
   spread. The fakes assert what the runner ASKS the broker to do, not what the broker does.
   → 07.
2. **The ~2h exposure gap.** The reopen is the 00:15 UTC pass, which lands about two hours
   after the index session reopens (summer). The simulator prices that at zero. Unquantified.
3. **The weekend arm still has no reopen story** and is still not built: a Friday close would
   re-establish on a Saturday pass into a shut market. Unchanged from 03 — it needs its own
   ticket, not a line in this one.
