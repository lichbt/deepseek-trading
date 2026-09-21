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

<!-- CACHE-SPLIT
Everything BELOW this marker is static across every code-gen call and is sent as
the SYSTEM message, where the provider's prefix cache can hold it; everything
ABOVE is the per-strategy spec and goes in the user message.

Measured 2026-08-22: with the spec on top, all 8 placeholders sat in the first 2%
of the file, so the cacheable prefix was ~44 tokens and codegen ran at cached=0
while the thesis path (already laid out this way) cached 69%.

So: keep this file's static half free of format placeholders, and do not move
per-strategy text below the marker — either would break caching again.
(No braces in this comment: the whole file is still str.format()-ed by some
callers, and a stray placeholder here raises KeyError.)
Nothing below may refer to the spec as "above".
-->
Rules:
- Use ONLY pandas and numpy. No ta, talib, or external libraries.
- The Entry, Filter, and Exit conditions in the STRATEGY SPEC are MANDATORY — implement each one literally.
- IMPLEMENT EXACTLY THOSE THREE AND NOTHING MORE. Do NOT add a condition the thesis
  does not state — no extra regime gate (efficiency ratio, autocorrelation, ADX...),
  no maximum holding period or `max_hold` cap, no second exit. A fidelity critic
  compares your code against the thesis and REJECTS any invented condition, which
  discards the whole strategy. If the thesis seems to need a guard it does not
  mention, leave it out: the thesis is the contract.
- Build a param_grid sweeping the param_hints values (add ±1 variants where sensible).
- param_grid must have AT MOST 4 KEYS. This is a hard gate: a 5th parameter is
  REJECTED and the whole strategy is discarded, not trimmed. Add ±1 VALUE variants
  to the keys you were given; do NOT invent additional parameters. If the thesis
  needs more knobs than 4, hard-code the least important ones as constants.
- Grid size must stay ≤ 200 combinations.
- Define generate_signals(df, params) returning pd.Series of int in {{-1, 0, 1}}.
- The THESIS EXIT is the exit. Implement it and stop — no holding-period cap of
  your own.
- PERFORMANCE (critical — hard timeout): each generate_signals call has a 30s ceiling and runs
  hundreds of times under grid search, on series up to ~30k bars (intraday H1/M30). A per-bar
  Python loop using `.iloc[i]` scalar access (`for i in range(len(df)): position.iloc[i] = ...`)
  is ~100x too slow and WILL time out on intraday data — it is the #1 cause of code failures.
  * PREFER fully vectorized position-building (boolean masks, .where, .shift, cumsum, ffill).
  * If a loop is unavoidable to hold a position N bars, loop over the (sparse) ENTRY indices on
    NUMPY ARRAYS — never over every bar, never with `.iloc[i]`:
    ```python
    raw = np.where(entry_long, 1, np.where(entry_short, -1, 0))  # entry dir, 0 elsewhere
    pos = np.zeros(len(df), dtype=int)
    for i in np.flatnonzero(raw):      # loops over entries (few), not bars (many)
        pos[i:i+N] = raw[i]            # later entries re-arm; plain numpy indexing
    position = pd.Series(pos, index=df.index)
    ```
  * The numpy array is ONLY for that position-building loop. `.rolling`, `.shift`, `.diff`,
    `.ewm`, `.pct_change`, `.dt`/`.dt.dayofweek` exist ONLY on pandas **Series**, never on a numpy
    array — calling them on an array raises `AttributeError: 'numpy.ndarray' object has no
    attribute 'rolling'` (a top-3 failure cause) and the strategy is DISCARDED. Compute every
    indicator on a Series FIRST (`df['close'].rolling(n)`, `df['close'].diff()`); for calendar use
    the injected columns (`df['dow']`, `df['turn_of_month']`) or `pd.to_datetime(df['date']).dt`,
    NEVER `df.index.dayofweek`. If you ever hold an array and need a rolling/shift, wrap it back:
    `pd.Series(arr, index=df.index).rolling(n)`.
  * NO UNBOUNDED `while` LOOPS (the #1 timeout cause, 381 recent failures): never `while`
    on a condition that scans forward bar-by-bar to find an exit (`while j < n: ... j += 1`)
    inside a `for entry in entries` loop — that is O(n²) and times out on 30k intraday bars.
    Model the exit VECTORIZED (a stop hit is `(low <= stop)`; an N-bar hold is `pos[i:i+N]`);
    if you truly must loop, it must be over the SPARSE entry indices only, with NO inner
    per-bar loop. Every loop must have a bound that is the number of ENTRIES, not bars.
- BOOLEAN before `&`/`|` (recent: 25 `bitwise_and not supported` TypeErrors): the operands of
  `&` and `|` must be BOOLEAN Series, never float/price series. `df['close'] & df['high']` throws.
  Write comparisons first: `(df['close'] > sma) & (atr > atr.median())`. Wrap each side in parens.
- NO INVENTED pandas methods (recent: `'Series' object has no attribute 'crosses'`): pandas has
  no `.crosses()`, `.crossover()`, `.cross_above()`. Write a cross explicitly:
  cross-above = `(a > b) & (a.shift(1) <= b.shift(1))`; cross-below = `(a < b) & (a.shift(1) >= b.shift(1))`.
  `.shift()`/`.rolling()`/`.diff()` exist on a Series, NOT on a python float/np.float64 scalar.
- GUARD DIVISIONS (recent: 142 `IS non-finite: -inf`): a `/` that can hit 0 (efficiency ratio,
  RSI's rs, any ratio) produces inf/NaN and voids the score. Use `.replace(0, np.nan)` on the
  denominator (e.g. `net / path.replace(0, np.nan)`) or add a tiny epsilon.
- SINGLE TIMEFRAME ONLY: df contains bars of ONE timeframe (the one named in the STRATEGY SPEC). Do NOT fetch or reference
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
- news      : above + days_to_event, days_since_event (calendar days, capped 60),
              event_window (1 on a release day + the bar after) — economic-release
              TIMING from the FRED calendar (CPI, NFP, GDP, PPI, PCE). The schedule
              is public in ADVANCE so these are look-ahead-safe. Timing only, no
              surprise/actual value.
- spread    : above + spread (close-time bid-ask difference, price units, >=0;
              real OANDA microstructure data — use for liquidity-aware
              entries, e.g. wide spread gates mean-reversion, tight spread
              confirms breakouts)
- pair      : above + close_leg2, spread  (also set "instrument2" key)

CRITICAL: volume, tick_count, bid, ask are NOT available. Use ONLY the columns listed above for your chosen archetype. Any reference to df["volume"], df.volume, or df["tick_count"] will cause a hard failure.
CRITICAL: NEVER use .shift(-1) or any negative shift — that reads a future bar (look-ahead bias) and will cause immediate rejection. Only .shift(1), .shift(2), etc. (past bars) are allowed.
CRITICAL — POSITION MUST BE CAUSAL AT EVERY BAR (auto-rejected by a truncation test that recomputes signals on df.iloc[:t+1] and compares to the full run). Two banned patterns:
  (a) RETROACTIVE EDITING: looping over exit/stop-hit indices and writing to EARLIER pos[] indices, e.g. `for idx in stop_indices: pos[entry:idx+1] = 0`. Zeroing a trade back to its entry because a stop hit LATER erases losers with future knowledge.
  (b) SCAN-AND-FILL a hold span: for each entry, scanning FORWARD to LOCATE the exit bar, then back-filling `pos[entry:exit] = dir`. This looks causal but is NOT — until the exit is found the held bars are left 0, so mid-hold (future hidden) the signal reads FLAT and live exits the position a bar after entry, never reproducing the backtest's multi-bar hold. `for i in entries: for j in range(i+1,n): if low[j]<=stop: break; pos[i:j]=dir` is exactly this bug.
The FIX — set pos[t] from STATE known at t, not from a located exit. Prefer vectorized (trailing stop = `close - m*ATR` running-max via cummax; exit = first bar `low <= stop`). If you must loop, use ONE stateful pass over ALL bars carrying (in_position, dir, trailing_stop): at each bar update the stop, check the exit, and set pos[t] = dir while in_position else 0 — so a still-open position reads `dir` at t WITHOUT needing to know when it eventually exits. A position opened at t is fixed at t; later bars may CLOSE it going forward, but nothing may reach back or leave held bars unresolved pending a future exit.

Output EXACTLY two fenced blocks:
```python
def generate_signals(df, params):
    ...  # your implementation
```
```json
{{"param_grid": {{"param1": [v1, v2, v3], ...}}, "archetype": "standard"}}
```
No JSON wrapping of the code. No extra text.
