<!-- event.md — single source for the event generation category. Loaded by auto_research._category_constraint('event'). Forced slot (i%10==5, ~5%), daily-pinned. Must literally contain 'days_to_event'/'event_window' so the schedule's is_event daily-pin fires. -->
# Economic-event (news) category

## CONSTRAINT

EVENT-TIMING: build a TWO-SIDED edge whose ENTRY is gated on the US economic-release calendar using the injected columns days_to_event, days_since_event, event_window (TIMING ONLY — there is NO surprise/actual value). E.g. fade range extremes into pre-release compression (days_to_event<=2), or trade the post-release reaction when event_window==1 with a price/vol entry. The ENTRY MUST reference at least one of days_to_event / days_since_event / event_window by name. Put the event timing in the ENTRY and give the filter_condition a SEPARATE price/volatility condition: a second event column in the filter is almost always implied by the first (event_window==1 implies days_to_event<=5) and such a gate is REJECTED as redundant. You MAY repeat an event column as ONE conjunct provided a real gate sits beside it (`event_window==1 AND realized_vol > median`). A thesis that does NOT reference an event column is OFF-SPEC and will be DISCARDED — do NOT fall back to a price-only strategy. Design every window for DAILY bars (these columns are day-resolution).

## GUIDANCE

## Economic-event data — available for release-timing theses

An **event archetype** injects the TIMING of high-impact scheduled US releases —
CPI, Employment Situation (NFP), GDP, PPI, PCE — from the FRED release calendar.
A release schedule is published in ADVANCE, so "days until the next release" is
known at bar time — these columns are look-ahead-safe.

Columns added (every instrument, daily):

- `days_to_event` — calendar days until the NEXT high-impact release (capped 60)
- `days_since_event` — calendar days since the LAST one (capped 60)
- `event_window` — 1 on the release day AND the bar after (the reaction window)

Reference any of these in `entry_condition` / `filter_condition` and the code
generator sets `archetype: "news"` so they're present. Two classic, opposite
mechanisms:
- **Pre-event compression** — vol/positioning contracts as `days_to_event` → 0
  (e.g. fade range extremes when `days_to_event <= 2`).
- **Post-event reaction** — the print lands and the market over/under-reacts;
  trade the drift or fade when `event_window == 1` or `days_since_event <= 1`.

There is NO surprise/actual-value column (the free calendar gives timing only) —
design around TIMING, not the released number. Direction/regime rules still apply:
an event-timing gate is a `filter_condition` (WHEN the edge is live), not a
one-sided bet — pair it with an entry that can fire both ways.

**⚠ Common failure (from live data): the event column ALONE fires too rarely →
zero signals → IS=0.** Releases are ~monthly, so `days_to_event<=2` on its own
gives only a handful of bars/year; layered with another filter it produces
*nothing* and the strategy fails at the IS gate. Fix: use the event column as the
`filter_condition` (WHEN) and put a PRICE/VOL trigger in `entry_condition` (WHAT)
that fires often inside that window — the two multiply to enough trades. **Worked
example:** `filter` = `event_window==1` (reaction window), `entry` = fade a
1.5-ATR range-extreme LONG/SHORT, exit after 2 bars. Event strategies are pinned
to DAILY (the columns are day-resolution — weekly makes them meaningless).
