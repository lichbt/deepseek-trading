# Persist policy-flat across a restart

Type: task
Status: open
Blocked by: 01

## Question

The pod loads state at startup and writes it back; a policy close at 20:50 and its reopen
at 21:05 can straddle a restart, a redeploy, or a crash. If the state is lost in between,
the sleeve is flat with an unchanged signal and today's rule keeps it flat — silently out
of the market until its next genuine flip.

Persist the `policy_flat` state from 01 the same way `stopped_signal` / `stopped_bar`
already are, so a restart cannot lose it.

- Follow the existing `sleeve_units` pattern rather than inventing a second mechanism —
  those columns exist precisely because a fired stop leaves no durable trace.
- Include enough to know the state is STALE: a policy close from three days ago is not a
  pending reopen, it is a bug. Decide and record what makes it expire.
- Migration must be additive and safe to run against the pod's mounted `/data/pipeline.db`,
  which is only seeded when absent and never refreshed by a push.

Deliberately NOT in this ticket: the passes themselves (03/04).

## Definition of done

Tests prove the state survives a simulated process restart, and that a stale entry expires
rather than firing a reopen days later. Migration applied and shown to be idempotent. Paste
real test output plus the full-suite result.
