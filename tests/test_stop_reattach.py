"""The re-attach backstop must not accept a rejected stop.

Block (2b) of run_once exists to self-heal a position the broker never stopped:
every pass it re-derives a stop from the CURRENT price and tries again. It was
the one place_stop call site still guarded by a bare `if ref:` rather than
`_stop_ok(ref)` — the exact defect _stop_ok was written to prevent.

That was survivable only while place_stop could not SEE a refusal: it almost
never returned a reject, so the bare truthiness test almost never fired. Making
place_stop confirm the attach turned a rejected retry into the routine outcome,
and with it this branch would have stored {'ord_status': '8', 'reject': ...} as
stop_ref and printed "✓ broker stop attached on retry". A stop_ref carrying
neither 'order_id' nor 'ref' makes cancel_stop return None, and the runner then
refuses to ever close the position — the 2026-07-28 failure mode, re-armed.
"""
import pytest

import fix_runner as fr


class _Ad:
    """Holds one position at the broker; its stop attach is switchable."""

    def __init__(self, stop_ok):
        self.calls = []
        self._stop_ok = stop_ok

    def open_pos_ids(self):
        return {'P-1': 10.0}

    def place_stop(self, pos_id, units, side, stop_px):
        self.calls.append(('stop', pos_id, round(stop_px, 4)))
        return ({'ord_status': '0', 'ref': str(pos_id)} if self._stop_ok
                else {'ord_status': '8', 'reject': 'wrong side of market'})

    def cancel_stop(self, ref, side):
        self.calls.append(('cancel', ref, side))
        return {'ord_status': '0'}

    def close_position(self, pos_id, units, side):
        self.calls.append(('close', pos_id, units, side))
        return {'ord_status': '2'}

    def execute_order(self, signed_units, tag):
        self.calls.append(('open', signed_units, tag))
        return 'P-NEW'


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

    def _run(state, sig, ad):
        sleeve = {'sid': 'nas100_x', 'inst': 'NAS100_USD', 'params': {'stop_mult': 2},
                  'fn': lambda df, params: [0], 'ws': 1, 'decay_kelly_scale': 1}
        monkeypatch.setattr(fr, 'latest', lambda s: (sig, 100.0, 1.0, 1.0))
        fr.run_once([sleeve], state, True,
                    {'fix': {'NAS100_USD': ad}, 'price': {'NAS100_USD': _Price()},
                     'equity': lambda: 100_000.0})
    return _run


def _unstopped():
    """Tracked position, no broker stop — what a rejected attach leaves behind."""
    return {'signal': 1, 'pos_id': 'P-1', 'units': 10.0, 'side': 1,
            'stop': 98.0, 'stop_ref': None}


def test_a_rejected_retry_is_not_recorded_as_attached(wired):
    """THE regression. A reject must leave stop_ref None so the NEXT pass retries."""
    state = {'nas100_x': _unstopped()}
    ad = _Ad(stop_ok=False)
    wired(state, 1, ad)

    assert state['nas100_x']['stop_ref'] is None, 'a reject must never become a stop_ref'
    assert state['nas100_x']['pos_id'] == 'P-1', 'the position is still held'


def test_a_rejected_retry_does_not_ratchet_the_software_stop(wired):
    """The success path re-anchors st['stop'] to the current price. Doing that on
    a FAILED retry too would walk the software stop up every pass — a trailing
    stop nobody chose."""
    state = {'nas100_x': _unstopped()}
    wired(state, 1, _Ad(stop_ok=False))

    assert state['nas100_x']['stop'] == pytest.approx(98.0)


def test_a_successful_retry_still_self_heals(wired):
    """The positive control: the guard must not be achieved by never attaching."""
    state = {'nas100_x': _unstopped()}
    ad = _Ad(stop_ok=True)
    wired(state, 1, ad)

    assert fr._stop_ok(state['nas100_x']['stop_ref'])
    # Re-derived from the CURRENT price (100.0 - 1*2*1.0), not the stale carried stop.
    assert state['nas100_x']['stop'] == pytest.approx(98.0)
    assert ('stop', 'P-1', 98.0) in ad.calls


def test_the_poisoned_ref_would_have_jammed_the_close(wired):
    """Why it matters beyond the lie: cancel_stop refuses a ref carrying neither
    'order_id' nor 'ref', and flatten_all/close both abort on that."""
    import ctrader_exec
    ad = object.__new__(ctrader_exec.CTraderExecAdapter)
    assert ad.cancel_stop({'ord_status': '8', 'reject': 'wrong side'}, 1) is None
