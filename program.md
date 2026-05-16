# Trading Strategy Autoresearch

## Objective
Discover simple, economically grounded trading strategies that survive rigorous out-of-sample validation.
Improve metrics by proposing genuinely different hypotheses — not by tweaking backtest parameters.

## Single Yardstick
All strategies are evaluated on the GT-Score only. No other metric matters.

## Knowledge Boundary
Reasoning is based on pre-2020 financial principles: behavioural finance, market microstructure, classical anomalies.
Never reference specific post-2019 events or correlation patterns.

## Strategy Families
- **speed-based**: gap fades, turn-of-month, session transitions
- **cross-market**: cross-sectional momentum, intermarket, value (PPP)
- **regime**: volatility regime switching, Hurst filter, breakout/trend
- **flow-proxy**: bar-imbalance estimation, stop-run anticipation, sentiment divergence
- **event-driven**: news straddle, post-news fade, surprise normalisation
- **statistical**: autocorrelation signals, skewness/kurtosis, cointegration
- **risk-factor**: carry+momentum hybrid, VRP harvesting, tail-hedge overlays

## Current Research Phase (Auto-Generated)
<!-- RESEARCH_PHASE_START -->
- Use D timeframe with breakout of 20-bar Donchian channel and 2-bar confirmation.
- Combine H1 RSI(2) < 10 for entry with H4 50-bar SMA trend filter.
- Switch to W timeframe with 5-bar high/low break and ADX(14) > 25.
<!-- RESEARCH_PHASE_END -->
