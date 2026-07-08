<!-- calendar.md — single source for the calendar generation category. Loaded by auto_research._category_constraint('calendar'). Forced slot (i%10==0, ~5%), daily-pinned. The calendar window IS the regime gate (no separate price detector). -->
# Calendar / seasonal category

## CONSTRAINT

CALENDAR/SEASONAL: design a TWO-SIDED edge from a dated institutional flow with a NAMED origin (month-end index/pension rebalancing, turn-of-month retirement inflows, options-expiry positioning, day-of-week liquidity). Build it from the calendar columns (dow, cal_month, tdom, tdom_left, turn_of_month) — NOT df.index. Name the flow and a falsifiable window; do NOT fish for the best weekday. The calendar window IS the regime gate (no separate price detector needed). Aim for balanced long/short occurrence.

## GUIDANCE

## Calendar / seasonal data — available for flow-timing theses

A **calendar archetype** injects explicit seasonal columns alongside the OHLC
bars, so a seasonal edge reads `df['dow']` etc. — NEVER `df.index.dayofweek`
(the signal frame is range-indexed at validation time; that crashes).

Columns added (every instrument, daily):

- `dow` — day of week, 0=Mon … 4=Fri
- `cal_month` — calendar month 1–12 (monthly seasonality)
- `tdom` — trading day-of-month, 1-indexed
- `tdom_left` — trading days until month end (1 = the last trading day)
- `turn_of_month` — 1 inside the documented flow window (first 3 + last trading day)

Use it when the edge is a **dated institutional flow with a named origin** —
month-end index/pension rebalancing, turn-of-month retirement inflows,
options-expiry positioning, day-of-week liquidity patterns. The code generator
sets `archetype: "calendar"` so the columns are present.

Calendar edges admit genuinely *two-sided* design and are *regime-independent*
(a month-end flow happens in bull and bear alike), so they can diversify a book
that is otherwise directional beta. BUT they are trivially over-fittable (many
day/month buckets) — a thesis MUST name the institutional flow it captures and
define a falsifiable window; fishing for "the best weekday" with no named cause
is rejected, and a one-day bucket with <20 occurrences/year is too thin to trust.

**⚠ Common failure (from live data): plain day-of-week seasonals DON'T survive
holdout.** Calendar has the worst holdout durability of any family (a
"Thursday is directional" edge halves dev→holdout) — these are usually
data-mined artifacts. Only calendar edges tied to a STRONG, dated institutional
flow generalise: **turn-of-month / month-end rebalancing** (`turn_of_month`,
`tdom_left<=1`) beats "day X of week" every time. If you can't name the flow and
why it recurs, don't propose it. **Worked example:** entry LONG `turn_of_month==1`
(pension inflow window) / SHORT `tdom==1 & prev-month-return<0` (rebalancing sells
into the new month), gated by a vol-regime filter, exit after 2 bars.
