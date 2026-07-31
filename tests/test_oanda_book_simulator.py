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


@pytest.fixture
def kelly_on():
    """kelly_multiplier now delegates to kelly_policy, whose shipped default is
    DISABLED (2026-07-31 — see the sweep in kelly_policy.py). These three test the
    FORMULA, which is only reachable with the overlay on."""
    import kelly_policy
    prev = kelly_policy.ENABLED
    kelly_policy.ENABLED = True
    yield
    kelly_policy.ENABLED = prev


def test_kelly_insufficient_is_half(kelly_on):
    assert kelly_multiplier([0.01] * 29) == 0.5


def test_kelly_positive_edge_is_double(kelly_on):
    assert kelly_multiplier([0.02, -0.01] * 30) == 2.0


def test_kelly_negative_edge_is_half(kelly_on):
    assert kelly_multiplier([0.005, -0.02] * 30) == 0.5


def test_kelly_is_neutral_in_the_shipped_configuration():
    """The live path: the simulator must model the un-levered book, or its risk
    figures describe a book that is not being traded."""
    assert kelly_multiplier([0.02, -0.01] * 30) == 1.0
    assert kelly_multiplier([0.005, -0.02] * 30) == 1.0


def test_decay_rechecks_after_21_days():
    now = pd.Timestamp("2026-01-01")
    scale, checked = decay_multiplier([-0.01] * 30, now, None, 1.0)
    assert scale == 0.5
    scale2, checked2 = decay_multiplier([0.01] * 30, now + pd.Timedelta(days=20), checked, scale)
    assert scale2 == 0.5
    assert checked2 == checked
    scale3, _ = decay_multiplier([0.01] * 30, now + pd.Timedelta(days=21), checked, scale)
    assert scale3 == 1.0


def _stop_out_sleeve(signal, prev_target=1):
    """A sleeve holding +1 at 100 with a stop at 95, whose bar 1 trades down to
    94 and stops out. `signal` is what the strategy says on each bar."""
    dates = pd.to_datetime(["2026-01-01", "2026-01-02", "2026-01-03", "2026-01-04"])
    frame = pd.DataFrame({
        "date": dates,
        "open": [100.0] * 4, "high": [100.0] * 4,
        "low": [100.0, 94.0, 96.0, 96.0], "close": [100.0, 96.0, 96.0, 96.0],
    })
    return dates, Sleeve("s", "EUR_USD", frame, pd.Series(signal),
                         pd.Series([2.5] * 4), 1.0, set(), 2.0,
                         pd.Series([1.0] * 4), units=1000, direction=1,
                         prev_target=prev_target, entry=100.0, stop=95.0)


def test_stopped_sleeve_does_not_re_enter_on_an_unchanged_signal():
    """The live rule (live_test.order_decision) and the validated return stream
    (pipeline_utils.compute_returns_with_stop) both keep a stopped-out sleeve
    FLAT until the signal VALUE changes. Comparing the signal to the POSITION
    instead re-entered the moment the stop zeroed direction — 555 of 2472
    entries on the 25-sleeve 2024-2026 book (2026-07-31)."""
    dates, sleeve = _stop_out_sleeve([1, 1, 1, 1])
    simulate([sleeve], dates[1], dates[3])
    assert sleeve.direction == 0
    assert sleeve.units == 0
    assert sleeve.entries == 0


def test_stopped_sleeve_does_re_enter_once_the_signal_changes():
    """Positive control: the flat-after-stop rule must not swallow a real flip."""
    dates, sleeve = _stop_out_sleeve([1, 1, -1, -1])
    simulate([sleeve], dates[1], dates[3])
    assert sleeve.direction == -1
    assert sleeve.entries == 1


def test_first_evaluated_bar_aligns_to_the_signal():
    """prev_target=None is a sleeve that has never been evaluated, which is the
    startup 'align' live DOES take — otherwise a book would start flat and stay
    flat until every sleeve happened to flip."""
    dates, sleeve = _stop_out_sleeve([1, 1, 1, 1], prev_target=None)
    sleeve.direction = 0
    sleeve.units = 0
    simulate([sleeve], dates[1], dates[3])
    assert sleeve.direction == 1
    assert sleeve.entries == 1
