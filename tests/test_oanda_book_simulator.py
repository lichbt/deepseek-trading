import json

import pandas as pd
import pytest

import oanda_book_simulator as obs
from oanda_book_simulator import Sleeve, decay_multiplier, kelly_multiplier, risk_units, simulate


def test_risk_units_composes_overlays():
    units = risk_units(100000, 1.0, 2.0, 2.0, 0.5, 2.0, 0.5)
    assert units == pytest.approx(250.0)


def test_risk_units_caps_effective_risk():
    units = risk_units(100000, 1.0, 2.0, 3.0, 1.0, 2.0, 1.0)
    assert units == pytest.approx(1000.0)


def test_risk_units_converts_quote_currency_and_applies_bounds():
    assert risk_units(100000, 1.0, 2.0, 1.0, 1.0, 1.0, 1.0,
                      quote_to_usd=0.01, instrument="GBP_JPY") == 25000
    assert risk_units(100000, 0.01, 2.0, 1.0, 1.0, 1.0, 1.0,
                      instrument="BTC_USD") == 1.0
    assert risk_units(1, 1000, 2.0, 1.0, 1.0, 1.0, 1.0,
                      instrument="ETH_USD") == 0.001


def test_simulate_converts_quote_pnl_to_usd():
    dates = pd.to_datetime(["2026-01-01", "2026-01-02"])
    frame = pd.DataFrame({
        "date": dates, "open": [100, 100], "high": [100, 102],
        "low": [100, 100], "close": [100, 102],
    })
    sleeve = Sleeve("s", "GBP_JPY", frame, pd.Series([1, 1]), pd.Series([1, 1]),
                    1.0, set(), 100.0, pd.Series([0.01, 0.01]),
                    units=1000, direction=1, entry=100, stop=0)
    result = simulate([sleeve], dates[1], dates[1])
    assert result.pnl.iloc[0] == pytest.approx(20.0)


def test_kelly_insufficient_is_half():
    assert kelly_multiplier([0.01] * 29) == 0.5


def test_kelly_positive_edge_is_double():
    assert kelly_multiplier([0.02, -0.01] * 30) == 2.0


def test_kelly_negative_edge_is_half():
    assert kelly_multiplier([0.005, -0.02] * 30) == 0.5


def test_decay_rechecks_after_21_days():
    now = pd.Timestamp("2026-01-01")
    scale, checked = decay_multiplier([-0.01] * 30, now, None, 1.0)
    assert scale == 0.5
    scale2, checked2 = decay_multiplier([0.01] * 30, now + pd.Timedelta(days=20), checked, scale)
    assert scale2 == 0.5
    assert checked2 == checked
    scale3, _ = decay_multiplier([0.01] * 30, now + pd.Timedelta(days=21), checked, scale)
    assert scale3 == 1.0
