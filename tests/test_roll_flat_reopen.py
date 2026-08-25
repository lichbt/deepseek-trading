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

    def execute_order(self, signed_units, tag, stop_loss=None):
        self.calls.append(('open', signed_units, tag, stop_loss))
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
    def test_a_rejected_stop_closes_the_position_rather_than_holding_it(self,
                                                                        wired,
                                                                        sleeve):
        """SUPERSEDES "…is retried not left bare", which asserted that a rejected
        stop left the position open behind the software stop. That was never a
        fallback under RUNNER_MODE=cron: run_once fires only on the external
        trigger, so the software stop next evaluates a WHOLE DAY later. i1 proved
        it — 73 points through its own stop on 2026-08-24 and still open.

        The retry is kept; what changed is the outcome when it also fails."""
        state = {'nas100_x': fr.FLAT(0)}
        ad = _Ad(stop_ok=False)
        wired(sleeve, state, 1, ad)

        assert [c[0] for c in ad.calls].count('stop') > 1     # still retried
        assert ad.calls[-1][0] == 'close', 'an unprotectable position is closed'
        assert state['nas100_x']['pos_id'] is None
        assert state['nas100_x']['signal'] == 1   # FLAT(sig): no churn next pass

    def test_a_rejected_stop_AND_a_refused_close_keeps_the_software_stop(self,
                                                                        wired,
                                                                        sleeve):
        """The honest fallback. If the close is refused too there is nothing left
        to do but hold it and say so loudly — never claim the stop attached."""
        state = {'nas100_x': fr.FLAT(0)}
        ad = _Ad(stop_ok=False)
        ad.close_position = lambda *a: {'ord_status': '8'}
        wired(sleeve, state, 1, ad)

        assert state['nas100_x']['pos_id'] == 'P-NEW'
        assert state['nas100_x']['stop_ref'] is None
        assert state['nas100_x']['stop'] == pytest.approx(98.0)

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


class TestTheGateRunsOnTheFillNotTheReference:
    """LIVE 2026-08-25, nas100usd_..._164540_i1 on the prop book.

    `roll_flat_resume` decides against `entry_ref` — the price read BEFORE the
    order is sent. NAS was mid-slide at the reopen (the 00:15 M5 bar ran
    28984.4 -> 28959.5), so the gate saw 28969 against a carried stop of 28954.46
    and said 'resume', and the fill landed at 28953.35. That put the carried stop
    on the WRONG SIDE of a long: the broker refused the amend, and the position
    ran bare while the log said "stop@broker OK".

    The reference and the fill are two different instants and only one of them
    decides whether the model is still in the trade.
    """

    class _AdFill(_Ad):
        """An adapter that reports the broker's own fill price."""

        def __init__(self, fill, close_ok=True, **kw):
            super().__init__(**kw)
            self._fill, self._close_ok = fill, close_ok

        def position_entry(self, pos_id):
            return self._fill

        def close_position(self, pos_id, units, side):
            self.calls.append(('close', pos_id, units, side))
            return {'ord_status': '2'} if self._close_ok else {'ord_status': '8'}

    @staticmethod
    def _carried(stop=99.0):
        day = fr.datetime.now(fr.timezone.utc).strftime('%Y-%m-%d')
        return {'signal': 0, 'pos_id': None, 'units': 0.0, 'side': 0,
                'stop': None, 'stop_ref': None, 'carry_stop': stop,
                'carry_units': 10.0, 'carry_side': 1, 'carry_day': day}

    def test_a_fill_through_the_carried_stop_is_closed_not_left_bare(self,
                                                                    wired,
                                                                    sleeve):
        """THE regression. Reference 100.0 clears the carried stop of 99.0, the
        fill at 98.5 does not — so the model exited during the gap and live must
        not be holding anything."""
        state = {'nas100_x': self._carried()}
        ad = self._AdFill(fill=98.5)
        wired(sleeve, state, 1, ad)

        kinds = [c[0] for c in ad.calls]
        assert kinds == ['open', 'close'], 'must not attach a wrong-side stop'
        assert state['nas100_x']['pos_id'] is None
        assert state['nas100_x']['signal'] == 1     # FLAT(sig): a fired stop's shape

    def test_a_fill_that_holds_above_the_carried_stop_still_resumes(self, wired,
                                                                    sleeve):
        """The positive control — the gate must not be achieved by never
        resuming."""
        state = {'nas100_x': self._carried()}
        ad = self._AdFill(fill=99.5)
        wired(sleeve, state, 1, ad)

        assert [c[0] for c in ad.calls] == ['open', 'stop']
        assert ad.calls[1][4] == pytest.approx(99.0)          # the CARRIED stop
        assert state['nas100_x']['pos_id'] == 'P-NEW'

    def test_a_refused_close_holds_on_the_fresh_atr_stop(self, wired, sleeve):
        """The close can be refused inside a session break (2026-08-10). Leaving
        the position behind the carried stop would leave it bare, because that is
        the stop the broker just refused — so it falls back to the ATR stop, which
        cannot be wrong-side."""
        state = {'nas100_x': self._carried()}
        ad = self._AdFill(fill=98.5, close_ok=False)
        wired(sleeve, state, 1, ad)

        assert [c[0] for c in ad.calls] == ['open', 'close', 'stop']
        assert ad.calls[2][4] == pytest.approx(98.0)          # fresh, not 99.0
        assert state['nas100_x']['pos_id'] == 'P-NEW'
        assert state['nas100_x']['stop'] == pytest.approx(98.0)

    def test_an_adapter_without_position_entry_is_untouched(self, wired, sleeve):
        """VENUE=fix is the default and the documented rollback, and FixAdapter
        has no position_entry. The re-check must be inert there, not an
        AttributeError that skips the sleeve."""
        state = {'nas100_x': self._carried()}
        ad = _Ad()
        wired(sleeve, state, 1, ad)

        assert [c[0] for c in ad.calls] == ['open', 'stop']
        assert ad.calls[1][4] == pytest.approx(99.0)
