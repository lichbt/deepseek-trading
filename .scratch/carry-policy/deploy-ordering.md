# The carry policy and the risk bump cannot ship together

Measured 2026-08-11, during the deploy that armed weekend-flat. Recorded because the
trap is invisible from either endpoint: both the old sizing and the new sizing are
safe, and the *transition* between them is not.

## What happened

`WEEKEND_FLAT_INSTRUMENTS` and `BASE_RISK=0.0065` were set in the same dashboard
edit, and the build applied both. The interlock was still holding pods at 0, so
nothing traded — which is the only reason this is a note and not an incident.

## Why it would have breached

Weekend-flat closes SPX500/XAG/XCU before the Friday close and does not re-enter.
On the day of the deploy (a Tuesday) all three were OPEN at the broker, and the leg
cannot act until Friday. So for three days the book runs on the **roll-only** curve
at the new sizing — a configuration neither of the two measured endpoints describes.

risk_model_sim, 22 sleeves, ctrader, commission+swap charged, BOOK_SCALE 1.1
modelled, 2024-01-01..2026-08-10, guard off:

| configuration | worst intraday | maxDD | tail to wall |
|---|---|---|---|
| 0.0055 roll-only (what was live) | -2.21% | -6.58% | 1.36x |
| **0.00715 roll-only (Tue->Fri reality)** | **-3.02%** | **-8.34%** | **0.99x** |
| 0.00715 + weekend leg holding | -1.98% | -3.70% | 1.51x |
| 0.0069 roll-only | -2.91% | -8.05% | 1.03x |
| 0.0069 + weekend leg holding | -1.97% | -3.64% | 1.52x |

0.99x means a day like the worst in sample is an instant DQ with no margin. Note the
advised 0.0069 is *also* unsafe roll-only (-2.91%, past the -2.40% halt) — so this is
not about the size of the bump, it is about the leg not being effective yet.

## The rule

**Arm a carry policy, let it fire once in production, and only then size up.** The
sizing that a policy makes safe is not safe during the window before the policy has
taken effect, because the positions it will act on are already open.

Corollary for weekend-flat specifically: its first effect is delayed by up to a week,
so the gap is days, not minutes. Roll-flat's is next-roll, so its gap is hours.

Precedent for not assuming a first firing works: roll-flat's first live night
(2026-08-10) failed completely — all 8 closes rejected, because the window sat inside
the index session break.

## Resolution

`BASE_RISK` reverted to 0.005 before the interlock was released. The bump waits on a
Friday that shows `[weekend-flat] FLATTEN ... closed 3, failed 0`. Because pod env
only applies on a real build, and only a file change triggers a build, the revert
needed its own build — this file is that change.
