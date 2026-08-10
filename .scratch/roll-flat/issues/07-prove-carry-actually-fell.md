# Prove the carry actually fell

Type: task
Status: open
Blocked by: 06

## Question

The destination is not "the policy is deployed" — it is "the carry fell, and we measured
it". Everything upstream is intent; this is the only ticket that closes the loop against
reality.

`scripts/swap_log.py` already samples every 3h into the append-only `broker_swap` table,
and a charge is the DELTA between consecutive observations of the same `position_id`.

- Across the first roll after deploy, the index positions must show **zero** swap delta.
  If they do not, say so and find out why rather than explaining it away.
- Compare the realised round-trip cost against the modelled full spread. This is the
  measurement deferred at charting when the 5.44× headroom was accepted rather than
  measured — if the real cost is materially worse, the scope choice reopens.
- Check the Friday roll separately: the triple is where most of the saving is, and it is
  the roll most likely to collide with a session-close rejection.

## Definition of done

`swap_log.py --report` output pasted, showing the index positions' deltas across at least
one weekday roll and one Friday roll. A one-line verdict: did carry fall, by how much, and
what did the round trips cost. Record the outcome as a Second Brain decision either way.
