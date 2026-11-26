"""stress_book is a RISK GATE, so its failure mode is reporting PASS on a book
that would breach. Two defects did exactly that until 2026-07-31:

  * it checked a 5% daily wall when The5ers' rule is 3%;
  * its "real-sized" line was a flat x1.5 guess that did not even cover the Kelly
    overlay — measured that day, 20 of 24 tradeable sleeves ran at 2.0x.

These pin the corrected thresholds and the per-bar Kelly, including the look-ahead
property that makes the Kelly series honest.
"""
import numpy as np
import pandas as pd
import pytest

import kelly_policy as K
import stress_book as SB


@pytest.fixture(autouse=True)
def _enabled():
    """These exercise the Kelly SERIES math, reachable only with the overlay on.
    The shipped default is OFF (see tests/test_kelly_policy.py); with it off every
    bar is 1.0x and stress_book reports the un-levered book, which is the point."""
    prev = K.ENABLED
    K.ENABLED = True
    yield
    K.ENABLED = prev


def test_series_is_flat_at_neutral_when_the_overlay_is_disabled():
    """The live configuration: no bar is levered, none is halved."""
    K.ENABLED = False
    k = SB._rolling_kelly_series(pd.Series([0.02] * 40 + [-0.01] * 40))
    assert (k == K.NEUTRAL).all()


# --- the wall -------------------------------------------------------------

def test_daily_limit_is_three_percent_not_five():
    """The binding prop rule. A 5% wall makes this gate structurally unable to
    fail on a book whose worst day sits between 3% and 5%."""
    assert SB.DAILY_LIMIT == 0.03
    assert SB.TOTAL_LIMIT == 0.10


def test_extra_sizing_multiplier_is_neutral_by_default():
    """It must not drift back into being a stand-in for Kelly, which is modelled."""
    assert SB.EXTRA_SIZING_MULT == 1.0


# --- the Kelly series -----------------------------------------------------

def test_kelly_series_sizes_each_bar_from_strictly_prior_returns():
    """THE look-ahead guard. Bar i must be sized from returns[:i]; using
    returns[:i+1] lets a sleeve's own outcome on day i set its size for day i,
    inflating exactly the tail this tool exists to bound.

    Construct a series whose edge flips sign at a known point: if the multiplier
    at the flip bar already reflects the flip, the window included the future.
    """
    losing = [-0.02] * 80          # long stretch of losses -> FLOOR
    winning = [0.03] * 80          # then a winning stretch
    s = pd.Series(losing + winning)
    k = SB._rolling_kelly_series(s)

    # At the very first winning bar, only losses are in the past -> still FLOOR.
    assert k.iloc[len(losing)] == K.FLOOR, "bar sized using its own future return"
    # Well after the regime change, the past is dominated by wins -> not FLOOR.
    assert k.iloc[-1] != K.FLOOR


def test_kelly_series_starts_at_floor_not_full_size():
    """With no history there is no evidence. Opening at UP would size the earliest
    bars of every backtest at 2x on nothing."""
    k = SB._rolling_kelly_series(pd.Series([0.01] * 50))
    assert k.iloc[0] == K.FLOOR


def test_kelly_series_only_ever_emits_policy_values():
    rng = np.random.default_rng(0)
    k = SB._rolling_kelly_series(pd.Series(rng.normal(0, 0.01, 400)))
    assert set(np.unique(k.values)) <= {K.FLOOR, K.NEUTRAL, K.UP}


def test_kelly_series_preserves_length_and_index():
    idx = pd.date_range("2024-01-01", periods=120, freq="D")
    s = pd.Series(np.linspace(-0.01, 0.01, 120), index=idx)
    k = SB._rolling_kelly_series(s)
    assert len(k) == len(s)
    assert (k.index == idx).all()


def test_a_winning_sleeve_is_scaled_up_relative_to_a_losing_one():
    """The whole point: Kelly must actually differentiate, not return a constant."""
    win = SB._rolling_kelly_series(pd.Series([0.03] * 60 + [-0.01] * 40))
    lose = SB._rolling_kelly_series(pd.Series([0.005] * 20 + [-0.04] * 80))
    assert win.iloc[-1] > lose.iloc[-1]


# --- the property the fudge violated --------------------------------------

def test_kelly_scaling_widens_the_tail_versus_unscaled():
    """A book at 2x Kelly must report a WORSE worst-day than the same book at 1x.
    The pre-fix code applied no Kelly at all and then multiplied by a flat 1.5,
    which understated a book running 2x on most sleeves."""
    r = pd.Series([0.02] * 40 + [-0.01] * 20 + [-0.05])   # edge, then a shock day
    k = SB._rolling_kelly_series(r)
    scaled_worst = (r * k).min()
    assert scaled_worst < r.min(), "Kelly must amplify the shock day, not damp it"
