# Thesis Generation Rules

## Strategy Families — pick the one that best fits the edge

| Family | What it is | Example entries |
|---|---|---|
| **regime** | Trade the direction of a sustained trend or volatility regime | Donchian breakout, ATR expansion entry, Hurst > 0.6 filter |
| **statistical** | Exploit measurable statistical properties of returns | Rolling skewness < -0.5 reversal, lag-1 autocorr momentum, kurtosis spike fade |
| **flow-proxy** | Proxy for order-flow imbalance without tick data | Large bar range relative to 50-bar median ATR, inside-bar breakout above prior high, gap-fill reversion |
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
- `filter_condition` — regime gate with exact numeric threshold (see "Regime gating" below)
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

## Regime gating — MANDATORY for mean-reversion / statistical strategies

Every edge only works in *some* market regimes. A mean-reversion strategy that
trades unconditionally makes money in ranging markets and gives it all back in
trending ones — its walk-forward score is then one good window averaged against
several zero windows, and it fails validation. The validator requires an edge to
show up in **at least 3 separate walk-forward windows**, so a strategy that only
works in one regime will be rejected.

**The `filter_condition` must be a regime gate that turns the strategy OFF when
its edge is not present.** It is not a vague "volatility filter" — it is the
specific condition under which the edge is alive.

### Regime detectors — pick one (do NOT default to ADX)

A regime detector measures the *state* of the market — trending vs ranging,
high-vol vs low-vol — as a single numeric condition. Choose whichever fits the
edge; vary it across theses so the research pool is not all ADX:

- **Trend strength** — `ADX(14)`, OR fast/slow MA *separation*
  `abs(EMA(20) − EMA(50)) / ATR`, OR MA-slope magnitude
  `abs(SMA(50) − SMA(50).shift(10)) / ATR`.
- **Mean-reversion strength** — lag-1 return autocorrelation over 30–60 bars
  (negative = mean-reverting, positive = trending). This measures the edge
  directly and is the cleanest gate for reversion strategies.
- **Volatility regime** — realized vol vs its 60-bar median, OR
  ATR vs its 50-bar median, OR Bollinger-band width vs its median.
- **Range vs extension** — distance of price from a long MA as a *magnitude*:
  `abs(close − SMA(50)) / ATR` (small = ranging, large = extended).
- **Persistence** — efficiency ratio (net move / sum of absolute moves over N
  bars) or a Hurst-style measure: high = trending, low = choppy.

### How to gate

- **Mean-reversion / statistical (skewness, RSI extremes, kurtosis, autocorr fade):**
  the edge lives in *ranging* markets. Gate it OFF when the market trends, e.g.
  `ADX(14) < 20`, `autocorr(30) < 0`, or `abs(close − SMA(50)) < 1.0×ATR`.

- **Trend / breakout / regime:** the edge lives in *trending* markets. Gate it
  OFF when the market ranges, e.g. `ADX(14) > 25`, `EMA(20)−EMA(50) separation
  above its median`, or `efficiency ratio > 0.3`.

### Rules for the gate

- **Direction-agnostic.** A regime gate classifies market *state*; it must NOT
  pick a *direction*. `close > SMA(200)` alone is NOT a regime gate — it is a
  long-bias directional signal. `abs(close − SMA(200)) > 1.5×ATR` IS a valid
  gate (extended in either direction). Slopes and separations must be wrapped
  in `abs()`; never gate on the raw sign of a moving-average comparison.

- **Symmetric with the edge.** If entry is "fade an extreme", the gate must
  confirm the market is mean-reverting *right now* — not just "volatility is
  high". High volatility inside a strong trend is exactly when a reversion
  strategy loses the most.

State the regime gate as a precise numeric condition in `filter_condition`.

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

- **Never use open-to-close direction as an entry signal.** `close > open` (bullish bar)
  is not an edge — it forces entry AFTER the move has happened and creates long bias >60%
  on trending assets like XAU, BTC, BCO. This pattern is permanently banned.

- **Never propose a purely directional strategy on XAU_USD, XAG_USD, BTC_USD, ETH_USD.**
  These instruments have structural upward drift. Any strategy that is net-long >60% of
  bars is capturing beta, not an edge. Use mean-reversion, regime-switch, or
  cross-market signals on these instruments instead.

- **Never run a mean-reversion entry without a regime gate.** A skewness/RSI/kurtosis
  reversion that trades in every market state will win in ranging windows and lose in
  trending ones, scoring 0 on most walk-forward windows. The `filter_condition` MUST
  restrict it to the ranging regime (see "Regime gating" above).

## Current Research Directives
<!-- RESEARCH_PHASE_START -->
- D timeframe: mean-reversion entries (skewness, RSI extremes) gated by ADX(14) < 20 so they only fire in ranging regimes.
- D timeframe: Donchian(20) breakout gated by ADX(14) > 25 — trend edge restricted to the trending regime.
- Prefer instruments with weak directional drift (EUR_USD, GBP_USD, EUR_GBP) over structurally trending ones.
<!-- RESEARCH_PHASE_END -->
