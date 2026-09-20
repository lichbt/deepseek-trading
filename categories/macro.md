<!-- macro.md — single source for the macro generation category. Loaded by auto_research._category_constraint('macro'). Dynamic: {instrument} and {cols} are replaced at runtime by _macro_constraint_for (cols = the instrument's available macro columns), and {driver} is the slot's PINNED macro driver (rotated by _macro_driver_for over _MACRO_DRIVERS) — when non-empty the slot MUST build on that driver and not drift to a US-real-yield/DXY default. Edit the WRAPPER text here; the per-instrument column list stays in macro_fetcher; the driver list stays in auto_research._MACRO_DRIVERS. -->
# Macro category

## CONSTRAINT

MACRO MODE: build a strategy whose edge IS a macro relationship. entry_condition or filter_condition MUST reference one or more of these EXACT columns, the ONLY ones available for {instrument}: {cols}. An invented column name fails the strategy. Macro values carry real-world PUBLICATION lags (rates/yields ~1 day, dollar index ~1 week, CPI/monthly ~6 weeks), so no same-day reactions — use persistent conditions and slow differentials. This is a macro-archetype strategy.

ASSIGNED DRIVER (build the edge on THIS, not a default): {driver}

Use home+US columns TOGETHER for a differential (never one side); for curve/term structure use us10y2y or us10y-us2y and trade the spread's TURN. Two-sided (long AND short) — one-sided macro→LONG is beta and is rejected.

## GUIDANCE

## Macro data — rate/yield/curve drivers

A **macro archetype** injects interest-rate, bond-yield, CPI and dollar-index
series alongside the OHLC bars. Use it when the edge genuinely IS a macro
relationship, not decoration on a price strategy. The exact columns available
are listed in the slot's CONSTRAINT (`{cols}`) — that list is authoritative and
per-instrument; never assume a column it omits (an invented name fails).

### The assigned driver (rotation)

Each macro slot is pinned to ONE driver by the scheduler (rotated over
`auto_research._MACRO_DRIVERS`) and spliced into the CONSTRAINT as `{driver}`.
Build on THAT driver. Do not re-list the drivers as a menu here: the menu is
exactly what let the model anchor on "real yield" and "dollar regime" (54% and
~1/3 of 1,821 macro slots). The rotation is the source of truth.

**⚠ Common failure (from live data): `us_real_yield < MA & dxy < MA` → LONG is a
beta trap.** It captures a past low-real-yield bull (2020–24) and INVERTS when
real yields rise — great in-sample/holdout, then implodes live. A macro edge must
be **two-sided**: define BOTH the long state (yields falling) and the short state
(yields rising). A one-sided `macro-condition → LONG` on a trending asset is
directional beta, which the drawdown gate and deploy review reject. **Worked
example:** LONG when `us10y−eu10y` is falling AND below its 60-day mean; SHORT
when rising AND above — the edge is the *differential's turn*, not a standing tilt.
