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

    # --- Unpack parameters ---
    rsi_window = params.get('rsi_window', 14)
    trend_ma = params.get('trend_ma', 50)
    atr_period = params.get('atr_period', 14)
    max_bars = params.get('max_bars', 10)

    # --- Calculate indicators ---
    rsi = ta.momentum.rsi(df['close'], window=rsi_window)
    sma = ta.trend.sma_indicator(df['close'], window=trend_ma)
    atr = ta.volatility.average_true_range(df['high'], df['low'], df['close'], window=atr_period)

    # --- Layer 1: Entry signal ---
    long_entry = rsi < params.get('oversold', 30)
    short_entry = rsi > params.get('overbought', 70)

    # --- Layer 2: Trend filter ---
    uptrend = df['close'] > sma
    downtrend = df['close'] < sma

    # --- State for exits (optional but recommended) ---
    pos = 0
    entry_price = 0.0
    entry_bar = 0

    # --- Combine entry + filter, then apply exit logic ---
    signal = pd.Series(0, index=df.index, dtype='int')

    for i in range(len(df)):
        bar_signal = signal.iloc[i - 1] if i > 0 else 0

        if bar_signal == 0:
            # No position — check for new entry
            if long_entry.iloc[i] and uptrend.iloc[i]:
                pos = 1
                entry_price = df['close'].iloc[i]
                entry_bar = i
            elif short_entry.iloc[i] and downtrend.iloc[i]:
                pos = -1
                entry_price = df['close'].iloc[i]
                entry_bar = i
        else:
            # In position — check exits
            bars_held = i - entry_bar
            # Time exit
            if bars_held >= max_bars:
                pos = 0
            else:
                # ATR-based stop
                if pos == 1:
                    stop = entry_price - params.get('stop_mult', 2.0) * atr.iloc[i]
                    if df['low'].iloc[i] <= stop:
                        pos = 0
                elif pos == -1:
                    stop = entry_price + params.get('stop_mult', 2.0) * atr.iloc[i]
                    if df['high'].iloc[i] >= stop:
                        pos = 0

        signal.iloc[i] = pos

    return signal.fillna(0).astype(int)
```

**WHY EXIT CONDITIONS MATTER:**
Simply being long or short 100% of the time results in near-50% win rate and tiny returns that get eaten by spread.
You MUST include exit logic to prevent holding through market chop.
Examples:
- Reverse on opposite signal (shown above)
- Use hold_bars parameter: exit after N bars in profit

**CRITICAL RULES:**
- Always `fillna(0)` on signals before returning
- Handle NaN in indicators: use `.fillna(...)` or `np.nan_to_num(...)`
- **YOU MUST HAVE EXIT LOGIC** — never stay long/short 100% of the time!
  - Reverse on opposite signal: `short_entry = ema_fast < ema_slow` flips to short when trend flips
  - This prevents holding through chop and is the main reason strategies fail
- Do NOT store state across bars (the function is stateless — one bar at a time)
- Return type must be `int` series: `.astype(int)` at the end
- df columns: `df['close']`, `df['high']`, `df['low']`, `df['open']`, `df['date']`
- There is NO `df['volume']` — never reference it

## Candidate JSON Format (output ONLY this, no extra text)
```json
{
  "strategy_id": "descriptive_id_with_version",
  "code": "def generate_signals(df, params):\n    ...",
  "param_grid": {"param1": [val1, val2], "param2": [val3, val4]},
  "rationale": "One-sentence economic hypothesis.",
  "timeframe": "D",
  "self_critique": {
    "why_this_might_be_noise": "Honest weakness.",
    "what_would_disprove_this": "Specific market condition.",
    "similar_already_rejected": ["id1", "id2"]
  }
}
```

## CRITICAL CODING RULES (MUST FOLLOW)
- df has EXACTLY these columns: date, open, high, low, close
- THERE IS NO VOLUME COLUMN. Never reference df['volume'], df['Volume'], or 'volume'.
- The function MUST end with: `return signals.fillna(0).astype(int)`
- Use `ta.trend.ema_indicator(...)` NOT `ta.EMA(...)` — check the ta library API
- Max 4 parameters, total grid combos <= 200
- **param_grid design**: Each parameter must directly control the strategy's entry or exit logic. Do NOT include parameters that don't affect signals.
- **Grid size check**: Calculate total combos before submitting. For 4 params with 5 values each = 625 combos (TOO MANY). Keep param values sparse — 2-4 values per param is usually sufficient.
- NO look-ahead: never use shift(-1), never reference future data
- Do NOT use talib or talib.* — use ta library
- After calculating any indicator, handle NaN: `indicator = indicator.ffill().fillna(0)`
- **MUST have entry + trend filter (two-layer template)**
- **MUST have exit logic** to prevent holding forever: time-based (max_bars) or price-based (ATR stop)

## Parameter Grid Design (CRITICAL — how to choose params)
The param_grid must match the strategy's logic, not be copy-pasted from examples.

Good param_grid (mean-reversion strategy):
```json
"param_grid": {
  "rsi_period": [10, 14, 20],        // entry condition parameter
  "oversold": [25, 30, 35],          // entry threshold
  "trend_ma": [50, 100, 200],        // trend filter parameter
  "max_bars": [8, 12, 16]            // exit parameter
}
```
Bad param_grid (too many params, includes unused params):
```json
"param_grid": {
  "rsi_period": [14],
  "atr_period": [14],        // ATR used in code but not for entry — remove it
  "sma_period": [20],        // never referenced in strategy logic
  "rsi_overbought": [70]     // only oversold used — include both or neither
}
```

Rule: if a parameter is not actively used in an `if` condition or `params.get()`, it should NOT be in the grid.

## Self-Critique Requirements (MUST INCLUDE in JSON output!)
- `why_this_might_be_noise`: Honest weakness description
- `what_would_disprove_this`: Specific market condition that would falsify the hypothesis
- `similar_already_rejected`: List strategy_ids from pipeline.db that were rejected (check DB before proposing)
- If new idea is similar to any in DB, discard it and propose a different one

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
- Regime silence dominant (28/30 failed with WF=0). Switch to H4 timeframe for shorter holding periods and more trading opportunities.
- Avg WF score 0.0000 is very low; try strategies that trade every 10-20 bars, not just during breakouts.
<!-- RESEARCH_PHASE_END -->