"""FIXED_RISK_USD sizing mode (fix_runner + oanda_book_simulator).

Percent-of-equity is the default and must stay byte-identical; fixed-dollar is an
opt-in knob. The asserts below pin the three properties that make the knob safe:
the percent path does not move, fixed dollars do not drift with equity, and the
per-trade MAXRISK cap stays equity-relative so a shrinking account can never risk
more than its cap.
"""
import importlib
import os

import oanda_book_simulator as sim

ATR, STOP, Q = 100.0, 1.5, 1.0


def _units(equity, fixed=None):
    os.environ.pop("FIXED_RISK_USD", None)
    if fixed is not None:
        os.environ["FIXED_RISK_USD"] = str(fixed)
    importlib.reload(sim)
    return sim.risk_units(equity, ATR, STOP, 1.0, 1.0, 1.0, 1.0,
                          risk=0.005, quote_to_usd=Q)


def test_percent_path_unchanged():
    # 100k * 0.005 / (1.5 * 100) == 3.3333 units
    assert abs(_units(100_000) - 3.3333) < 1e-3
    # and it scales with equity, which is the whole point of percent sizing
    assert abs(_units(200_000) - 6.6667) < 1e-3


def test_fixed_dollars_do_not_scale_with_equity():
    lo, hi = _units(100_000, fixed=504), _units(219_733, fixed=504)
    assert abs(lo - hi) < 1e-9, (lo, hi)
    assert abs(lo - 504 / (STOP * ATR)) < 1e-9


def test_maxrisk_cap_stays_equity_relative():
    # 10k equity, MAXRISK 2% => $200 risk => 1.3333 units, not the $504 asked for
    assert abs(_units(10_000, fixed=504) - 200 / (STOP * ATR)) < 1e-9
