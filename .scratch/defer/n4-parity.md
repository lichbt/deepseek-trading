# N4 — Deferred-order / daily-bar simulator parity

Measurement: for every weekday in 2024-01-01..2026-12-31, take the prop pass instant (00:15 UTC). If AU200_AUD is SHUT then, step forward in 5-minute increments (cap 24h) to its next open, and compare the broker-day label of the reopen to the broker-day label of the pass.

Broker day = `prop_guard.broker_now(dt).strftime('%Y-%m-%d')` (America/New_York + 7h). Schedule membership via `fix_runner.session_end(now, intervals)`.

## Counts

- Weekdays swept: **784**
- OPEN at the pass (no deferral needed): **450**
- SHUT, reopened in the SAME broker day (deferral works): **334**
- SHUT, reopened only in a LATER broker day (deferral FAILS — sleeve would silently not trade): **0**

## Date ranges where the FAIL category occurs

- _None._ The same-broker-day property holds for every weekday in the sweep window.

## Verdict

PASS — the deferred fill always lands on the same broker bar the daily-bar simulator assumed, so the simulator's performance figures are valid for the deferred sleeves.
