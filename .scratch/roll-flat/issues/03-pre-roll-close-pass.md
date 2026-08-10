# The pre-roll close pass, and surviving 21:00

Type: task
Status: resolved
Assignee: lich (claude session 2026-08-10)
Blocked by: 02

## Question

Build the pass that closes covered positions before the 21:00 UTC rollover — indices every
day, the selective set on the Friday session (which is the THURSDAY-stamped bar).

The hard part is not the close, it is the minute it runs in. 21:00 UTC is the documented
failure window: daily orders at the bar close previously hit broker maintenance and
returned `MARKET_HALTED`, leaving the book dormant until a pending-entry retry was added.
This policy deliberately routes every index round trip through that window, nightly.

So this ticket must answer, with evidence rather than intent:

- **What time actually works?** ~20:50 is the assumption, not a measurement. The index
  session shuts BEFORE the 21:00 rollover (summer; winter is 21:50) — establish the real
  window rather than inheriting the guess.
- **What happens when the close is rejected?** The position then carries swap anyway. Is
  that a retry, an abort, or an accepted miss — and does the sleeve end the night in a
  state 04 can reopen from?
- **DST.** The host cron is +08 with no `CRON_TZ` support and the guard must be written in
  UTC — the same trap that once fired a trading pass inside the index close.
- It must not fight the guard, which already resets `prev_target = 0` on a daily halt.

Deliberately NOT in this ticket: the reopen (04).

## Definition of done

The pass exists, is scheduled, and has been exercised against the real venue in a way that
does NOT place book-changing orders — a dry run, or a rehearsal on a flat book. Paste the
observed behaviour at the real time of day, including at least one rejection path. State
plainly what remains unproven.

## Carried in from 05 (2026-08-10) — read before designing the schedule

- **There is no 21:05 trigger.** Since 2026-07-28 the pod fires HOURLY at `:15` and acts
  only in the **00:00 UTC hour** (`scripts/zeabur_interlock.sh cron-install`), precisely
  because 21:05 UTC sat 15 minutes inside the index session close. Any wording in this
  ticket that assumes a 21:05 pass is stale.
- **The close time cannot be a UTC constant.** The broker's day rolls at 21:00 UTC in US
  summer and 22:00 UTC in US winter (server clock = America/New_York + 7h, run and pasted
  in 05). A fixed 20:50 UTC close pays the FULL carry all winter. Derive the minute from
  the broker clock the way `prop_guard._trading_day` does, or the policy no-ops for ~4.5
  months a year and nothing will report it.
- A DAILY halt is keyed on the trading-day label, so in summer it stops binding at the same
  instant the roll happens; a TOTAL halt binds across it. The close pass must not depend on
  a halt still being latched.

## Answer

**Built, tested and rehearsed against the real board. Default OFF — `ROLL_FLAT=1` arms it,
so rollback is unsetting an env var, not a code revert.**

### It rides the poll loop, not cron

`_run_triggered` is already awake every `TRIGGER_POLL` seconds and already reads the broker
clock through `prop_guard` for its guard ticks. The close pass goes there. A second cron
line would have reintroduced the exact trap this ticket warns about: the host is +08 with a
cron that has no `CRON_TZ` support, so a scheduled UTC time there is not a UTC time.

### What time actually works — derived, not assumed

`prop_guard.broker_now()` is now THE definition of the broker wall clock and `_trading_day`
is a `strftime` of it, so the window and the latch cannot read different clocks. The window
is the last `ROLL_FLAT_LEAD` (10) minutes before the broker's midnight, which is also when
the index session shuts:

```
now UTC 2026-08-10T12:33:52
broker wall clock now: 2026-08-10 15:33   trading day 2026-08-10
due now? False   ROLL_FLAT=False   lead=10min   scope=['DE30_EUR','NAS100_USD','SPX500_USD']
next fires at 2026-08-10 20:50 UTC  (broker 23:50)
```

20:50 UTC — the assumption the map inherited, now *derived*. In winter the same code fires
at 21:50 UTC with no edit. Both regimes are pinned by tests, including the negative:
`test_winter_fires_an_hour_later_in_utc` asserts 20:52 UTC in December is NOT due, which is
the failure a UTC constant would have shipped silently.

### What happens when the close is rejected

**Retry while the window lasts, then accept the miss.** `flatten_all` already keeps a
rejected position open, in state and stopped; the latch is written only when EVERY covered
position closed, so the next poll retries. Once the window passes, the sleeve carries one
night of swap — deliberately cheaper than closing *after* the roll, which would pay the
round trip AND the carry. Pinned by
`test_a_rejected_close_keeps_the_position_and_does_not_latch` and
`test_the_retry_succeeds_on_the_next_poll`.

### It does not fight the guard

Both write `FLAT(0)` and mean the same thing, so neither ordering breaks: a halt that
already flattened leaves nothing for the 20:52 pass to find (`flatten_all` skips positions
with no `pos_id`), and a close followed by a halt leaves the halt only the rest of the book.
Pinned by `TestItDoesNotFightTheGuard`.

### Rehearsal on the real venue — no orders placed

Real account 48171893, real positions, `live=False` (which returns before `flatten_all`
touches an adapter at all):

```
  instrument   position_id        volume  covered?
  AUD_USD      4517172           1400000  no — keeps carrying
  NAS100_USD   4496836                 8  YES
  EUR_GBP      4496838            200000  no — keeps carrying
  XAG_USD      4496832              5000  no — keeps carrying
  BTC_USD      4496835                 1  no — keeps carrying
  USD_CHF      4496831            500000  no — keeps carrying

  forced now = 2026-08-10 20:52 UTC   (broker wall clock 23:52)
  [roll-flat] FLATTEN (roll-flat 2026-08-10): closed 1, failed 0
  result: (['nas100_usd_rehearsal'], [])
  latch written by a dry run? None

  nas100_usd_rehearsal   signal +0  pos_id None      <- FLAT(0), reopens next pass
  (the other five keep their pos_id and their signal)
```

One covered position selected out of six, the other five untouched, nothing sent, and a dry
run does not latch — so a rehearsal cannot suppress the real close.

### Evidence

- `tests/test_roll_flat_close.py` — **17 passed** (window in both DST regimes, the latch,
  scoping, the rejection path, the retry, the dry run, the guard collision)
- Full suite — **1193 passed** (was 1176)

## What remains unproven

1. **The real rejection path.** `live=False` short-circuits before the adapter, so the
   rejection tested is synthetic. A genuine broker refusal at 20:50 has not been observed.
2. **The 21:00 failure window itself.** This policy routes an index round trip through the
   minute that once returned `MARKET_HALTED`. The close is a CLOSE — the historical failure
   was on entries — but that is reasoning, not a measurement.
3. **Nothing has run at the real time of day on the pod.** `ROLL_FLAT` defaults off and is
   unset there; the rehearsal forced `now`.
4. **The Friday/selective arm is deliberately NOT built.** The mechanism takes an instrument
   set, so it is configuration away — but the weekend rule's REOPEN is unresolved: `FLAT(0)`
   re-establishes on the next pass, which for a Friday close is a Saturday pass into a shut
   market. The simulator modelled a Sunday reopen (`--monday-reentry`). **04 must settle this
   before the weekend arm is armed.**
