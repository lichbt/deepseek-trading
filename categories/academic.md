<!-- academic.md — single source for the academic-recall generation category. Loaded by auto_research._category_constraint('academic'). Dynamic: {anomaly} is the slot's PINNED anomaly (rotated by _academic_constraint_for) and {cols} is the instrument's available macro column list. Forced slot (i%6==1, ~15%), ranked last so it draws only from the free creative backbone. Added 2026-08-09. The rationale MUST open with `ACADEMIC(<anomaly>):` — that prefix is the ONLY attribution this category has (strategies has no gen_category column), so every measurement of whether academic recall beats free-form generation depends on it. -->
# Academic-recall category

## CONSTRAINT

ACADEMIC RECALL MODE: you are a quantitative researcher drawing on documented pre-2020 academic finance literature. Your assigned anomaly for this slot is: {anomaly}. Build the thesis on THAT anomaly — do not substitute another one, and do not fall back to a generic indicator strategy. A thesis that is not a time-series implementation of the assigned anomaly is OFF-SPEC and will be DISCARDED.

The `rationale` field MUST begin with the literal prefix `ACADEMIC(<anomaly name>): ` and then, in one sentence, state (a) WHY the anomaly exists — the behavioural or structural mechanism, not a restatement of the rule — and (b) its known decay or regime dependency (when the literature says it stops working). Follow this SHAPE, substituting your assigned anomaly — do not copy the placeholder, and do not switch to whatever anomaly an example names: `ACADEMIC(<Assigned Anomaly>): <one clause naming the behavioural or structural reason participants leave this return on the table>, which <one clause naming the regime or period where the literature says it stops working>, so the filter gates on <that condition>.`

SINGLE INSTRUMENT ONLY — there is no cross-section here. Every strategy trades ONE instrument's own history, so cross-sectional forms are impossible: do NOT propose ranking a universe, sorting deciles, long-short baskets, betting-against-beta across names, or per-name earnings drift. Implement the TIME-SERIES form of the anomaly on this instrument.

If the assigned anomaly is driven by macro data (carry, value/PPP, policy divergence, real yields), you MUST reference one or more of these EXACT macro columns, which are the ONLY ones available for {instrument}: {cols}. Do NOT invent a column name outside that list — it will not be injected and the strategy will fail. Declare this a macro-archetype strategy when you use them. Macro values arrive with real-world PUBLICATION lags (daily rates/yields ~1 day, dollar index ~1 week, CPI and monthly series ~6 weeks), so design around persistent conditions and slow-moving differentials, never same-day reactions. If the anomaly is price-based (momentum, reversal, breakout, volatility), use OHLC only and declare a standard-archetype strategy.

The published version of an anomaly is a starting point, not the strategy: state the regime dependency the literature documents and encode it as the `filter_condition`. Prefer a two-sided edge — the time-series form of most of these anomalies flips sign, and a one-sided long-only version of a drifting instrument is a beta harvest, not the anomaly.

HARD LIMITS: deterministic vectorized code, at most 4 tunable parameters, at most 200 original grid combinations. Never call `.rolling(...).apply(...)`; use `.ewm()`, rolling reductions, `.diff()`, `.shift()`, and `np.where`. The validator owns the ATR stop through `compute_returns_with_stop` — generated code MUST NOT implement trailing-stop state, per-bar position loops, or entry-price tracking.

## GUIDANCE

## Academic recall — anomalies from the literature, in time-series form

An **academic-recall slot** pins ONE documented anomaly and asks for its
time-series implementation on the assigned instrument. The point is to anchor the
thesis in a mechanism that has at some stage been measured and published, rather
than inventing an indicator rule — a different prior over which relationship to
trade, not new data.

### The constraint that removes most of the textbook list

Every sleeve trades **one instrument**. Anomalies defined by a *cross-section* —
cross-sectional momentum, value or size sorts, betting-against-beta, the
idiosyncratic-volatility puzzle, accruals, post-earnings-announcement drift —
cannot be expressed and must not be proposed. Only the time-series form counts.
Likewise there is no options data, so the volatility risk premium exists here only
as a realized-vol term-structure proxy, never as implied-minus-realized.

### The rotation

Each slot is assigned one of these, in order:

- **Time-series momentum (12-1)** — a positive trailing return over ~12 months
  excluding the most recent month predicts continuation; documented across futures
  and FX (Moskowitz–Ooi–Pedersen). Decays in choppy, mean-reverting regimes.
- **Short-term reversal** — 1–5 bar overreaction to liquidity shocks reverts.
  Strongest after high-volume or high-range bars; it inverts during strong trends.
- **Long-term reversal** — multi-year overextension corrects (De Bondt–Thaler).
  Needs a long lookback; on daily bars this is a very slow filter, prefer weekly.
- **FX carry** — high-rate currencies earn a premium as compensation for crash
  risk. Requires a rate differential from the macro columns; it unwinds sharply in
  risk-off, so a volatility gate is part of the anomaly, not an addition.
- **Real-exchange-rate value (PPP deviation)** — currencies far from purchasing-
  power parity mean-revert over long horizons. Slow, and the mechanism is the
  deviation itself, not price momentum.
- **Monetary-policy divergence** — a widening policy-rate or real-yield gap drives
  a persistent directional flow. Publication-lagged, so it is a *condition*, not an
  event trade.
- **Low-volatility effect (time-series form)** — risk-adjusted returns are better
  when realized volatility is low; leverage constraints keep the effect alive.
  Expressed as exposure gated on a volatility percentile.
- **Volatility risk premium proxy** — the term structure of *realized* volatility
  (short-window vs long-window) carries information about compensation for variance
  risk. No options data exists here, so this is a proxy and should be described as
  one.
- **Turn-of-the-month flow** — institutional rebalancing concentrates returns
  around month boundaries. Day-resolution; overlaps the calendar category, so it
  must be justified as flow, not fitted to a specific day.
- **Time-series breakout / managed futures** — the trend-following premium: a
  breakout of an N-bar range persists. The oldest documented form, and the one most
  likely to have decayed — say so and gate it.

### What is being measured

The rationale prefix `ACADEMIC(...)` is the attribution tag. It is how a later
session compares this category's pass rate against free-form generation:

```sql
select status, count(*) from strategies
 where rationale like 'ACADEMIC(%' group by status;
```

A thesis that drops the prefix is invisible to that comparison, which is why the
constraint makes it mandatory.
