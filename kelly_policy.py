"""Single source of truth for the Kelly sizing overlay.

Both books size positions with a Kelly multiplier, and until now each carried its
own copy of the constants and its own transcription of the formula —
fix_runner._rolling_kelly and live_test._update_kelly. Measured 2026-07-31 they
agreed on all 24 tradeable sleeves, but nothing enforced that: the two sets of
constants could drift apart silently, and the only symptom would be the prop book
and the paper book sizing the same sleeve differently.

WHY THE POLICY LIVES HERE AND THE VALUE DOES NOT. The obvious alternative is to
have portfolio.py compute Kelly and ship it in portfolio_state.json beside
decay_kelly_scale. That would be worse: portfolio_state.json is git-tracked and
reaches the pod ONLY via a push (the pod never runs portfolio.py), and both books
read it once at process start. Kelly would go from recomputed-every-pass to
updated-on-deploy — days of staleness on the single largest sizing lever, since
Kelly multiplies the risk fraction directly while conviction and weight merely
redistribute inside an already-pinned cluster cap. So each book still computes
locally every pass; identical formula + identical params + identical inputs is
what makes them agree, not a shared file.

The multiplier is BINARY by design (2.0 or 0.5), not continuous — a recompute is
a 4x swing, so the edge cases below are risk decisions, not formatting.
"""
from typing import Optional, Sequence

import numpy as np

# --- policy ---------------------------------------------------------------

# DISABLED 2026-07-31. The overlay is Sharpe-neutral — it buys return by taking
# proportionally more risk — and the daily margin is the binding constraint on a
# 3% wall where one breach is an instant DQ.
#
# Cadence sweep on the real-sized 25-sleeve book (2024-01-01 -> 2026-07-29, same
# book and window, ONLY the Kelly setting varying):
#
#   config      worst day   margin   maxDD    return   Sharpe   days<-2%
#   disabled      -1.75%    +1.25   -4.45%    48.03%    2.07        0
#   every 63      -2.13%    +0.87   -5.19%    57.01%    2.08        2
#   every 21      -2.16%    +0.84   -5.43%    55.94%    2.02        2
#   every  1      -2.44%    +0.56   -6.14%    49.95%    1.83        2
#
# Disabled more than doubles the margin (0.56 -> 1.25 pp) and is the ONLY config
# with no day below -2%; Sharpe is unchanged (2.07 vs 2.02-2.08). Cost is ~8 pp of
# return over 2.5 years. Differences BETWEEN enabled cadences rest on the same 2
# days and are noise — the on/off difference is an event-count difference and is
# not. This confirms the long-standing note in live_test._update_kelly that the
# overlay "adds no risk-adjusted value, it just trades DD budget for speed".
#
# Re-enable only for a deliberate speed-over-margin decision (e.g. a challenge
# deadline). If you do, use 21 or 63, NOT 1 — every-bar measured worst on every
# column. Re-run the sweep first; the book changes.
ENABLED = False         # False -> NEUTRAL (1.0), not defensive (0.5).
LOOKBACK_DAYS = 1825    # candle history fetched before selecting the active window
ACTIVE_WINDOW = 60      # most recent N ACTIVE (non-zero) position-return bars
MIN_TRADES = 30         # below this there is no evidence -> FLOOR
UP = 2.0                # positive edge
FLOOR = 0.5             # negative edge, or too little evidence
NEUTRAL = 1.0           # only-wins, and the disabled case

# Recompute cadence, in evaluated bars. DORMANT while ENABLED is False — the
# multiplier is NEUTRAL however often it is called, and live_test skips the
# recompute entirely rather than reconstructing a sleeve to learn that.
#
# Set to 21, not 1, because every-bar measured WORST of every cadence tried
# (margin +0.56 pp vs +0.84 at 21). Leaving 1 here would hand a future re-enable
# the worst setting by default.
#
# CAVEAT IF YOU RE-ENABLE: fix_runner does NOT honour this. It recomputes inside
# latest() on every pass with no bar counter, so it would run every-pass while
# live_test runs every 21 bars — the exact drift centralising this was meant to
# remove. Honouring it needs a counter persisted in fix_runner_state.json (which
# is per-host live broker state, handled carefully). Do that BEFORE re-enabling.
RECOMPUTE_EVERY = 21


def kelly_multiplier(position_returns: Optional[Sequence[float]]) -> float:
    """Binary Kelly multiplier from a per-bar POSITION-RETURN series.

    `position_returns` is signal.shift(1) * bar_return — i.e. already signed, with
    flat bars at 0.0. Zeros are dropped before the window is taken, so a sleeve
    that is rarely in the market still looks back over enough real trades.

    Returns FLOOR on None/empty so a data failure can only ever REDUCE size.
    """
    if not ENABLED:
        return NEUTRAL
    if position_returns is None:
        return FLOOR

    a = np.asarray(position_returns, dtype=float)
    a = a[np.isfinite(a)]
    active = a[a != 0.0][-ACTIVE_WINDOW:]

    # Too few trades is NOT a reason to size up — it is the absence of evidence.
    if len(active) < MIN_TRADES:
        return FLOOR

    wins, losses = active[active > 0], active[active < 0]
    # An unbroken win streak is not licence for full Kelly: B is undefined with no
    # losses, so fall back to NEUTRAL rather than UP.
    if len(wins) == 0 or len(losses) == 0:
        return NEUTRAL if len(wins) else FLOOR

    win_rate = len(wins) / len(active)
    avg_loss = abs(losses.mean())
    b = wins.mean() / avg_loss if avg_loss > 0 else 0.0
    # b <= 0 leaves k at 0, which is NOT > 0, so the result is FLOOR. A guard that
    # returned early with a different default here would flip a degenerate payoff
    # ratio to 2x.
    k = win_rate - (1 - win_rate) / b if b > 0 else 0.0
    return UP if k > 0 else FLOOR


def position_returns_from_signal(signal, closes) -> np.ndarray:
    """signal.shift(1) * pct_change(close), flats as 0.0 — the input both books
    were building separately and identically."""
    sig = np.asarray(signal, dtype=float)
    px = np.asarray(closes, dtype=float)
    n = min(len(sig), len(px))
    out = np.zeros(n)
    for i in range(1, n):
        if sig[i - 1] != 0 and px[i - 1] != 0:
            out[i] = sig[i - 1] * (px[i] - px[i - 1]) / px[i - 1]
    return out
