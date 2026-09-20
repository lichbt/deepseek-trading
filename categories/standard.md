<!-- standard.md — single source for the standard generation category. Loaded by auto_research._category_constraint('standard'). Loaded as a LIST: each item below is separated by a line containing only '---'. Order matters (rotation index). The cross-market PAIR constraint lives in pair.md, not here, and is appended to the rotation at load time. -->
# Standard (creative default) category

## CONSTRAINT

Must avoid all moving-average crossover logic. Use price-relative or range-based entry instead.
---
Entry must be a directional momentum/continuation signal — trade WITH the move, not a fade. Do NOT use mean-reversion, skewness, or autocorrelation.
---
Use only a DATED FLOW effect — turn-of-month/month-end rebalancing or a named session flow; a bare day-of-week bucket is NOT enough. The dated window IS the ENTRY trigger (archetype 'calendar', built from turn_of_month/tdom_left), and the filter_condition must add a SEPARATE price/volatility state — a filter that repeats the window gates nothing and is REJECTED. Never df.index.dayofweek: the OANDA daily bar is stamped at its OPEN, so a weekday label is one session early. Name the flow and the payer in the rationale.
---
Name the FORCED COUNTERPARTY in the rationale — who must trade regardless of price (index rebalancer, stop cluster, liquidation or expiry flow) — and put the OHLC footprint that reveals them in the entry_condition. A thesis that names no payer is OFF-SPEC.
---
Exit must be time-boxed: a mechanism exit reading the quantity the entry is built on (indicator cross, level reclaimed, spread reverted) PLUS a hard bar-count timeout. Do not add a separate price/ATR stop — the backtest injects one anyway.
---
Do NOT enter on a bare highest-high/lowest-low break: the trigger must read a volatility STATE (realized vol or range expansion against its own rolling quantile) and the rationale must say what that state changes about who is trading. Two-sided.
---
Strategy must be mean-reverting in entry but momentum-confirming in filter.
---
Use an asymmetric parameter grid: longs and shorts use different lookbacks.
---
Gate the edge with a volatility regime (realized vol or ATR vs its median) or a calendar window — do NOT gate with autocorrelation or efficiency ratio.

## GUIDANCE

The default creative rotation: generic price/structure micro-constraints that force mechanical variety (no MA-crossover, breakout-only, asymmetric grids, time-boxed exits, vol-regime gating, etc.). One is chosen per non-forced slot. These are NOT an archetype — they produce plain-OHLC 'standard' strategies.

### Measured 2026-09-16 — why a shape constraint alone is not enough

Over 2026-08-27..09-15 (the only era with `slot_label`) the free creative backbone
drew 2442 theses for 1 pass; the macro rotation drew 2405 for 11. 33.6% of the
creative theses scored EXACTLY zero in-sample — they never entered — and only
38.4% cleared `MIN_IS_SCORE=0.3`, against 52.1% for macro (`validator.py:232`).

Per item, in rotation order: 'day-of-week / session only' 52.8% zero-signal and
0.56% IS+WF; 'quantile breakout' 49.4% zero and 22.4% IS; 'open-to-close range
spread' 0.0% WF pass in 240 theses; 'vol/calendar gate' 33.3% zero (it also
restates the global independent-gate rule, so it adds no variety). The one item
that pins a MECHANISM — the cross-market PAIR item, `CREATIVE[9]` — clears IS
67.3% of the time and lands level with macro at IS+WF (5.49% vs 6.74%).

Those three were rewritten in 2026-09-16 to name a mechanism and a payer rather
than a shape. Keep the mechanical variety: it decorrelates the pool, and the
pool's documented failure mode (`reviewer.md`) is monoculture. But a constraint
that only prescribes a shape hands the model no mechanism to be faithful to, and
a thesis with no mechanism is what scores zero.

Do NOT splice this GUIDANCE into the shared prompt — it is maintainer
documentation, and `_get_thesis_rules` deliberately omits 'standard' for the
same 12,000-token guardrail reason 'gap' is omitted (`auto_research.py:1620`).
