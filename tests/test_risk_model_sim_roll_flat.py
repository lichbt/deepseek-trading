"""--roll-flat in risk_model_sim must mean EXACTLY what it means in
oanda_book_simulator.

risk_model_sim is the only harness that measures the intraday floating low — the
figure The5ers actually judges — so the policy has to be scoreable there. But the
two harnesses are separate implementations of the same replay, kept honest by
`--check-baseline`. A flag that drifts between them turns every cross-harness
comparison into a comparison of harnesses.
"""
import pickle

import pandas as pd
import pytest

import oanda_book_simulator as obs
import prop_risk_model as M
from oanda_book_simulator import Sleeve

from scripts import risk_model_sim as R


def _sleeve(instrument="NAS100_USD"):
    """Wed/Thu/Sun/Mon/Tue on the real OANDA daily stamping, flat prices so the
    only P&L is the cost model."""
    dates = pd.to_datetime(["2025-12-31", "2026-01-01", "2026-01-04",
                            "2026-01-05", "2026-01-06"])
    frame = pd.DataFrame({
        "date": dates, "open": [100.0] * 5, "high": [100.0] * 5,
        "low": [100.0] * 5, "close": [100.0] * 5,
    })
    return dates, Sleeve("s", instrument, frame, pd.Series([1] * 5),
                         pd.Series([2.5] * 5), 1.0, set(), 2.0,
                         pd.Series([1.0] * 5), units=10.0, direction=1,
                         prev_target=1, entry=100.0, stop=95.0)


def _run_both(**kw):
    dates, a = _sleeve()
    base = obs.simulate([a], dates[1], dates[4], venue="ctrader", **kw)
    _, b = _sleeve()
    cfg = R.config_from(0.005, 0.02, 0.80, components=())
    res, summ = R.run(cfg, pickle.dumps([b]), dates[1], dates[4],
                      venue="ctrader", skip_min_lot=False, guard=False, **kw)
    return base, res, summ, a, b


def test_roll_flat_equity_matches_the_sanctioned_simulator():
    base, res, _, _, _ = _run_both(charge_swap=True, roll_flat="indices")
    assert len(base) == len(res)
    assert base.equity.values == pytest.approx(res.equity.values, abs=1e-9)


def test_roll_flat_replaces_the_carry_with_a_round_trip():
    """The elif is the point: a bar either pays swap or pays the round trip,
    never both."""
    _, _, summ, _, _ = _run_both(charge_swap=True, roll_flat="indices")
    assert summ["swap_paid"] == 0.0
    assert summ["spread_paid"] < 0.0

    _, _, held, _, _ = _run_both(charge_swap=True)
    assert held["swap_paid"] < 0.0
    assert held["spread_paid"] == 0.0


def test_roll_flat_indices_leaves_an_unlisted_instrument_carrying():
    dates, s = _sleeve("EUR_USD")
    cfg = R.config_from(0.005, 0.02, 0.80, components=())
    _, summ = R.run(cfg, pickle.dumps([s]), dates[1], dates[4],
                    venue="ctrader", skip_min_lot=False, guard=False,
                    charge_swap=True, roll_flat="indices")
    assert summ["swap_paid"] < 0.0
    assert summ["spread_paid"] == 0.0


def test_roll_flat_needs_charge_swap_and_is_off_by_default():
    """It prices an alternative to the carry, so with no carry charged there is
    nothing to replace and the flag must cost nothing."""
    _, _, summ, _, _ = _run_both(roll_flat="indices")
    assert summ["swap_paid"] == 0.0
    assert summ["spread_paid"] == 0.0

    _, _, off, _, _ = _run_both(charge_swap=True)
    assert off["spread_paid"] == 0.0


def test_roll_flat_does_not_move_the_intraday_low():
    """The roll is a cash adjustment at the bar close, not an excursion the
    floating equity passes through — so the column the firm judges must be
    untouched by the policy's cost."""
    _, res, _, _, _ = _run_both(charge_swap=True, roll_flat="indices")
    # Prices are flat, so the only thing that could move either column is the
    # policy. It moves the CLOSE (equity ends below the day base every bar) and
    # leaves the low sitting exactly on the base.
    assert (res.intraday_low_cotimed.values
            == pytest.approx(res.day_base.values, abs=1e-9))
    assert (res.equity < res.day_base).all()
