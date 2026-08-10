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


# --- swap / weekend-flat -----------------------------------------------------

def _weekend_sleeve(instrument="NAS100_USD", signal=(1, 1, 1, 1, 1)):
    """Wed / Thu / Sun / Mon / Tue — the real OANDA daily stamping. There is no
    Friday- or Saturday-stamped bar, so the THURSDAY-stamped bar IS the Friday
    session and its close is the last print before the 21:00 rollover.

    The Thursday sits at index 1 because `simulate` skips a sleeve's index-0 bar
    (it has no previous close to mark against)."""
    dates = pd.to_datetime(["2025-12-31", "2026-01-01", "2026-01-04",
                            "2026-01-05", "2026-01-06"])
    assert [d.weekday() for d in dates] == [2, 3, 6, 0, 1]
    frame = pd.DataFrame({
        "date": dates, "open": [100.0] * 5, "high": [100.0] * 5,
        "low": [100.0] * 5, "close": [100.0] * 5,
    })
    return dates, Sleeve("s", instrument, frame, pd.Series(list(signal)),
                         pd.Series([2.5] * 5), 1.0, set(), 2.0,
                         pd.Series([1.0] * 5), units=10.0, direction=1,
                         prev_target=1, entry=100.0, stop=95.0)


def test_weekend_flat_closes_at_the_friday_close():
    dates, sleeve = _weekend_sleeve()
    simulate([sleeve], dates[1], dates[1], weekend_flat="all")
    assert sleeve.direction == 0
    assert sleeve.units == 0


def test_weekend_flat_does_not_re_enter_on_monday():
    """THE POINT OF THE ARM. live_test.order_decision returns None whenever
    latest_signal == prev_signal, so a sleeve flattened by policy stays flat
    until the signal genuinely CHANGES — it does not reappear at the Sunday
    reopen. Re-entering here would credit the book an entry neither the runner
    nor the validated return stream ever takes."""
    dates, sleeve = _weekend_sleeve(signal=(1, 1, 1, 1, 1))
    simulate([sleeve], dates[1], dates[4], weekend_flat="all")
    assert sleeve.direction == 0
    assert sleeve.entries == 0


def test_weekend_flat_still_takes_a_genuine_flip():
    """Positive control: the policy flatten must not swallow a real signal
    change, or the sleeve would be permanently dead rather than weekend-flat."""
    dates, sleeve = _weekend_sleeve(signal=(1, 1, 1, -1, -1))
    simulate([sleeve], dates[1], dates[4], weekend_flat="all")
    assert sleeve.direction == -1
    assert sleeve.entries == 1


def test_weekend_flat_selective_holds_an_unlisted_instrument():
    dates, sleeve = _weekend_sleeve(instrument="EUR_USD")
    assert "EUR_USD" not in obs.SELECTIVE_FLAT
    simulate([sleeve], dates[1], dates[1], weekend_flat="selective")
    assert sleeve.direction == 1


def test_measured_swap_is_already_in_account_currency():
    """A measured rate comes off broker_swap.swap_usd, so applying quote_to_usd
    again would double-count the FX leg."""
    assert obs.swap_charge("NAS100_USD", 1.0, 28609.0, 0.01, 1, False) == \
        pytest.approx(-35.875)


def test_proxied_swap_converts_from_the_quote_currency():
    charge = obs.swap_charge("DE30_EUR", 1.0, 20000.0, 1.1, 1, False)
    assert charge == pytest.approx(-0.0002306 * 20000.0 * 1.1)


def test_friday_bar_charges_three_days_without_a_special_case():
    """An ordinary instrument is charged weekdays-only but takes a 3x Friday
    roll, and the triple exactly offsets the two uncharged weekend days — so
    charge-days equals the calendar gap on every bar."""
    weekday = obs.swap_charge("XAG_USD", 100.0, 62.0, 1.0, 1, False)
    weekend = obs.swap_charge("XAG_USD", 100.0, 62.0, 1.0, 3, True)
    assert weekend == pytest.approx(weekday * 3)


def test_seven_day_instrument_takes_two_extra_days_over_a_weekend():
    """WTI accrues Saturday and Sunday ON TOP of the Friday triple — measured
    -1.40/unit on both weekend days plus a -4.20 Friday charge."""
    assert obs.swap_charge("WTICO_USD", 1.0, 84.0, 1.0, 3, True) == \
        pytest.approx(-0.70 * 5)


def test_unpriced_instrument_is_charged_nothing_rather_than_guessed():
    assert obs.swap_charge("SUGAR_USD", 1000.0, 20.0, 1.0, 3, True) == 0.0
