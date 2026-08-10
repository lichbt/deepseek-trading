# Does the deliberate-flat state survive a restart?

Type: task
Status: open
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
