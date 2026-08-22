# Task: durable pytest suite for the deferred-action mechanism

Repo: /Users/lich/deepseek-oanda-trading  (run from repo root, use ./venv/bin/python)

Write ONE new file `tests/test_deferred_actions.py`. Do NOT modify any other file.
Do NOT modify `fix_runner.py` — if a test fails, report it, do not "fix" the source.

## What you are testing

`fix_runner.py` recently gained a deferred-action mechanism. The pod places all
orders in a single daily pass, but some instruments' markets are shut at that
instant, so the intent is queued and executed later when the session opens.

Functions under test (all in `fix_runner`):
- `market_shut(inst, adapter, now)` -> True / False / None. None = schedule
  unreadable = "caller proceeds exactly as before".
- `session_end(now, intervals, tzname='Europe/Bucharest')` -> UTC datetime or None.
  `intervals` is [(start_sec, end_sec)] from Sunday 00:00 in that timezone.
- `_read_deferred()` / `_write_deferred(q)` / `clear_deferred(sid, q=None)`
- `defer_action(sid, inst, kind, sig, prev_sig, units, stop_mult, atr, st, now, broker_day)`
- `deferred_drain(sleeves, state, adapters, live, now=None)`

Module globals you must override in fixtures (assign directly, then restore):
`DEFER_FILE`, `STATE_FILE`, `DEFER_SHUT_MARKET`, `GUARD_ENABLED`, `_read_halt`,
`halt_is_active`, `_SESSION_CACHE` (call `.clear()` between tests — it caches
schedules for the process lifetime and WILL leak between tests if you forget).

`FLAT` is `lambda sig=0: {'signal':sig,'pos_id':None,'units':0.0,'side':0,'stop':None,'stop_ref':None}`.

## Fakes you need

adapters is a dict: `{'fix': {inst: adapter}, 'price': {inst: price_adapter},
'equity': callable_returning_float}`.

Adapter must implement: `session_intervals()`, `open_pos_ids()` (returns a set),
`execute_order(signed_units, tag)` -> pos_id or None, `place_stop(pos_id, units,
side, px)` -> dict, `cancel_stop(ref, side)` -> truthy or None,
`close_position(pos_id, units, side)` -> dict.

CRITICAL: `place_stop` success is `{'ord_status': '0', 'ref': str(pos_id)}`.
`fix_runner._stop_ok(ref)` returns True ONLY for a dict with `ord_status == '0'`.
A bare string is treated as FAILURE and triggers one retry — if you return a
string you will see 3 sends instead of 2 and misread it as a bug.

Price adapter implements `get_current_price()`.

Sleeve dicts look like `{'sid': 's1', 'inst': 'AU200_AUD', 'params': {'stop_mult': 1.5}}`.

Derive the expected broker day with:
`import prop_guard; prop_guard.broker_now(now).strftime('%Y-%m-%d')`
Do NOT hardcode it — the broker clock is America/New_York + 7h, not UTC.

## Required test cases (all must be present)

Session logic:
1. AU200_AUD's real schedule (02:50-09:29 and 10:10-23:59 Europe/Bucharest,
   weekdays Mon-Fri) is OPEN at the 00:15 UTC pass on 2026-08-19 and SHUT on
   2026-12-16. This is the seasonal bug the mechanism exists for.
2. `market_shut` returns None when `session_intervals()` returns [].

Queue:
3. `defer_action` then `_read_deferred` round-trips; the record does NOT contain a
   `stop` key (the stop price is deliberately derived at fill time, never carried).
4. A second `defer_action` for the same sid REPLACES the first (supersession), it
   does not stack a second entry.
5. `clear_deferred` removes it.

Drain — each of these must assert BOTH the number of broker calls made AND the
resulting queue length:
6.  Guard halt active -> queue emptied, ZERO broker calls.
7.  `_read_halt` raises -> queue HELD (still 1 entry), ZERO broker calls. This is
    fail-closed behaviour and is the single most important test in the file.
8.  Intent whose `broker_day` differs from today's -> dropped, zero calls.
9.  Session still shut -> intent kept, zero calls.
10. Session open -> exactly 2 calls (execute_order then place_stop), queue empty,
    and `state[sid]` has the right pos_id, units, side, and a non-None stop_ref.
11. DOUBLE-SEND: state already holds a pos_id that IS in `open_pos_ids()` and the
    intent is an 'open' -> intent dropped, ZERO calls.
12. A 'close' intent whose pos_id is NOT in `open_pos_ids()` -> dropped, zero calls.
13. `cancel_stop` returns None -> abort: no `close_position` call, position left in
    state untouched, intent kept.
14. `execute_order` returns None -> intent kept for retry.
15. `DEFER_SHUT_MARKET = False` -> `deferred_drain` is a no-op even with a full queue.

## Evidence required

Run: `./venv/bin/python -m pytest tests/test_deferred_actions.py -q`
then:  `./venv/bin/python -m pytest -q`   (the FULL suite must stay green)

PASTE THE ACTUAL OUTPUT of both commands into your answer.

Write a short summary to `.scratch/defer/n3-tests.md` covering: how many tests,
what each group covers, and anything you found that looks wrong in `fix_runner.py`
(report it, do not fix it).

End your answer with exactly one line:
VERDICT: PASS or VERDICT: FAIL — <reason>

Claim PASS only if BOTH pytest commands exited 0.
