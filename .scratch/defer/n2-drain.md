# N2 — drain on the existing 60s tick

VERDICT: PASS — `pytest -q` 1294 passed, exit 0; all 9 critical paths verified by a
purpose-built harness this session. Still behaviour-neutral by default
(`DEFER_SHUT_MARKET` unset). Nothing pushed.

## `deferred_drain(sleeves, state, adapters, live, now=None)`

Rides the poll that already exists for the guard. No new cron line, no second
process, no change to trigger semantics.

**It executes a decision, it does not make one.** Signal, direction and size were
settled by the pass and recorded. The only value derived at drain time is the stop
price, from the live entry price — a stop computed hours earlier sits on the wrong
side of current market and is rejected.

## Order in the poll loop

`weekend_flat_close` -> `weekend_flat_reopen` -> `roll_flat_close` -> **drain** ->
trigger/pass.

- **After both flat legs**, because they close positions and write state the drain
  must see. Draining first could open a position roll-flat then immediately closes
  — two round trips for nothing.
- **Before the trigger**, because a pass about to run re-decides the sleeve anyway.
  Settling first means the pass sees a real position rather than a stale intent.

## The safety clauses, in the order they fire

| # | Clause | Behaviour |
|---|---|---|
| 1 | **Guard halt** | drops the ENTIRE queue, opens and closes alike, and returns |
| 2 | **Halt check raises** | HOLDS the queue — fails closed, never open |
| 3 | **Supersession** | intent whose `broker_day` != today's is dropped |
| 4 | **Still shut** | left queued, nothing sent |
| 5 | **Broker snapshot fails** | holds the queue |
| 6 | **Double-send** | sleeve already holds a position in `open_pos_ids()` -> open intent dropped |
| 7 | **Position already gone** | close intent dropped; a `flip` degrades to `open` |
| 8 | **Stop cancel unconfirmed** | aborts before closing, position left intact, intent kept |
| 9 | **Entry rejected** | intent kept, retries next tick |

Clause 1 is the load-bearing one. The queue exists to place orders when nothing
else is watching, so an entry surviving a halt would re-enter a book the breaker
had just flattened. Closes are dropped too rather than kept: the guard has already
flattened, so a queued close aims at a position that no longer exists.

Clause 2 matters as much and is easy to get backwards. If the halt file cannot be
read, the queue is HELD, not drained — draining is the action that needs the guard's
permission, so an unreadable guard must block it.

## Verified this session

```
PASS  HALT active -> queue dropped, nothing sent            sent=0  queued=0
PASS  halt check RAISES -> queue HELD, nothing sent         sent=0  queued=1
PASS  stale broker_day -> superseded, dropped               sent=0  queued=0
PASS  still SHUT -> held, nothing sent                      sent=0  queued=1
PASS  session OPEN -> order + stop attached, queue cleared  sent=2  queued=0
PASS  DOUBLE-SEND: already holds pos -> dropped, no order   sent=0  queued=0
PASS  close intent, position already gone -> dropped        sent=0  queued=0
PASS  STOP CANCEL UNCONFIRMED -> abort, no close, kept      sent=1  queued=1
PASS  entry rejected -> intent kept for retry               sent=1  queued=1
```

State written by a drained open matches `run_once` exactly, including storing the
whole `place_stop` ack as `stop_ref` (a bare id there would make `cancel_stop`
return None and the runner would refuse to ever close the position).

## Deliberately NOT done here

The drain does not call `run_once` for a subset of sleeves, which was the obvious
reuse. `sweep_orphans` iterates STATE and treats any sid absent from `sleeves` as
departed — passing a subset would close the entire rest of the book. A separate,
smaller executor is the correct shape, and it duplicates no decision logic because
every decision was already recorded.

## Left for N3 / N4 / N5

N3 turns the harness above into a durable `tests/` suite. N4 asserts sim parity.
N5 decides whether HK33 is actually reachable — a 00:15 pass with a drain that can
only fire while the pod is awake still needs HK33's session to intersect the poll.
