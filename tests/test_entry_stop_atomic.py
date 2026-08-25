"""The stop must ride on the ORDER, not follow it — on cTrader only.

Every failure this file's siblings document lives in the same window: the gap
between "the position exists at the broker" and "the position is protected at the
broker". place_stop confirming the attach makes that window VISIBLE; attaching at
entry removes it. A ProtoOANewOrderReq carrying stopLoss is atomic — the broker
either creates the position with the stop or rejects the order — so a wrong-side
stop costs the entry instead of the protection, which is the right way round: a
stop on the wrong side of the fill IS the stop-out condition.

The venue gate is load-bearing, not caution. FixAdapter.execute_order sends the
stop as a SEPARATE order after the fill and RAISES when it is rejected, leaving a
filled unstopped position and an exception mid-sleeve — strictly worse than
attaching afterwards and checking. VENUE=fix is the documented rollback, so it has
to keep exactly today's behaviour.
"""
import pytest

import fix_runner as fr


class _Ad:
    def __init__(self):
        self.calls = []

    def open_pos_ids(self):
        return {}

    def execute_order(self, signed_units, tag, stop_loss=None):
        self.calls.append(('open', signed_units, tag, stop_loss))
        return 'P-NEW'

    def place_stop(self, pos_id, units, side, stop_px):
        self.calls.append(('stop', pos_id, round(stop_px, 4)))
        return {'ord_status': '0', 'ref': str(pos_id)}

    def cancel_stop(self, ref, side):
        return {'ord_status': '0'}

    def close_position(self, pos_id, units, side):
        self.calls.append(('close', pos_id, units, side))
        return {'ord_status': '2'}


class _Price:
    def get_current_price(self):
        return 100.0


@pytest.fixture
def wired(monkeypatch, tmp_path):
    monkeypatch.setattr(fr, 'STATE_FILE', str(tmp_path / 'state.json'))
    monkeypatch.setattr(fr, 'HALT_FILE', str(tmp_path / 'halt.json'))
    monkeypatch.setattr(fr, 'min_lot_implied_risk', lambda *a, **k: (10, (10, 10), 0.004))
    monkeypatch.setattr(fr, 'size_units', lambda *a, **k: (10, (10, 10)))
    monkeypatch.setattr(fr, 'q2usd', lambda inst: 1.0)
    monkeypatch.setattr(fr, 'maybe_reconcile', lambda *a, **k: None)

    def _run(sig, ad, venue):
        monkeypatch.setattr(fr, 'VENUE', venue)
        monkeypatch.setattr(fr, 'latest', lambda s: (sig, 100.0, 1.0, 1.0))
        sleeve = {'sid': 'nas100_x', 'inst': 'NAS100_USD', 'params': {'stop_mult': 2},
                  'fn': lambda df, params: [0], 'ws': 1, 'decay_kelly_scale': 1}
        state = {'nas100_x': fr.FLAT(0)}
        fr.run_once([sleeve], state, True,
                    {'fix': {'NAS100_USD': ad}, 'price': {'NAS100_USD': _Price()},
                     'equity': lambda: 100_000.0})
        return state
    return _run


def test_on_ctrader_the_stop_rides_on_the_order(wired):
    """THE point. Live price 100.0, stop_mult 2, ATR 1.0 -> stop 98.0, and it is
    on the entry itself, not a follow-up amend."""
    ad = _Ad()
    wired(1, ad, 'ctrader')

    opens = [c for c in ad.calls if c[0] == 'open']
    assert len(opens) == 1
    assert opens[0][3] == pytest.approx(98.0), 'the order must carry the stop'


def test_the_entry_uses_the_venue_gate_rather_than_the_raw_stop(wired,
                                                                monkeypatch):
    """VENUE=fix is the documented rollback and must keep today's behaviour, where
    FixAdapter places a separate stop order after the fill and RAISES on rejection.

    Asserted through the gate rather than by driving run_once with VENUE='fix':
    that path calls _refresh_marks, which needs the whole FIX session object, and a
    fake rich enough to satisfy it would be testing the mock. What matters is that
    the call site reads _entry_stop and passes whatever it returns."""
    monkeypatch.setattr(fr, '_entry_stop', lambda px: 'SENTINEL')
    ad = _Ad()
    wired(1, ad, 'ctrader')

    opens = [c for c in ad.calls if c[0] == 'open']
    assert opens[0][3] == 'SENTINEL', 'the entry must go through the venue gate'


def test_the_follow_up_attach_still_runs_and_confirms(wired):
    """Belt and braces, deliberately: on cTrader the amend re-states a stop the
    order already carries, and place_stop's read-back is then the PROOF that the
    atomic attach actually landed. Cheap, and the only evidence that exists."""
    ad = _Ad()
    state = wired(1, ad, 'ctrader')

    assert [c[0] for c in ad.calls] == ['open', 'stop']
    assert fr._stop_ok(state['nas100_x']['stop_ref'])


def test_a_short_carries_its_stop_too(wired):
    ad = _Ad()
    wired(-1, ad, 'ctrader')

    opens = [c for c in ad.calls if c[0] == 'open']
    assert opens[0][1] == -10
    assert opens[0][3] == pytest.approx(102.0)      # 100 + 1*2*1.0


@pytest.mark.parametrize('venue,expected', [('ctrader', 98.0), ('fix', None)])
def test_entry_stop_is_the_only_venue_switch(venue, expected, monkeypatch):
    """The gate is one function, so it cannot drift apart across the three call
    sites that use it."""
    monkeypatch.setattr(fr, 'VENUE', venue)
    got = fr._entry_stop(98.0)
    assert got == expected if expected is None else got == pytest.approx(expected)
