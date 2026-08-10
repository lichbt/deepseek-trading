# The pre-roll close pass, and surviving 21:00

Type: task
Status: open
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
