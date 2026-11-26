# n3b-drain — deferred_drain SAFETY CLAUSES

9 tests in `tests/test_deferred_drain.py`; harness pasted verbatim, nothing else touched.
Isolated run: 9 passed. Full suite: 1315 passed (0.41s / 55.23s).

Clauses proved, each asserting BOTH `len(ad.sent)` and `len(F._read_deferred())`:
(1) latched halt drops the whole queue, zero broker calls; (2) halt check RAISING
holds the queue — fail-closed, the most important test here; (3) supersession by
broker_day ('1999-01-01' dropped, no timer/expiry); (4) still-shut session keeps the
intent; (5) happy path = exactly open+stop, state recorded (pos_id/units/side/stop_ref).

(6) double-send guard: state already holds POSX AND broker lists it -> open dropped,
zero calls (pass died mid-write, no doubling); (7) close against a gone pos_id dropped;
(8) stop-cancel unconfirmed -> abort BEFORE close_position, position intact, intent kept;
(9) DEFER_SHUT_MARKET=False leaves a full queue untouched. q2usd patched to 1.0 in the
happy path so risk math is deterministic and network-free; every other test short-circuits
before the open leg so q2usd never runs. env fixture clears _SESSION_CACHE per test.
