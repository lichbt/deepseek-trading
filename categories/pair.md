<!-- pair.md — single source for the pair generation category. Loaded by auto_research._category_constraint('pair'). Injected into the creative rotation (appended after the standard list) AND used by the code-level pair guard. Keep the 'instrument2 ... DISCARDED' language — it is what drives compliance. -->
# Cross-market / pair category

## CONSTRAINT

Cross-market PAIR: trade the SPREAD/RATIO between the instrument and a SECOND tradeable OANDA instrument (e.g. XAU_USD vs XAG_USD, EUR_USD vs EUR_JPY, AUD_USD vs XCU_USD). You MUST set the "instrument2" field to that second instrument's OANDA symbol (INSTRUMENT_UNDERSCORE format, a REAL instrument, never a ratio like ETH_BTC) — the entry/exit reference close_leg2 / the spread. A pair thesis WITHOUT the instrument2 field is DISCARDED. (For a macro FACTOR instead of a pair — DXY, a rate differential — use the macro archetype, not this.)

## GUIDANCE

## Cross-market / pair data — available for divergence & relative-value theses

A **pair archetype** fetches a SECOND instrument alongside the primary and injects
its price plus the relationship, so the edge trades the SPREAD between two markets
rather than one price in isolation. This is a genuinely DIFFERENT mechanism from the
price-only families (it is currently under-used — favour it) — use it for lead-lag,
relative-value, and divergence edges.

Columns added (aligned by date to the primary's OHLC):

- `close_leg2` — the second instrument's close
- `spread` — the primary/second price ratio (leg1 / leg2)

To use it: set `strategy_family: "cross-market"` AND set `instrument2` to the related
market (REQUIRED — the code generator sets `archetype: "pair"` and fetches it; without
`instrument2` the archetype errors). Then write the signal on `spread` / `close_leg2`
in `entry_condition` / `filter_condition` — e.g. "spread z-score > 2σ over 60 bars →
fade the divergence", "close_leg2 breaks out but the primary lags → follow the leader".

Concrete pairs from the tradable universe (pick economically-linked legs):
- `XAU_USD` vs `XAG_USD` — gold/silver ratio mean-reversion
- `AUD_USD` vs `XCU_USD` — commodity currency vs copper
- `WTICO_USD` vs `USD_CAD` — oil vs the oil-linked loonie
- `NAS100_USD` vs `SPX500_USD` — index relative-value / lead-lag
- `EUR_USD` vs `EUR_JPY` — EUR-cross divergence

The direction-agnostic and regime-gating rules still apply: gate the spread signal on
a market state (volatility regime, a correlation-stability window) and make it
two-sided (fade divergences BOTH ways).

**⚠ Common failure (from live data): a missing or invalid `instrument2` DISCARDS
the strategy.** If any condition uses `close_leg2`/`spread` you MUST set
`instrument2` to a REAL tradeable OANDA symbol in underscore format (`XAG_USD`,
`EUR_JPY`) — NEVER a derived/ratio name like `ETH_BTC`, `GOLD`, or `SPX`, which
don't exist as instruments and fail to load. Both legs must have overlapping
history (crypto only goes back ~2019). **Worked example:** `instrument2:"XAG_USD"`,
`entry` = LONG when `spread` (XAU/XAG) z-score over 60 bars < −2 / SHORT when > +2,
`filter` = realized-vol below its 60-bar median (stable-correlation regime),
exit when z-score crosses 0.
