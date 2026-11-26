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

# DISABLED 2026-07-31, RE-CONFIRMED the same day against corrected numbers.
# The overlay buys return by taking proportionally more risk, and the daily
# margin is the binding constraint on a 3% wall where one breach is an instant DQ.
#
# THE FIRST SWEEP'S NUMBERS WERE WRONG AND ARE REPLACED BELOW. It ran through
# oanda_book_simulator before commit 58c1a6f, where a stopped-out sleeve
# re-entered on an UNCHANGED signal — 555 of 2472 entries were phantom, and each
# one eventually closed into closed_returns, firing the decay halving 605 times
# instead of 165. The old table understated risk across the board (it recorded
# disabled at -1.75% / +1.25 pp; the truth is -2.35% / +0.65 pp).
#
# Cadence sweep re-run on the CORRECTED simulator, real-sized 25-sleeve book
# (2024-01-01 -> 2026-07-30, same book and window, ONLY the Kelly setting varying):
#
#   config         worst day   margin   maxDD    return   Sharpe   days<=-2%
#   disabled         -2.35%    +0.65   -5.70%    47.76%    2.10        1
#   every 63         -2.72%    +0.28   -7.61%    51.55%    1.95        5
#   every 21         -2.71%    +0.29   -8.49%    55.72%    1.93        4
#   every  1         -2.71%    +0.29   -8.01%    55.91%    1.93        4
#   up1.5/fl0.5      -2.45%    +0.55   -7.37%    49.96%    1.99        3
#   up1.5/fl0.75     -2.40%    +0.60   -7.02%    52.30%    2.04        3
#   up1.5/fl1.0      -2.35%    +0.65   -6.74%    54.64%    2.08        3
#
# Disabled still more than doubles the margin (0.28 -> 0.65 pp), and it now also
# WINS ON SHARPE (2.10 vs 1.93-1.95) rather than merely tying. So the old
# "Sharpe-neutral" framing understated the case: disabling improves risk-adjusted
# return AND margin AND tail-day count, for ~4-8 pp of return over 2.5 years.
#
# TWO CORRECTIONS TO WHAT THE OLD TABLE APPEARED TO SHOW —
#
# (1) THE CADENCE RANKING WAS NOISE AND IS NOW GONE. The old data separated
#     every-1 (-2.44%) from every-21/63 (-2.16/-2.13), which is what the previous
#     "use 21 or 63, NEVER 1" instruction rested on. Corrected, all three are
#     indistinguishable: -2.71/-2.71/-2.72, margin 0.28-0.29. THERE IS NO
#     EVIDENCE BASIS FOR PREFERRING ANY CADENCE. RECOMPUTE_EVERY stays at 21 as a
#     cheap conservative default, not because it measured better.
#
# (2) KELLY CANNOT CHANGE ENTRY TIMING AT ALL, so the cadences were never
#     differentially contaminated — only uniformly so. Entries (1917) and decay
#     events (165) are IDENTICAL across all seven configs above, because the stop
#     sits at entry +/- mult*ATR and is independent of position size. Kelly scales
#     units; it cannot move a stop, an entry or an exit.
#
# up1.5/fl1.0 was priced properly this time and REJECTED. It ties disabled on
# worst day and margin with +6.9 pp more return and is genuinely faster in the
# short run (MC 60d pass 11.0% vs 6.5%, 120d 47.9% vs 39.1%). But it is leverage,
# not edge: return +14.4% for vol +12.5%, Sharpe 2.08 vs 2.10 — the selectivity
# earns nothing. With FLOOR=1.0 the multiplier never goes below 1.0, so the
# risk-reducing leg is gone entirely and it ONLY sizes up. Costs: maxDD -6.74% vs
# -5.70%, MC total-DD breach 0.95% vs 0.37% at 756d, and (holding the fitted tail
# index fixed, since a free t-fit is too unstable to use — bootstrapped df CIs
# overlap almost completely) 1.4-1.9x the probability of a <=-3% day, i.e. of the
# instant DQ. If you want that speed, raise BASE_RISK transparently instead;
# routing leverage through an overlay whose defining leg is switched off hides it.
# Note also that fl1.0 was picked by sweeping this exact 2.5-year path, which is
# selection on noise — disabled is the null, not a swept cell.
#
# Re-enable only for a deliberate speed-over-margin decision (e.g. a challenge
# deadline). Re-run the sweep first; the book changes.
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
# Set to 21 as a cheap conservative default, NOT because it measured better. The
# corrected sweep above finds every-1, every-21 and every-63 indistinguishable
# (-2.71/-2.71/-2.72% worst day, margin 0.28-0.29 pp); the apparent every-bar
# penalty in the pre-58c1a6f numbers was an artifact of the phantom-re-entry bug.
# 21 is kept because recomputing less often is cheaper and cannot be worse, and
# because leaving 1 here would hand a future re-enable the most aggressive
# cadence by default — but do not cite evidence for the choice, there is none.
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
