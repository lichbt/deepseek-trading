<!--
Code-generation prompt template for auto_research.py (Step B: thesis → code).
Loaded by _get_codegen_template() and filled with str.format().

Placeholders (all required): {instrument} {timeframe} {family} {hypothesis}
{entry} {filter} {exit} {param_hints}

Literal braces for JSON / set examples are escaped as {{ }} — keep them escaped.
-->
Implement this trading strategy EXACTLY as specified. Do NOT substitute generic indicators.

STRATEGY SPEC:
- Instrument:  {instrument}
- Timeframe:   {timeframe}  ← use EXACTLY this timeframe in the JSON output
- Family:      {family}
- Hypothesis:  {hypothesis}
- Entry:       {entry}
- Filter:      {filter}
- Exit:        {exit}
- Param hints: {param_hints}

Rules:
- Use ONLY pandas and numpy. No ta, talib, or external libraries.
- The Entry, Filter, and Exit conditions above are MANDATORY — implement each one literally.
- Build a param_grid sweeping the param_hints values (add ±1 variants where sensible).
- Grid size must stay ≤ 200 combinations.
- Define generate_signals(df, params) returning pd.Series of int in {{-1, 0, 1}}.
- Include explicit exit logic so the strategy exits during extended chop (no new signal after N bars).
- SINGLE TIMEFRAME ONLY: df contains bars of ONE timeframe ({timeframe}). Do NOT fetch or reference
  a different timeframe (H4/D/W/H1) inside generate_signals. Simulate higher-timeframe context
  with longer rolling windows (e.g. 200-bar MA on D ≈ 40-bar weekly MA).
- REGIME GATE (critical): the Filter condition MUST be a regime gate that switches the strategy
  OFF when its edge is not present — not a vague volatility filter. The strategy is walk-forward
  validated across 5 separate time windows and must be profitable in at least 3 of them; a
  strategy that only works in one market regime is rejected.
  * Pick a regime DETECTOR — do NOT default to ADX. Options (use the one matching the edge,
    and vary it from prior strategies): ADX(14); fast/slow MA separation abs(EMA20-EMA50)/ATR;
    MA-slope magnitude abs(SMA50 - SMA50.shift(10))/ATR; lag-1 return autocorrelation over
    30-60 bars (negative = ranging); realized vol vs its 60-bar median; abs(close - SMA50)/ATR
    (small = ranging, large = extended); efficiency ratio (net move / summed abs moves).
    Any MA type is allowed (SMA, EMA, WMA, Hull) — but WMA/Hull MUST be vectorized using
    cumulative sums or shifted-series arithmetic, NOT df.rolling(n).apply(), which is too
    slow and will hit the strategy-call timeout under grid search.
  * Mean-reversion / statistical entries (skewness, RSI extremes, kurtosis, autocorr fade): the
    edge lives in RANGING markets — gate OFF when trending, e.g. `adx < 20`,
    `autocorr(30) < 0`, or `abs(close - sma50) < 1.0*atr`.
  * Trend / breakout entries: the edge lives in TRENDING markets — gate OFF when ranging, e.g.
    `adx > 25`, MA-separation above its median, or `efficiency_ratio > 0.3`.
  * DIRECTION-AGNOSTIC: the gate classifies market STATE, never picks a direction. `close > sma`
    alone is a long-bias signal, NOT a regime gate. Wrap slopes/separations in abs(); never gate
    on the raw sign of a moving-average comparison.
  Implement the gate as a boolean Series ANDed into the entry; entries outside the regime must
  produce 0, not a position.

REGIME DETECTOR REFERENCE SNIPPETS — paste the ONE matching block INLINE inside
generate_signals. These are NOT helper functions: the output contains
generate_signals ONLY, so calling a name like `regime_autocorr(...)` raises
NameError because no such function is ever defined. Each block computes a Series
called `regime`; AND `regime <threshold>` into the entry. `df`, `params`, and any
`atr` Series are computed by you inside generate_signals first.

```python
# --- lag-1 autocorrelation of returns (negative = ranging, positive = trending)
returns = df['close'].pct_change()
regime  = returns.rolling(window).corr(returns.shift(1))

# --- Kaufman efficiency ratio over `window` bars (0 = choppy, 1 = trending)
net    = (df['close'] - df['close'].shift(window)).abs()
path   = df['close'].diff().abs().rolling(window).sum()
regime = net / path.replace(0, np.nan)

# --- MA-slope magnitude, ATR-normalised (large = trending, small = flat)
sma    = df['close'].rolling(ma_window).mean()
regime = (sma - sma.shift(slope_lag)).abs() / atr

# --- fast/slow MA separation, ATR-normalised (large = trending)
regime = (df['close'].ewm(span=fast).mean()
          - df['close'].ewm(span=slow).mean()).abs() / atr

# --- realized-vol regime: current vol vs its own median (>1 = high-vol regime)
vol    = df['close'].pct_change().rolling(window).std()
regime = vol / vol.rolling(median_window).median()

# --- distance from mean, ATR-normalised (small = ranging, large = extended)
regime = (df['close'] - df['close'].rolling(50).mean()).abs() / atr
```

These are inline calculations — do NOT use df.rolling(n).apply() equivalents
(too slow), and do NOT wrap them in a `def`. ADX you compute directly from OHLC.
Always wrap the regime threshold comparison in a boolean Series and AND it into
the entry.

- SIGNAL DENSITY (critical): the strategy MUST fire at least 15-30 signals per year of data.
  If your first-attempt threshold produces fewer signals, LOOSEN it (e.g. autocorr > 0.1 not > 0.5,
  ADX > 15 not > 25). Put the LOOSEST threshold first in each param_grid list so the grid always
  has a tradeable configuration. Never combine more than 2 simultaneous AND-conditions in the entry
  (the regime gate counts as one of the two).

Available df columns by archetype (choose one, set "archetype" key in JSON):
- standard  : close, open, high, low, date  (default — use pandas/numpy only)
- macro     : above + US series (always) fed_rate, us10y, us_real_yield, us_cpi, dxy
              + home-currency series for the instrument: ecb_rate/eu10y/eu_cpi (EUR),
              uk10y/uk_cpi (GBP), jp10y (JPY), au10y (AUD)
              (use when entry/filter depend on interest rates, yields, or CPI;
              reference only the columns listed above — others inject as NaN)
- session   : above + session ('London','New_York','Asian','Overlap','Closed')
- news      : above + event_impact ('high'/'medium'/'low'/'none'), event_surprise (float)
- pair      : above + close_leg2, spread  (also set "instrument2" key)

CRITICAL: volume, tick_count, bid, ask are NOT available. Use ONLY the columns listed above for your chosen archetype. Any reference to df["volume"], df.volume, or df["tick_count"] will cause a hard failure.
CRITICAL: NEVER use .shift(-1) or any negative shift — that reads a future bar (look-ahead bias) and will cause immediate rejection. Only .shift(1), .shift(2), etc. (past bars) are allowed.

Output EXACTLY two fenced blocks:
```python
def generate_signals(df, params):
    ...  # your implementation
```
```json
{{"param_grid": {{"param1": [v1, v2, v3], ...}}, "archetype": "standard"}}
```
No JSON wrapping of the code. No extra text.
