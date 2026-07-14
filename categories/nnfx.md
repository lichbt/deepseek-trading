<!-- nnfx.md — NNFX (No Nonsense Forex) generation category. Loaded by auto_research._category_constraint('nnfx'). Forced slot (~7% of batch). Strategies use standard OHLC data (archetype='standard'). -->
# NNFX (No Nonsense Forex) category

## CONSTRAINT

NNFX MODE: design a strategy using the No Nonsense Forex multi-layer indicator filter. The strategy MUST have ALL FOUR layers — do NOT collapse them into fewer indicators:

1. BASELINE — a SINGLE trend-direction indicator. Use a CHEAP VECTORIZED one: Kijun-Sen slope via (rolling max(26)+rolling min(26))/2, DEMA/TEMA vs price (nested ewm), linear-regression slope, or Supertrend. **DO NOT use Hull MA** — it is the #1 cause of grid-search timeouts here. It answers ONLY "trend is UP or DOWN". Entry direction MUST follow the baseline.

2. CONFIRMATION — a DIFFERENT indicator family that confirms the baseline (e.g. MACD histogram sign, Awesome Oscillator, Stochastic %K crossover, CCI > 0 / < 0, momentum oscillator). Must be INDEPENDENT of the baseline — not derived from the same MA. Entry fires ONLY when baseline AND confirmation AGREE on direction.

3. VOLUME/MOMENTUM FILTER — since tick volume is unavailable, use a VOLATILITY or MOMENTUM proxy as the filter_condition: ATR(14) > its N-bar median, ADX(14) > threshold, Bollinger Band width expansion, or range expansion ratio. This gates OUT low-energy noise periods. Keep this gate LOOSE (e.g. ADX>15 not >25, ATR above its ~40th percentile) — combined with the baseline AND confirmation AND, a tight gate here STARVES entries to zero (a top failure mode). It filters noise, it must not block most bars.

4. EXIT — an ATR-based trailing stop OR an independent exit indicator (Chandelier Exit logic, opposite Parabolic SAR flip, a DIFFERENT oscillator crossing its midline). The exit MUST be separate from the entry indicators — "opposite of entry signal" alone is NOT acceptable; at minimum add an ATR trailing stop.

Combine layers in the output: entry_condition = "baseline direction + confirmation agreement" (layers 1+2), filter_condition = "volume/momentum gate" (layer 3), exit_condition = "independent exit mechanism" (layer 4). PERFORMANCE (HARD RULES — violations time out and are auto-discarded): (a) NEVER call `.rolling(n).apply(...)` anywhere — it is the #1 timeout cause. Every indicator here is expressible with `.ewm()`, `.rolling(n).mean()/.std()/.median()/.max()/.min()`, `.diff()`, `.shift()`, `np.where` only. (b) NO Hull MA (see baseline). (c) `.rolling(n)` is a Rolling object — call `.mean()/.std()/.sum()` before arithmetic (`.rolling(n) * 2` is a TypeError; `.rolling(n).mean() * 2` is correct). This is a standard-archetype strategy (OHLC only).

## GUIDANCE

## NNFX (No Nonsense Forex) — multi-layer indicator filtering

The NNFX method layers INDEPENDENT indicators so each layer filters false
signals from the previous one. This produces fewer but higher-conviction entries
compared to single-indicator strategies.

### Layer structure

- **Baseline** (trend direction): a slow, smooth indicator whose slope or
  position relative to price determines the primary trend. Popular choices
  implementable in pandas/numpy:
  - Hull Moving Average: `WMA(2×WMA(n/2) − WMA(n), √n)` — compute WMA via
    `(weights * series).rolling(n).sum() / weights.rolling(n).sum()` with
    `weights = pd.Series(range(1, n+1))`
  - Kijun-Sen: `(df['high'].rolling(26).max() + df['low'].rolling(26).min()) / 2`
  - DEMA: `2×EMA(n) − EMA(EMA(n), n)`
  - Linear regression slope: `df['close'].rolling(n).apply(lambda x: np.polyfit(range(len(x)),x,1)[0])`
    — CAUTION: `.apply()` is slow; prefer the vectorized OLS formula

- **Confirmation** (signal validation): a momentum or trend oscillator from a
  DIFFERENT family. If the baseline uses a moving average, the confirmation
  should use an oscillator (or vice versa):
  - MACD histogram: `EMA(12) − EMA(26)` minus its 9-period EMA
  - Awesome Oscillator: `SMA(median_price, 5) − SMA(median_price, 34)`
  - Stochastic %K: `(close − lowest(14)) / (highest(14) − lowest(14))`
  - CCI: `(typical_price − SMA(tp, 20)) / (0.015 × MAD(tp, 20))`

- **Volume/momentum filter**: with no reliable tick volume in FX, use volatility
  expansion as a proxy for conviction:
  - ATR above its rolling median: `atr > atr.rolling(60).median()`
  - ADX above threshold: `ADX(14) > 20`
  - Bollinger bandwidth expansion: `(upper − lower) / sma > threshold`

- **Exit**: NNFX emphasises ATR-based exits over indicator-cross exits because
  ATR adapts to volatility:
  - ATR trailing stop: track `entry_price ± N×ATR` and exit when price crosses
  - Chandelier: `highest(22) − 3×ATR(22)` for longs
  - A distinct oscillator crossing its midline (different from confirmation)

### Why this category diversifies the pool

Standard creative-rotation strategies use one entry indicator + one regime gate.
NNFX forces FOUR independent layers, creating a fundamentally different signal
profile — fewer trades, stronger conviction, and natural regime adaptation
through the multi-layer filter chain. This structural diversity complements the
existing mean-reversion and trend-following monocultures.
