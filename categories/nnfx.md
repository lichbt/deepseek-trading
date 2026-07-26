<!-- nnfx.md — NNFX (No Nonsense Forex) generation category. Loaded by auto_research._category_constraint('nnfx'). Forced slot (~2% of batch). Strategies use standard OHLC data (archetype='standard'). -->
# NNFX (No Nonsense Forex) category

## CONSTRAINT

NNFX MODE: design a lean No Nonsense Forex strategy with TWO mandatory independent layers and ONE optional regime filter:

1. BASELINE — one cheap vectorized direction indicator: EMA slope, Donchian midpoint slope, vectorized linear-regression slope, or Kijun-Sen. Rotate choices; DO NOT default to Kijun-Sen.

2. CONFIRMATION — one independent momentum family: Fisher transform, ROC, CCI, Stochastic, or Awesome Oscillator. It must not derive from the baseline family. DO NOT default to MACD.

3. OPTIONAL VOLATILITY/STRUCTURE FILTER — only when it does not starve entries: Bollinger-width percentile, realized-volatility percentile, or ATR percentile. Do not require this fourth gate.

EXIT — baseline cross or confirmation reversal. The validator owns the ATR stop through `compute_returns_with_stop`; generated code MUST NOT implement ATR/Chandelier trailing-stop state, per-bar position loops, or entry-price tracking.

Default example: EMA-slope baseline + Fisher confirmation + optional Bollinger-width regime; exit on baseline cross or Fisher reversal. Use no more than 3 tunable parameters for this example.

Role orthogonality is mandatory: direction baseline + momentum confirmation + optional volatility/structure filter. Do not generate the repeated Kijun+MACD+ADX template. HARD LIMITS: deterministic code, at most 4 tunable parameters, at most 200 original grid combinations. PERFORMANCE: never call `.rolling(n).apply(...)`; use vectorized `.ewm()`, rolling reductions, `.diff()`, `.shift()`, and `np.where`. This is a standard-archetype strategy (OHLC only).

## GUIDANCE

## NNFX (No Nonsense Forex) — multi-layer indicator filtering

The NNFX method combines independent indicator roles. Here, two orthogonal
signal layers are mandatory; a third regime filter is optional to preserve
enough trades for walk-forward testing.

### Layer structure

- **Baseline**: EMA slope, Donchian midpoint slope, vectorized linear-regression
  slope, or Kijun-Sen. Rotate families instead of repeating Kijun.
- **Confirmation**: Fisher transform, ROC, CCI, Stochastic, or Awesome
  Oscillator. It must measure momentum independently from the baseline.
- **Optional regime filter**: Bollinger bandwidth percentile, realized-volatility
  percentile, or ATR percentile. Omit it when it starves signals.
- **Exit**: baseline cross or confirmation reversal. The validator applies the
  common ATR stop model; generated signal code stays stateless and vectorized.

### Why this category diversifies the pool

The category combines direction and momentum families while rotating each role.
The optional volatility layer changes market selection without forcing every
strategy into the former Kijun+MACD+ADX+ATR template.
