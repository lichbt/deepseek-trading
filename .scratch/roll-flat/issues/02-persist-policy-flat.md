# Does the deliberate-flat state survive a restart?

Type: task
Status: resolved
Assignee: lich (claude session 2026-08-10)
Blocked by: 01

## Question

**Re-scoped by 01, which found the state already exists and is already persisted.**
This is now a verification and one decision, not a build — no new column, no migration.

`FLAT(0)` is written into `fix_runner_state.json` by `flatten_all` and by `run_once`, so a
policy close is durable for free. What has not been checked is what happens when the gap
between the close and the reopen contains a restart, a redeploy, or an outage.

- **Confirm** a `FLAT(0)` written at 20:50 is still `FLAT(0)` after a pod restart, and that
  the next pass re-establishes. The state file lives on the mounted volume and is
  deliberately untracked — a shipped copy would make a fresh volume claim positions it does
  not own.
- **Decide expiry.** `FLAT(0)` means "re-establish next pass" with no timestamp. If the pod
  is down from Friday to Monday, it re-establishes at whatever price it wakes to. The
  guard's halt already behaves this way and that has been accepted — the question is
  whether the roll policy should inherit it or bound it.
- **Check the collision.** The guard writes `FLAT(0)` too. A halt and a roll close on the
  same evening are indistinguishable in state. Establish whether that matters — if the
  halt is active, the reopen must not fire.

## Definition of done

A written answer to each bullet with the evidence that produced it — a real restart of the
state file, not reasoning about the code. If expiry or a halt interlock turns out to be
needed, it is built and tested here.

## Answer

**All three bullets clear with no code change. No expiry, no interlock.**

### Restart survival — verified on a real file

`fix_runner_state.json` is plain `json.dump`/`json.load` of the same dict, so the round trip
is lossless by construction — but "by construction" is what ticket 01 was told about the
state that turned out not to exist, so it was measured instead. Four tests in
`tests/test_roll_flat_state.py::TestItSurvivesARestart` go through the real write (the
`json.dump` ending `run_once`/`flatten_all`) and the real read (the `json.load` in `main`)
on a real file:

- a policy close written at 20:50 is still `signal == 0` after the round trip and still
  reads as a change → **reopens**
- a stop-out is still `signal == -1` and still reads as no-change → **stays flat**
- both in ONE file — a policy close and a stop on the same evening — do not blur

`tests/test_roll_flat_state.py` — **12 passed** (was 8).

### Expiry — NOT needed, and a timestamp would gate nothing

**`FLAT(0)` carries no stale intent.** It records only "no position, no remembered signal";
it does not record what to re-enter. `run_once` recomputes the signal from `latest(s)` on
every pass (`fix_runner.py:711`) and re-sizes from the current ATR and equity. So a
`FLAT(0)` that sat through a three-day outage re-establishes on the signal that is live when
the pod **wakes**, not the one that was live when it **closed** — behaviourally identical to
the startup align the runner already takes on a first-ever pass.

The bullet's worry ("it re-establishes at whatever price it wakes to") is real but it is not
a property of the roll policy: it is what this runner does after ANY downtime, for any
sleeve, and bounding it belongs to a downtime policy rather than here. Pinned as
`test_flat_zero_carries_no_stale_intent`.

### The guard collision — indistinguishable, and it does not matter

Both writers are `FLAT(0)` and the state cannot tell them apart. Walked through both
orderings:

| order | outcome |
|---|---|
| halt 19:00, then roll close 20:50 | book is already flat; the close pass finds nothing open and no-ops (`flatten_all` skips flat sleeves — `test_flat_sleeves_are_skipped`) |
| roll close 20:50, then halt 20:55 | indices already flat; the halt flattens the rest. No double close |

Neither fights, because a close is always safe and both mean the same thing to the reopen:
re-establish when trading resumes. **The asymmetry that matters is on the reopen, not the
close** — and that is already gated: `run_once` sets `trade = False` while a halt is active
(`fix_runner.py:698-703`), so entries cannot fire under a live halt.

### What remains unproven

The gate's PREDICATE is tested (`halt_is_active` — latches for the day, lifts at the broker
day roll, total never lifts). Its **wiring** into `run_once` is one `trade = False` line with
no test, because that needs a full `run_once` harness. **04 exercises exactly that path** and
should assert it rather than assume it.

Also carried from 05: in US summer a DAILY halt stops binding at the same instant the day
rolls, so a post-roll reopen is not blocked by a halt latched earlier that evening. That is
legitimate — the firm's daily loss resets at the same instant — but it means a halted day's
book re-establishes in full at the next pass. The roll policy neither extends nor shortens
that.

## Evidence

- `tests/test_roll_flat_state.py` — `12 passed`
- Full suite — see the map's Decisions row
