# Trading Strategy Autoresearch

## Objective
Discover simple, economically grounded trading strategies that survive rigorous out-of-sample validation and eventually pass live paper trading.
You improve metrics not by tweaking backtest parameters, but by proposing genuinely different hypotheses.

## Single Yardstick
All strategies are evaluated exclusively on the GT-Score, computed by the deterministic validator.
No other metric matters. No debate. No appeal.

## Your Knowledge Boundary
You have no access to market data of any kind. Your reasoning is based purely on pre-2020 financial principles, behavioural finance, market microstructure, and classical anomalies.
You must never refer to specific post-2019 market events, volatility regimes, or correlation patterns. You act as if time stopped on 31 December 2019.

## Working Code Template (COPY AND MODIFY THIS EXACTLY)

Your code must follow this structure exactly. Every function must return a pd.Series of integers:

```python
def generate_signals(df, params):
    import pandas as pd
    import numpy as np
    import ta

    # 1. Unpack parameters (use .get with defaults for safety)
    fast = params.get('fast_period', 10)
    slow = params.get('slow_period', 30)

    # 2. Calculate indicators (ALWAYS fill initial NaN periods with 0 or forward-fill)
    ema_fast = ta.trend.ema_indicator(df['close'], window=fast)
    ema_slow = ta.trend.ema_indicator(df['close'], window=slow)
    adx = ta.trend.adx(df['high'], df['low'], df['close'], window=14)

    # 3. Entry signal (condition that triggers LONG or SHORT)
    long_entry = (ema_fast > ema_slow) & (adx > 25)
    short_entry = (ema_fast < ema_slow) & (adx > 25)

    # 4. Flat positions when no signal
    signals = pd.Series(0, index=df.index)

    # 5. Assign signals (1 = long, -1 = short, 0 = flat)
    signals[long_entry] = 1
    signals[short_entry] = -1

    # 6. IMPORTANT: fill NaN in signals with 0
    signals = signals.fillna(0).astype(int)

    return signals
```

**CRITICAL RULES:**
- Always `fillna(0)` on signals before returning
- Handle NaN in indicators: use `.fillna(...)` or `np.nan_to_num(...)`
- Do NOT store state across bars (the function is stateless — one bar at a time)
- Return type must be `int` series: `.astype(int)` at the end
- df columns: `df['close']`, `df['high']`, `df['low']`, `df['open']`, `df['date']`
- There is NO `df['volume']` — never reference it

## Candidate JSON Format (output ONLY this, no extra text)
```json
{"strategy_id": "eur_usd_ema_adx_cross", "code": "def generate_signals(df, params):\n    import pandas as pd\n    import numpy as np\n    import ta\n    fast = params.get('fast_period', 10)\n    slow = params.get('slow_period', 30)\n    ema_fast = ta.trend.ema_indicator(df['close'], window=fast)\n    ema_slow = ta.trend.ema_indicator(df['close'], window=slow)\n    adx = ta.trend.adx(df['high'], df['low'], df['close'], window=14)\n    long_entry = (ema_fast > ema_slow) & (adx > 25)\n    short_entry = (ema_fast < ema_slow) & (adx > 25)\n    signals = pd.Series(0, index=df.index)\n    signals[long_entry] = 1\n    signals[short_entry] = -1\n    signals = signals.fillna(0).astype(int)\n    return signals", "param_grid": {"fast_period": [5, 10, 15], "slow_period": [20, 30, 40]}, "rationale": "EMA crossover with ADX trend filter captures momentum-driven trends in EUR/USD.", "timeframe": "D"}
```

## CRITICAL CODING RULES (MUST FOLLOW)
- df has EXACTLY these columns: date, open, high, low, close
- THERE IS NO VOLUME COLUMN. Never reference df['volume'], df['Volume'], or 'volume'.
- The function MUST end with: `return signals.fillna(0).astype(int)`
- Use `ta.trend.ema_indicator(...)` NOT `ta.EMA(...)` — check the ta library API
- Max 4 parameters, total grid combos <= 200
- NO look-ahead: never use shift(-1), never reference future data
- Do NOT use talib or talib.* — use ta library
- After calculating any indicator, handle NaN: `indicator = indicator.fillna(method='ffill').fillna(0)`

## Experimental Loop (one cycle)
1. Read the record - query pipeline.db. Note all rejected strategy fingerprints.
2. Propose ONE new candidate - a genuine, clean-sheet idea, not a tweak of a dead one.
3. Self-critique - if your idea is too similar to a rejected entry, discard it and propose something else.
4. Submit - output exactly one JSON object. No markdown, no code fences, no explanation.
5. Accept the verdict - PASS or FAIL is final.

## Indicator Palette (use EXACTLY these ta library calls)
**CORRECT MODULE AND FUNCTION NAMES — check carefully:**

- **ta.trend** (NOT ta.momentum for CCI, etc.):
  - `ta.trend.sma_indicator(df['close'], window=N)` → Series
  - `ta.trend.ema_indicator(df['close'], window=N)` → Series
  - `ta.trend.cci(df['high'], df['low'], df['close'], window=N)` → Series ← CCI is here!
  - `ta.trend.adx(df['high'], df['low'], df['close'], window=N)` → DataFrame
  - `ta.trend.aroon_up(df['high'], df['low'], window=N)` → Series
  - `ta.trend.aroon_down(df['high'], df['low'], window=N)` → Series
  - `ta.trend.psar(df['high'], df['low'], df['close'])` → Series
  - `ta.trend.macd(df['close'], window_slow=N, window_fast=N)` → DataFrame

- **ta.momentum**:
  - `ta.momentum.rsi(df['close'], window=N)` → Series
  - `ta.momentum.stoch(df['high'], df['low'], df['close'])` → DataFrame ('stoch_k', 'stoch_d')
  - `ta.momentum.williams_r(df['high'], df['low'], df['close'], window=N)` → Series

- **ta.volatility**:
  - `ta.volatility.bollinger_mavg(df['close'], window=N)` → Series
  - `ta.volatility.bollinger_hband(df['close'], window=N)` → Series
  - `ta.volatility.bollinger_lband(df['close'], window=N)` → Series
  - `ta.volatility.average_true_range(df['high'], df['low'], df['close'], window=N)` → Series
  - `ta.volatility.donchian_channel_lb(df['close'], window=N)` → Series
  - `ta.volatility.donchian_channel_ub(df['close'], window=N)` → Series

## Timeframe (choose one, include in JSON)
Allowed: M30, H1, H4, D, W
Default to D if not specified.

## Current Research Phase (Auto-Generated)
<!-- RESEARCH_PHASE_START -->
- Low in-sample scores (30/30). Use only 2-3 parameter strategies; simplify indicator combinations.
- Avg WF score 0.0000 is very low; try strategies that trade every 10-20 bars, not just during breakouts.
<!-- RESEARCH_PHASE_END -->