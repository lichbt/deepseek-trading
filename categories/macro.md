<!-- macro.md — single source for the macro generation category. Loaded by auto_research._category_constraint('macro'). Dynamic: {instrument} and {cols} are replaced at runtime by _macro_constraint_for (cols = the instrument's available macro columns). Edit the WRAPPER text here; the per-instrument column list stays in macro_fetcher. -->
# Macro category

## CONSTRAINT

MACRO MODE: design a strategy whose edge is driven by macro data — rate differentials, carry, central-bank policy divergence, real-yield moves, or DXY regime. entry_condition or filter_condition MUST reference one or more of these EXACT macro columns, which are the ONLY ones available for {instrument}: {cols}. Do NOT reference any macro column outside that list — inventing a column name will fail the strategy. IMPORTANT: macro values arrive with their real-world PUBLICATION lags (daily rates/yields ~1 day late, the dollar index ~1 week late, CPI and other monthly series ~6 weeks late). Same-day macro reactions are NOT observable — design the edge around persistent macro conditions and slow-moving differentials, not immediate responses to today's data. This is a macro-archetype strategy.

## GUIDANCE

## Macro data — available for rate-driven theses

A **macro archetype** is available that injects interest-rate, bond-yield, CPI
and dollar-index series alongside the OHLC bars. Use it when the edge is
genuinely driven by monetary policy or macro factors — rate differentials,
carry, policy divergence, real-yield moves — not as decoration on a price
strategy.

Columns added (daily, forward-filled from FRED data):

- **US (added for every instrument):** `fed_rate`, `us10y`, `us_real_yield`,
  `us_cpi`, `dxy`
- **Home-currency of the instrument:** the matching central-bank rate, 10y
  yield, and CPI — e.g. EUR pairs also get `ecb_rate`, `eu10y`, `eu_cpi`;
  GBP pairs get `uk10y`, `uk_cpi`; JPY pairs get `jp10y`.

To use it, the strategy spec should describe a macro relationship in
`entry_condition` / `filter_condition` (e.g. "us10y − eu10y spread widening",
"real yield rising", "DXY breaking its 60-day mean"). The code generator will
set `archetype: "macro"` so these columns are present.

Good macro theses: rate-differential momentum, carry tilt by policy stance,
DXY regime as a cross-market filter, real-yield-driven gold/FX moves. The
direction-agnostic and regime-gating rules above still apply — a macro signal
is an entry/filter input, not a licence to take a one-sided bet.

**⚠ Common failure (from live data): `us_real_yield < MA & dxy < MA` → LONG is a
beta trap.** This exact template captures a past low-real-yield bull (2020–24) and
INVERTS when real yields rise — it scores great in-sample/holdout, then implodes
live. A macro edge must be **two-sided**: define BOTH the long state (yields
falling) and the short state (yields rising), not just the long leg. A one-sided
`macro-condition → LONG` on a trending asset is directional beta, which the
drawdown gate and deploy review reject. **Worked example:** LONG when
`us10y−eu10y` spread is falling AND below its 60-day mean; SHORT when it is rising
AND above — symmetric, so the edge is the *differential's turn*, not a standing tilt.
