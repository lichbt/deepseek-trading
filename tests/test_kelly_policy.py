"""The Kelly overlay is the largest sizing lever in the stack — it multiplies the
risk fraction directly, clamped only by MAXRISK, while conviction and weight merely
redistribute inside an already-pinned cluster cap. It is also BINARY, so every
branch below is a 4x swing in position size rather than a rounding difference.

These pin the branches where a refactor changes RISK without breaking anything
visibly, plus the guarantee that both books resolve to one implementation.
"""
import importlib

import numpy as np
import pytest

import kelly_policy as K


def test_the_shipped_default_is_disabled():
    """Kelly was turned OFF 2026-07-31 on a cadence sweep: disabled more than
    doubled the daily margin (0.56 -> 1.25 pp) at unchanged Sharpe. Read from a
    FRESH import so the enable-fixture below cannot mask a change to the default."""
    fresh = importlib.reload(importlib.import_module('kelly_policy'))
    try:
        assert fresh.ENABLED is False, (
            'Kelly re-enabled — that is a deliberate speed-over-margin decision; '
            're-run the cadence sweep and update the rationale in kelly_policy.py')
    finally:
        fresh.ENABLED = False


@pytest.fixture(autouse=True)
def _enabled():
    """The formula tests below exercise the MATH, which is only reachable when the
    overlay is on. Kept separate from the shipped default, asserted above."""
    prev = K.ENABLED
    K.ENABLED = True
    yield
    K.ENABLED = prev


def _returns(wins, losses, flats=0):
    """A position-return series with the given win/loss counts and padding flats."""
    return np.array([0.0] * flats + [0.01] * wins + [-0.01] * losses)


# --- the evidence floor ---------------------------------------------------

def test_below_min_trades_floors_rather_than_boosting():
    """Too few trades is the ABSENCE of evidence, never a licence to size up.
    Returning NEUTRAL or UP here would double a sleeve nothing is known about."""
    r = _returns(wins=K.MIN_TRADES - 1, losses=0)
    assert K.kelly_multiplier(r) == K.FLOOR


def test_exactly_min_trades_is_enough():
    """The gate is `< MIN_TRADES`, so MIN_TRADES itself must score."""
    r = _returns(wins=K.MIN_TRADES, losses=0)
    assert K.kelly_multiplier(r) == K.NEUTRAL   # all wins -> neutral, not floor


def test_flats_do_not_count_toward_the_minimum():
    """A rarely-in-market sleeve must look back over real trades, not calendar bars:
    500 flat bars plus 5 trades is still 5 trades."""
    assert K.kelly_multiplier(_returns(wins=5, losses=0, flats=500)) == K.FLOOR


# --- degenerate distributions ---------------------------------------------

def test_only_wins_is_neutral_not_full_kelly():
    """B is undefined with no losses. An unbroken win streak is not evidence of
    a 2x-worthy edge — it usually means the window is too short."""
    assert K.kelly_multiplier(_returns(wins=40, losses=0)) == K.NEUTRAL


def test_only_losses_floors():
    assert K.kelly_multiplier(_returns(wins=0, losses=40)) == K.FLOOR


def test_negative_zero_is_treated_as_flat_not_as_a_loss():
    """-0.0 == 0.0, so those bars are dropped as flats and the series is all-wins.
    If -0.0 were ever counted as a loss, avg_loss would be 0, b would be 0, and the
    result would silently become FLOOR instead of NEUTRAL."""
    r = np.array([0.01] * 40 + [-0.0] * 40)
    assert K.kelly_multiplier(r) == K.NEUTRAL


def test_the_b_guard_cannot_be_reached_with_real_losses():
    """Documents WHY `if b > 0 else 0.0` is the safe default: any series with at
    least one strictly-negative bar has avg_loss > 0, so b > 0. The guard only
    fires on the impossible case, and when it does it must FLOOR, not boost."""
    r = np.array([0.02] * 35 + [-0.01] * 35)
    wins, losses = r[r > 0], r[r < 0]
    assert abs(losses.mean()) > 0            # the precondition that makes b valid
    assert K.kelly_multiplier(r) == K.UP


# --- the actual edge decision ---------------------------------------------

def test_positive_edge_sizes_up():
    """High win rate with a favourable payoff -> k > 0 -> UP."""
    assert K.kelly_multiplier(np.array([0.02] * 40 + [-0.01] * 20)) == K.UP


def test_negative_edge_floors():
    """Losses bigger and more frequent than wins -> k <= 0 -> FLOOR."""
    assert K.kelly_multiplier(np.array([0.01] * 15 + [-0.03] * 45)) == K.FLOOR


def test_only_the_last_active_window_counts():
    """Ancient history must not rescue a sleeve that is losing now."""
    old_good = [0.05] * 200
    recent_bad = [0.001] * 10 + [-0.05] * 50
    assert K.kelly_multiplier(np.array(old_good + recent_bad)) == K.FLOOR


# --- failure and disable modes --------------------------------------------

def test_no_data_floors_so_a_fetch_failure_can_only_shrink():
    assert K.kelly_multiplier(None) == K.FLOOR
    assert K.kelly_multiplier(np.array([])) == K.FLOOR


def test_nonfinite_values_are_dropped_not_propagated():
    """A NaN reaching the mean would make every comparison False and silently FLOOR
    a sleeve that has a real edge — drop them instead."""
    r = np.array([0.02] * 40 + [-0.01] * 20 + [np.nan, np.inf])
    assert K.kelly_multiplier(r) == K.UP


def test_disabled_is_neutral_not_defensive():
    """Turning the overlay OFF must mean 1.0x — sizing as the risk model intends.
    Returning FLOOR would halve the whole book on what reads as a no-op switch.
    This is the LIVE path now, not a hypothetical: ENABLED ships False."""
    r = np.array([0.02] * 40 + [-0.01] * 20)
    K.ENABLED = False
    assert K.kelly_multiplier(r) == K.NEUTRAL == 1.0
    K.ENABLED = True
    assert K.kelly_multiplier(r) == K.UP


def test_disabled_overrides_every_branch_including_no_data():
    """Off must be unconditional. If the disable check sat after the None/short
    guards, a fetch failure would still return 0.5x and halve a sleeve."""
    K.ENABLED = False
    assert K.kelly_multiplier(None) == K.NEUTRAL
    assert K.kelly_multiplier(np.array([])) == K.NEUTRAL
    assert K.kelly_multiplier(np.array([-0.05] * 90)) == K.NEUTRAL


# --- both books resolve to ONE implementation -----------------------------

def test_fix_runner_delegates_to_the_shared_policy():
    import fix_runner
    r = np.array([0.02] * 40 + [-0.01] * 20)
    assert fix_runner._rolling_kelly(r) == K.kelly_multiplier(r)


def test_both_books_export_the_same_constants():
    """The drift this module exists to prevent: two files, two copies, no check."""
    import fix_runner
    import live_test
    assert fix_runner.KELLY_WIN == live_test.KELLY_ACTIVE_WINDOW == K.ACTIVE_WINDOW
    assert fix_runner.KELLY_MIN == live_test.KELLY_MIN_TRADES == K.MIN_TRADES
    assert fix_runner.KELLY_UP == K.UP


def test_live_test_reads_its_cadence_from_the_policy():
    """live_test held its own 21 while fix_runner recomputed every pass, so the same
    sleeve could sit at 2x on one book and 0.5x on the other for a month."""
    import live_test
    assert live_test.KELLY_RECOMPUTE == K.RECOMPUTE_EVERY


def test_dormant_cadence_is_not_the_worst_measured_value():
    """Every-bar measured worst of every cadence swept (margin +0.56 vs +0.84 at 21).
    If the overlay is ever re-enabled it must not default to that."""
    assert K.RECOMPUTE_EVERY != 1
    assert K.RECOMPUTE_EVERY == 21


def test_disabled_kelly_skips_the_expensive_recompute(monkeypatch):
    """With the overlay off, _update_kelly must NOT reconstruct the sleeve — the
    answer is NEUTRAL regardless, and the reconstruction is a candle fetch plus a
    full strategy re-run, per sleeve per bar."""
    import live_test

    called = []

    class _Stub:
        kelly_mult = 2.0
        _kelly_bar_count = 0

        def _recent_position_returns(self):
            called.append(1)
            return np.array([0.02] * 40 + [-0.01] * 20)

    stub = _Stub()
    K.ENABLED = False
    live_test.LiveTrader._update_kelly(stub, force=True)
    assert not called, 'reconstructed the sleeve despite Kelly being disabled'
    assert stub.kelly_mult == K.NEUTRAL


# --- the input builder ----------------------------------------------------

def test_position_returns_shift_the_signal():
    """The bar's return is earned by YESTERDAY's position. An unshifted signal is
    look-ahead and would make every sleeve appear to have edge."""
    sig = [0, 1, 1, 0]
    closes = [100.0, 110.0, 121.0, 121.0]
    out = K.position_returns_from_signal(sig, closes)
    assert out[0] == 0.0            # no prior bar
    assert out[1] == 0.0            # signal was FLAT going into bar 1
    assert out[2] == pytest.approx(0.10)   # long through the +10% bar
    assert out[3] == 0.0            # flat again


def test_position_returns_carry_short_pnl_with_the_right_sign():
    out = K.position_returns_from_signal([-1, -1], [100.0, 90.0])
    assert out[1] == pytest.approx(0.10)   # short a -10% bar is +10%
