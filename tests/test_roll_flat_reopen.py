"""Does the ordinary trading pass reopen what the pre-roll close flattened?
(roll-flat ticket 04.)

The whole policy is costed on there being NO reopen pass — a covered sleeve is
closed at the broker's 23:50 and re-established by the trading pass that already
runs after the roll, because `FLAT(0)` reads as a signal change. That assumption
had never been exercised: `flatten_all`'s FLAT(0) is tested where it is written,
and `acts_on_signal` is tested as a predicate, but nothing drove `run_once` from
a policy-closed state through to a broker order.

These drive the real `run_once` with fake adapters. They cover the three things
that would make the reopen worse than not closing at all:

  * reopening WITHOUT a broker-side stop — under netting the software stop is
    evaluated per-bar inside a loop that may not be running, so an unstopped
    position is genuinely unprotected
  * reopening a sleeve that went flat for a REAL reason (a stop fired), which is
    the divergence commit 58c1a6f removed
  * opening at all while the guard's halt is latched — the gate whose predicate
    is tested but whose single `trade = False` wiring is not

What they cannot cover is a real fill: size against a real min-lot, the stop read
back from the broker, and the spread actually paid at the roll. That needs the
policy armed on the pod and is ticket 07's evidence.
"""
import json

import pytest

import fix_runner as fr


class _Ad:
    """Records what the runner asked the broker to do."""
    symbol = 'NAS100'

    def __init__(self, entry_ok=True, stop_ok=True):
        self.calls = []
        self._entry_ok, self._stop_ok = entry_ok, stop_ok

    def open_pos_ids(self):
        return {}

    def cancel_stop(self, ref, side):
        self.calls.append(('cancel', ref, side))
        return {'ord_status': '4'}

    def close_position(self, pos_id, units, side):
        self.calls.append(('close', pos_id, units, side))
        return {'ord_status': '2'}

    def execute_order(self, signed_units, tag):
        self.calls.append(('open', signed_units, tag))
        return 'P-NEW' if self._entry_ok else None

    def place_stop(self, pid, units, side, stop_px):
        self.calls.append(('stop', pid, units, side, round(stop_px, 4)))
        return {'ord_status': '0'} if self._stop_ok else {'ord_status': '8'}


class _Price:
    def get_current_price(self):
        return 100.0


@pytest.fixture
def sleeve():
    return {'sid': 'nas100_x', 'inst': 'NAS100_USD', 'params': {'stop_mult': 2},
            'fn': lambda df, params: [0], 'ws': 1, 'decay_kelly_scale': 1}


@pytest.fixture
def wired(monkeypatch, tmp_path):
    """The runner, with market data and sizing pinned so the assertions are about
    the DECISION, not about the data feed."""
    monkeypatch.setattr(fr, 'STATE_FILE', str(tmp_path / 'state.json'))
    monkeypatch.setattr(fr, 'HALT_FILE', str(tmp_path / 'halt.json'))
    monkeypatch.setattr(fr, 'min_lot_implied_risk',
                        lambda *a, **k: (10, (10, 10), 0.004))
    monkeypatch.setattr(fr, 'size_units', lambda *a, **k: (10, (10, 10)))
    monkeypatch.setattr(fr, 'q2usd', lambda inst: 1.0)
    monkeypatch.setattr(fr, 'maybe_reconcile', lambda *a, **k: None)

    def _run(sleeve, state, sig, ad):
        monkeypatch.setattr(fr, 'latest', lambda s: (sig, 100.0, 1.0, 1.0))
        fr.run_once([sleeve], state, True,
                    {'fix': {'NAS100_USD': ad}, 'price': {'NAS100_USD': _Price()},
                     'equity': lambda: 100_000.0})
    return _run


class TestTheReopen:
    def test_a_policy_closed_sleeve_reopens_with_the_stop_attached(self, wired,
                                                                   sleeve):
        """The load-bearing assumption. FLAT(0) + a live signal = an order, and
        the stop goes on in the same pass."""
        state = {'nas100_x': fr.FLAT(0)}
        ad = _Ad()
        wired(sleeve, state, 1, ad)

        kinds = [c[0] for c in ad.calls]
        assert kinds == ['open', 'stop']
        assert ad.calls[0][1] == 10                      # +10u, i.e. BUY 10
        # Stop from the LIVE price (100.0), not the stale close: 100 - 1*2*1.0
        assert ad.calls[1][4] == pytest.approx(98.0)
        assert state['nas100_x']['pos_id'] == 'P-NEW'
        assert state['nas100_x']['stop_ref'] == {'ord_status': '0'}
        assert state['nas100_x']['signal'] == 1

    def test_it_reopens_on_the_current_signal_not_the_one_it_closed_on(self,
                                                                      wired,
                                                                      sleeve):
        """FLAT(0) carries no intent (ticket 02), so if the signal flipped
        between the close and the reopen, the reopen follows the NEW one. A
        policy that re-established the old side would be trading yesterday."""
        state = {'nas100_x': fr.FLAT(0)}
        ad = _Ad()
        wired(sleeve, state, -1, ad)
        assert ad.calls[0][:2] == ('open', -10)          # SELL, not BUY
        assert state['nas100_x']['side'] == -1

    def test_a_flat_signal_reopens_nothing(self, wired, sleeve):
        """Closed before the roll, and by the reopen the strategy says flat.
        There is nothing to re-establish and no order should be sent."""
        state = {'nas100_x': fr.FLAT(0)}
        ad = _Ad()
        wired(sleeve, state, 0, ad)
        assert ad.calls == []
        assert state['nas100_x']['pos_id'] is None


class TestItDoesNotReopenWhatItDidNotClose:
    def test_a_stopped_out_sleeve_is_not_reopened(self, wired, sleeve):
        """THE NEGATIVE TEST. A stop fired overnight, so the sleeve is
        FLAT(signal) with the signal unchanged — re-entering here is exactly the
        divergence from the validated return stream that 58c1a6f removed."""
        state = {'nas100_x': fr.FLAT(1)}
        ad = _Ad()
        wired(sleeve, state, 1, ad)
        assert ad.calls == []
        assert state['nas100_x']['signal'] == 1

    def test_but_a_genuine_flip_after_a_stop_still_trades(self, wired, sleeve):
        """The positive control: the negative test above must not be achieved by
        freezing the sleeve."""
        state = {'nas100_x': fr.FLAT(1)}
        ad = _Ad()
        wired(sleeve, state, -1, ad)
        assert [c[0] for c in ad.calls] == ['open', 'stop']
        assert state['nas100_x']['side'] == -1


class TestTheHaltGateWiring:
    """Ticket 02 handed this over: `halt_is_active` is tested as a predicate, but
    the one line in `run_once` that acts on it (`trade = False`) was not. Without
    it the post-roll pass would re-open the entire book minutes after the guard
    flattened it, and the breaker would look like it fired and changed nothing.
    """

    def test_a_latched_halt_blocks_the_reopen(self, wired, sleeve, monkeypatch):
        import prop_guard
        from datetime import datetime, timezone
        monkeypatch.setattr(fr, 'GUARD_ENABLED', True)
        today = prop_guard._trading_day(datetime.now(timezone.utc))
        json.dump({'kind': 'daily', 'day': today, 'dd': -0.024},
                  open(fr.HALT_FILE, 'w'))

        state = {'nas100_x': fr.FLAT(0)}
        ad = _Ad()
        wired(sleeve, state, 1, ad)
        assert ad.calls == []

    def test_yesterdays_daily_halt_does_not_block_today(self, wired, sleeve,
                                                        monkeypatch):
        """A daily halt is a PAUSE keyed on the broker trading day. In US summer
        that day rolls at the same instant as the swap roll, so a halt latched
        before the close does NOT bind the reopen after it — which is correct,
        the firm's daily loss resets at the same moment."""
        monkeypatch.setattr(fr, 'GUARD_ENABLED', True)
        json.dump({'kind': 'daily', 'day': '1999-01-01', 'dd': -0.024},
                  open(fr.HALT_FILE, 'w'))

        state = {'nas100_x': fr.FLAT(0)}
        ad = _Ad()
        wired(sleeve, state, 1, ad)
        assert [c[0] for c in ad.calls] == ['open', 'stop']

    def test_a_total_halt_blocks_regardless_of_the_day(self, wired, sleeve,
                                                       monkeypatch):
        monkeypatch.setattr(fr, 'GUARD_ENABLED', True)
        json.dump({'kind': 'total', 'day': '1999-01-01', 'dd': -0.081},
                  open(fr.HALT_FILE, 'w'))

        state = {'nas100_x': fr.FLAT(0)}
        ad = _Ad()
        wired(sleeve, state, 1, ad)
        assert ad.calls == []


class TestAReopenThatCannotBeProtected:
    def test_a_rejected_stop_is_retried_not_left_bare(self, wired, sleeve):
        """A reopen without a broker stop is worse than not reopening: under
        netting the software stop only runs while the loop does. The runner must
        not accept a rejected stop as attached."""
        state = {'nas100_x': fr.FLAT(0)}
        ad = _Ad(stop_ok=False)
        wired(sleeve, state, 1, ad)

        assert [c[0] for c in ad.calls].count('stop') > 1     # retried
        assert state['nas100_x']['stop_ref'] is None          # never claimed OK
        assert state['nas100_x']['stop'] == pytest.approx(98.0)   # software stop armed

    def test_a_failed_entry_keeps_the_signal_so_the_next_pass_retries(self,
                                                                     wired,
                                                                     sleeve):
        """The 2026-07-28 failure mode, on the reopen path: an entry rejected
        inside the index close must NOT advance the recorded signal, or the
        sleeve sits flat with a live signal it can never act on."""
        state = {'nas100_x': fr.FLAT(0)}
        ad = _Ad(entry_ok=False)
        wired(sleeve, state, 1, ad)
        assert state['nas100_x']['pos_id'] is None
        assert state['nas100_x']['signal'] == 0               # still FLAT(0)
        assert fr.acts_on_signal(1, state['nas100_x']) is True    # retried
