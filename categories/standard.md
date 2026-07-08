<!-- standard.md — single source for the standard generation category. Loaded by auto_research._category_constraint('standard'). Loaded as a LIST: each item below is separated by a line containing only '---'. Order matters (rotation index). The cross-market PAIR constraint lives in pair.md, not here, and is appended to the rotation at load time. -->
# Standard (creative default) category

## CONSTRAINT

Must avoid all moving-average crossover logic. Use price-relative or range-based entry instead.
---
Entry must be a directional momentum/continuation signal — trade WITH the move, not a fade. Do NOT use mean-reversion, skewness, or autocorrelation.
---
Use only day-of-week or time-of-session effects — no rolling indicator windows.
---
Build a spread strategy using the open-to-close range as the signal — no second instrument needed.
---
Exit must be purely time-based (fixed bar count). No price-based stop.
---
Entry only on breakout above/below a quantile of the last N bars' range.
---
Strategy must be mean-reverting in entry but momentum-confirming in filter.
---
Use an asymmetric parameter grid: longs and shorts use different lookbacks.
---
Gate the edge with a volatility regime (realized vol or ATR vs its median) or a calendar window — do NOT gate with autocorrelation or efficiency ratio.

## GUIDANCE

The default creative rotation: generic price/structure micro-constraints that force mechanical variety (no MA-crossover, breakout-only, asymmetric grids, time-based exits, vol-regime gating, etc.). One is chosen per non-forced slot. These are NOT an archetype — they produce plain-OHLC 'standard' strategies.
