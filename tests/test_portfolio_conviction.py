"""Tests for the manual conviction multiplier in portfolio.inverse_vol_weights.

A conviction < 1.0 shrinks a sleeve's inverse-vol allocation (and thus its live
risk-per-trade) and survives rebalances — used to deploy a low-conviction
diversifier at a small size.
"""
import pytest

import pandas as pd
import portfolio

# deterministic identical return streams -> identical vol, so any weight
# difference comes purely from the conviction multiplier
_R = pd.Series([0.01, -0.012, 0.008, -0.015, 0.02, -0.01, 0.013, -0.009])


@pytest.fixture(autouse=True)
def _invvol(monkeypatch):
    """Conviction is an INVVOL-path feature, so pin the scheme.

    portfolio imports .env, which has carried WEIGHTING=equal since the 2026-08-08
    deploy, and the equal branch bypasses CONVICTION deliberately. Without this the
    file tests whatever the machine happens to be configured for: the shrink test
    failed outright, and — worse — test_no_conviction_equal_vol_equal_weight PASSED
    for the wrong reason, because equal weighting also yields equal weights.
    """
    monkeypatch.setattr(portfolio, "WEIGHTING", "invvol")


def test_conviction_shrinks_weight(monkeypatch):
    monkeypatch.setattr(portfolio, "CONVICTION", {"B": 0.5})
    weights, _, _ = portfolio.inverse_vol_weights({"A": _R.copy(), "B": _R.copy()})
    assert weights["A"] > weights["B"]
    assert abs(weights["B"] / weights["A"] - 0.5) < 1e-9   # exactly half


def test_no_conviction_equal_vol_equal_weight(monkeypatch):
    monkeypatch.setattr(portfolio, "CONVICTION", {})
    weights, _, _ = portfolio.inverse_vol_weights({"A": _R.copy(), "B": _R.copy()})
    assert abs(weights["A"] - weights["B"]) < 1e-12


def test_equal_weighting_bypasses_conviction(monkeypatch):
    """Pins the behaviour that broke the tests above, so it is a decision not a
    surprise: under WEIGHTING=equal a conviction trim is INERT by design."""
    monkeypatch.setattr(portfolio, "WEIGHTING", "equal")
    monkeypatch.setattr(portfolio, "CONVICTION", {"B": 0.5})
    weights, _, _ = portfolio.inverse_vol_weights({"A": _R.copy(), "B": _R.copy()})
    assert abs(weights["A"] - weights["B"]) < 1e-12


def test_weights_still_sum_to_one(monkeypatch):
    monkeypatch.setattr(portfolio, "CONVICTION", {"B": 0.3})
    weights, _, _ = portfolio.inverse_vol_weights({"A": _R.copy(), "B": _R.copy(), "C": _R.copy()})
    assert abs(sum(weights.values()) - 1.0) < 1e-9
