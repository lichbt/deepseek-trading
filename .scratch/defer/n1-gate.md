# N1 — session gate & deferred-action store

VERDICT: PASS — `pytest -q` 1294 passed, exit 0. Behaviour-neutral by default
(`DEFER_SHUT_MARKET` unset). No drain yet; that is N2.

## What changed (`fix_runner.py`, all local, nothing pushed)

1. **Config** — `DEFER_SHUT_MARKET` (default `0`) + `DEFER_FILE`
   (`/data/deferred_actions.json`). Default-off matches ROLL_FLAT / WEEKEND_FLAT /
   VENUE, so rollback is unsetting the var, never a code revert.
2. **`market_shut(inst, adapter, now)`** — True / False / **None = unknown**.
   Reuses `_session_intervals` + `session_end`, so "open" has ONE definition in
   this file. Unknown means proceed exactly as before.
3. **Queue helpers** — `_read_deferred` / `_write_deferred` (atomic via
   `os.replace`) / `defer_action` / `clear_deferred`. Keyed by sleeve id.
4. **The gate** in `run_once`, after the min-lot risk cap and **before the close
   block**.
5. **Boot banner** — names any sleeve whose market is shut at this pass.

## Two decisions that differ from the plan, and why

**The stop price is NOT carried.** The plan said "carry the pass's units and stop".
Units are carried; the stop is not. The ordinary path computes the stop from the
LIVE entry price precisely because "a stale close can put the stop on the wrong
side of current market -> broker rejects it". A stop computed at 00:15 and applied
at 02:50 is that same stale stop. So `stop_mult` and `atr` travel instead and the
drain derives the stop at fill time. Sizing parity is preserved; stop validity is
not sacrificed for it.

**The gate sits before the close, not after sizing generally.** The close path
cancels the broker stop as its FIRST act. A close sent into a shut session can
cancel the stop and then be rejected, leaving the position bare — the 2026-08-10
NAS100 incident, ~3h unstopped. Gating before it means we never begin a close we
cannot finish. This is a safety fix, not only an availability one.

## Queue schema (`deferred_actions.json`)

```json
{"<sleeve_id>": {
  "kind": "open|close|flip", "inst": "AU200_AUD",
  "signal": 1, "prev_signal": 0,
  "units": 2.0, "stop_mult": 1.5, "atr": 12.3,
  "pos_id": null, "stop_ref": null, "side": null, "held_units": null,
  "broker_day": "2026-12-16", "created": "2026-12-16T00:15:03+00:00"}}
```

Keyed by sleeve: a sleeve wants one thing at a time, so the queue is idempotent for
free and supersession is natural — the next broker day's pass overwrites rather
than stacking. **No timer, no expiry count**: a staleness cutoff was measured and
rejected twice (2026-07-31, re-confirmed 2026-08-06, "there is no k"), and one here
would be that rejected policy under a new name.

## Verified this session

| check | result |
|---|---|
| AU200 real schedule, 19 Aug pass | `market_shut` = False (open) |
| AU200 real schedule, 16 Dec pass | `market_shut` = True (shut) |
| no schedule readable | `None` — caller proceeds as today |
| defer → supersede same sleeve | 1 entry, signal/day replaced |
| stop price stored | no (by design) |
| clear | queue empty |
| full suite | 1294 passed, exit 0 |

## Left for N2

Drain on the 60s tick; **drop the queue on a guard halt**; reconcile against broker
positions before draining (double-send guard); derive the stop at fill time.
