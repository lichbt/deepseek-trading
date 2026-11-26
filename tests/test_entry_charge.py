"""The entry charge — the half of the round trip this simulator never billed.

Until 2026-09-07 every charge site in oanda_book_simulator was a CLOSE: the
stop-out, the flip, weekend-flat and roll-flat. A position was opened for free,
so `--charge-spread` figures ran optimistic — the right commission card applied
at the wrong number of sites. `_half_spread` has always documented the model as
half a spread on an entry from flat and half on an exit to flat; these pin that
the entry half now actually fires.
"""
import pandas as pd
import pytest

import oanda_book_simulator as obs
from oanda_book_simulator import Sleeve, simulate


def _flat_sleeve(signal, n=6, instrument='EUR_USD'):
    """A sleeve that starts FLAT, so the first trade is an ENTRY, not an exit."""
    dates = pd.to_datetime(['2026-01-%02d' % (d + 1) for d in range(n)])
    frame = pd.DataFrame({
        'date': dates,
        'open': [100.0] * n, 'high': [100.5] * n,
        'low': [99.5] * n, 'close': [100.0] * n,
    })
    return Sleeve('s', instrument, frame, pd.Series(signal), pd.Series([1.0] * n),
                  1.0, set(), 100.0, pd.Series([0.01] * n),
                  units=0, direction=0, entry=0, stop=0), dates


def _run(signal, charge, **kw):
    sleeve, dates = _flat_sleeve(signal)
    res = simulate([sleeve], dates[1], dates[-1], charge_spread=charge, **kw)
    return sleeve, res


def test_opening_a_position_is_charged():
    """The defect, pinned: a sleeve that only ever OPENS still pays."""
    sleeve, _ = _run([0, 1, 1, 1, 1, 1], charge=True)
    assert sleeve.entries >= 1, 'fixture never opened a position'
    assert sleeve.spread_paid < 0, 'entry paid no spread'
    assert sleeve.comm_paid <= 0


def test_no_charge_when_the_flag_is_off():
    sleeve, _ = _run([0, 1, 1, 1, 1, 1], charge=False)
    assert sleeve.entries >= 1
    assert sleeve.spread_paid == 0
    assert sleeve.comm_paid == 0


def test_charging_entries_makes_the_book_strictly_worse():
    """A cost that does not reduce pnl is not being applied."""
    _, free = _run([0, 1, 1, 1, 1, 1], charge=False)
    _, paid = _run([0, 1, 1, 1, 1, 1], charge=True)
    # .sum(), not .iloc[0]: the charge lands on the bar the position opened on,
    # which is not the first row of the evaluation window.
    assert paid.pnl.sum() < free.pnl.sum()


def test_entry_is_charged_at_the_price_transacted():
    """Charged at row.open (the fill), not at prev.close like the exit sites."""
    sleeve, _ = _run([0, 1, 1, 1, 1, 1], charge=True)
    expected = -obs._half_spread(sleeve.instrument, sleeve.units,
                                 float(sleeve.entry), sleeve.markq)
    assert sleeve.spread_paid == pytest.approx(expected, rel=1e-6)


def test_a_reversal_pays_a_full_spread():
    """_half_spread's docstring: 'a reversal pays a full spread'. One entry plus
    one flip-close is two halves — the exit half already existed, the entry half
    is what this change adds."""
    one_entry, _ = _run([0, 1, 1, 1, 1, 1], charge=True)
    reversal, _ = _run([0, 1, 1, -1, -1, -1], charge=True)
    assert reversal.entries > one_entry.entries
    assert reversal.spread_paid < one_entry.spread_paid


def test_the_charge_reaches_the_book_pnl_not_just_the_sleeve_counter():
    """Every other charge site updates the book pnl AND the per-sleeve counters.
    An entry that moved only the counters would leave spread_paid looking right
    while the headline return stayed optimistic — the exact shape of the defect
    this fixes."""
    sleeve, paid = _run([0, 1, 1, 1, 1, 1], charge=True)
    _, free = _run([0, 1, 1, 1, 1, 1], charge=False)
    delta = paid.pnl.sum() - free.pnl.sum()
    assert delta == pytest.approx(sleeve.spread_paid + sleeve.comm_paid, rel=1e-6)
