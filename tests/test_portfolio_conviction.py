"""Tests for the manual conviction multiplier in portfolio.inverse_vol_weights.

A conviction < 1.0 shrinks a sleeve's inverse-vol allocation (and thus its live
risk-per-trade) and survives rebalances — used to deploy a low-conviction
diversifier at a small size.
"""
import pandas as pd
import portfolio

# deterministic identical return streams -> identical vol, so any weight
# difference comes purely from the conviction multiplier
_R = pd.Series([0.01, -0.012, 0.008, -0.015, 0.02, -0.01, 0.013, -0.009])


def test_conviction_shrinks_weight(monkeypatch):
    monkeypatch.setattr(portfolio, "CONVICTION", {"B": 0.5})
    weights, _, _ = portfolio.inverse_vol_weights({"A": _R.copy(), "B": _R.copy()})
    assert weights["A"] > weights["B"]
    assert abs(weights["B"] / weights["A"] - 0.5) < 1e-9   # exactly half


def test_no_conviction_equal_vol_equal_weight(monkeypatch):
    monkeypatch.setattr(portfolio, "CONVICTION", {})
    weights, _, _ = portfolio.inverse_vol_weights({"A": _R.copy(), "B": _R.copy()})
    assert abs(weights["A"] - weights["B"]) < 1e-12


def test_weights_still_sum_to_one(monkeypatch):
    monkeypatch.setattr(portfolio, "CONVICTION", {"B": 0.3})
    weights, _, _ = portfolio.inverse_vol_weights({"A": _R.copy(), "B": _R.copy(), "C": _R.copy()})
    assert abs(sum(weights.values()) - 1.0) < 1e-9
