# Thesis Generation Rules

## Strategy Families — pick the one that best fits the edge

| Family | What it is | Example entries |
|---|---|---|
| **regime** | Trade the direction of a sustained trend or volatility regime | Donchian breakout, ATR expansion entry, Hurst > 0.6 filter |
| **statistical** | Exploit measurable statistical properties of returns | Rolling skewness < -0.5 reversal, lag-1 autocorr momentum, kurtosis spike fade |
| **flow-proxy** | Proxy for order-flow imbalance without tick data | Large bar range relative to history, open-to-close vs prior range midpoint, inside-bar breakout |
| **speed-based** | Calendar or session timing anomalies | Day-of-week effect, month-end rebalancing, session-open gap fade |
| **risk-factor** | Carry, volatility risk premium, or macro factor exposure | High-yield vs low-yield FX carry, ATR contraction before expansion, VRP mean reversion |
| **cross-market** | Signal from a related instrument or spread | DXY vs gold inverse, AUD/USD vs iron ore proxy, EUR/USD vs EUR/JPY divergence |
| **event-driven** | Trade around scheduled macro events or surprise releases | Pre-NFP volatility contraction, post-CPI fade, central bank day patterns |

**Pick the family that matches the economic edge, not the indicator used.**
Donchian breakout = regime. Skewness reversal = statistical. Monday gap = speed-based.

## Output Format
Each thesis is ONE JSON object with exactly these keys:
- `instrument` — FX pair or commodity (e.g. "EUR_USD")
- `strategy_family` — one of: speed-based, cross-market, regime, flow-proxy, event-driven, statistical, risk-factor
- `timeframe` — one of: M30, H1, H4, D, W
- `rationale` — one sentence: WHY this edge exists economically
- `entry_condition` — exact measurable trigger: indicator name, lookback, threshold
- `filter_condition` — regime or volatility gate with exact numeric threshold
- `exit_condition` — how to exit: ATR multiple OR fixed bar count OR indicator cross
- `param_hints` — dict of param → list of sweep values, LOOSEST value first

## DOS ✓

- **One timeframe only.** Pick D, H4, H1, M30, or W. Use it for EVERYTHING — entry, filter, exit.
  Express higher-TF context as longer windows: 200-bar MA on D ≈ weekly context.

- **Specific thresholds.** Write exact numbers: `ADX(14) > 20`, `ATR > 50-bar median ATR`, `skewness < -0.3`.
  Vague conditions like "when trend is strong" will be rejected.

- `param_hints` **loosest first.** The first value in each list must fire at least 15 signals in 6 months.
  Example: `{"adx_thresh": [15, 20, 25]}` — not `[25, 20, 15]`.

- **Max 2 AND conditions in entry.** More than 2 simultaneous conditions kills signal density.
  Good: `close > Donchian(20) AND ADX(14) > 15`
  Bad: `close > Donchian(20) AND ADX > 25 AND ATR > median AND skew < -0.3`

- **State the exit precisely.** Choose one: time-based (`exit after N bars`), ATR-stop (`1.5× ATR(14)`),
  or indicator-cross (`exit when RSI crosses 50`). Do not leave it vague.

- **Economic rationale first.** The rationale must explain WHY the edge exists, not WHAT the rule is.
  Good: "Institutional re-balancing at month-end creates predictable USD demand."
  Bad: "Enter when RSI is low."

## DON'TS ✗

- **Never mix timeframes.** Do not write "daily entry with weekly filter" or "H1 entry, D trend".
  All indicators in one thesis must reference the same timeframe.

- **Never reference volume, tick count, bid, or ask.** These columns do not exist in the data.

- **Never use shift(-1) or future data.** Only past bars: shift(1), shift(2), etc.

- **Never combine more than 2 conditions with AND in the entry signal.**
  Every AND you add halves the signal count.

- **Never use param_hints with only one value per param.** Each param needs at least 2–3 sweep values
  so the validator can find a working configuration.

- **Never propose the same strategy structure twice in one batch.**
  Each thesis must use a mechanically different entry logic.
