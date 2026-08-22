# Task: prove a deferred order still lands on the same BAR the backtest assumed

Repo: /Users/lich/deepseek-oanda-trading  (run from repo root, use ./venv/bin/python)

Write ONE new file `tests/test_defer_sim_parity.py`. Do NOT modify any other file.
Do NOT modify `fix_runner.py` or any simulator — if something fails, REPORT it.

## Background

The prop pod places every order in one daily pass at 00:15 UTC. Two instruments'
markets are shut at that instant, so their orders are now queued and executed when
the session opens instead. Every performance figure for this book comes from a
DAILY-BAR simulator (`oanda_book_simulator.py`, `scripts/risk_model_sim.py`) which
has no intraday clock — it assumes an action decided for a bar happens on that bar.

The deferral is therefore safe for those figures IF AND ONLY IF the delayed
execution still falls inside the SAME BROKER DAY as the pass that decided it. If a
session opened only after the broker day rolled, the order would land on the next
bar, the simulator would be wrong, and (because `deferred_drain` supersedes any
intent whose `broker_day` differs from today's) the intent would be dropped unfired
and the sleeve would never trade at all.

THIS IS AN OPEN QUESTION, NOT A KNOWN-GOOD FACT. Your job is to measure it and
report the truth, whatever it is. A FAIL here is a valid and useful result.

## The measurement

The broker day boundary comes from `prop_guard.broker_now(dt)` (America/New_York
+ 7h) — NEVER a UTC constant. Get the day label with
`prop_guard.broker_now(dt).strftime('%Y-%m-%d')`.

Sessions are published as [(start_sec, end_sec)] from Sunday 00:00 in
Europe/Bucharest. Use `fix_runner.session_end(now, intervals)` to test membership:
it returns None when the instrument is SHUT at `now`, a UTC datetime when open.

Hardcode these two real schedules as fixtures (read from the live broker
2026-08-18; do not attempt a network call, the test must run offline):

- AU200_AUD: for each weekday Mon..Fri, two windows —
  02:50-09:29 and 10:10-23:59 Europe/Bucharest.
- A control that is open essentially always: NAS100_USD, 00:05-23:55 Mon..Fri.

## Required tests

1. **Same-broker-day property, AU200.** For every weekday from 2024-01-01 to
   2026-12-31: take the pass instant (00:15 UTC that day). If AU200 is OPEN then,
   nothing to prove. If it is SHUT, find the NEXT instant at which it opens by
   stepping forward in 5-minute increments (cap the search at 24h), and assert that
   `broker_day(that instant) == broker_day(pass instant)`.
   Report, in the test's failure message, the first date where it does not hold.

2. **Control.** Same property for NAS100_USD, which should be open at the pass on
   every weekday — assert it is never deferred at all.

3. **The supersede boundary is consistent with (1).** Assert directly that
   `deferred_drain`'s supersede rule and the reopen instant agree: build the same
   broker-day label from the pass instant and from the reopen instant found in (1)
   and assert equality. (This is what makes an intent survive long enough to fire.)

4. **No intraday input to sizing.** Assert that `fix_runner.defer_action` stores
   `units`, `stop_mult` and `atr` but NOT a `stop` price — i.e. the size is fixed at
   pass time (so it matches the simulator) while the stop is derived later.

## Also produce a summary table

For AU200 across 2024-01-01..2026-12-31, count and report in
`.scratch/defer/n4-parity.md`:
- how many weekdays the instrument was OPEN at the pass (no deferral needed)
- how many it was SHUT and reopened in the SAME broker day (deferral works)
- how many it was SHUT and reopened only in a LATER broker day (deferral FAILS —
  these are days the sleeve would silently not trade)
- the date ranges where the third category occurs, if any

## Evidence required

Run: `./venv/bin/python -m pytest tests/test_defer_sim_parity.py -q`
then:  `./venv/bin/python -m pytest -q`

PASTE THE ACTUAL OUTPUT of both into your answer.

End your answer with exactly one line:
VERDICT: PASS or VERDICT: FAIL — <reason>

Claim PASS only if both pytest runs exited 0. If the same-broker-day property is
FALSE for some dates, that is a genuine finding: write the tests so they express
the real behaviour, state the finding prominently, and report VERDICT: FAIL with
the dates. Do not weaken a test to make it green.
