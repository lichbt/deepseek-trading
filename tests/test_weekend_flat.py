"""Does weekend-flat close on Friday and then STAY out?

The arm is costed entirely on there being NO re-entry: the +18.02% / -2.65% maxDD
figure comes from a simulator where the weekend close leaves `prev_target` alone, so
the sleeve waits for a genuine signal flip. If the runner cleared the signal instead
— which is what every other close path in this file does — the next pass would order
into a market shut until Sunday, and the measured numbers would describe nothing.

That makes `preserve_signal` the whole policy, and it fails SILENTLY in both
directions: clear it and the book re-enters immediately, preserve it on the guard's
halt and the book sits out for weeks. So it is pinned here from both sides.

These drive the real `flatten_all` and `weekend_flat_close` with fake adapters. What
they cannot cover is a real Friday fill — the spread actually paid at the weekly
close, and whether the venue's Friday session end is where the schedule says. That
needs the arm armed on the pod.
"""
import json
from datetime import datetime, timezone

import pytest

import fix_runner as fr


class _Ad:
    """Records what the runner asked the broker to do."""
    symbol = 'XAGUSD'

    def __init__(self, close_ok=True, stop_ok=True):
        self.calls = []
        self._close_ok, self._stop_ok = close_ok, stop_ok

    def cancel_stop(self, ref, side):
        self.calls.append(('cancel', ref, side))
        return {'ord_status': '4'}

    def close_position(self, pos_id, units, side):
        self.calls.append(('close', pos_id, units, side))
        return {'ord_status': '2'} if self._close_ok else {'ord_status': '8'}

    def place_stop(self, pid, units, side, stop_px):
        self.calls.append(('stop', pid, units, side, stop_px))
        return {'ord_status': '0'} if self._stop_ok else {'ord_status': '8'}

    def session_intervals(self):
        return []


def _held(sid='xag_x', inst='XAG_USD', signal=1):
    return {sid: {'signal': signal, 'pos_id': 'P-1', 'units': 50.0, 'side': 1,
                  'stop': 40.0, 'stop_ref': 'S-1', 'inst': inst}}


def _adapters(ad, inst='XAG_USD'):
    return {'fix': {inst: ad}, 'price': {}, 'equity': lambda: 100_000.0}


# A Friday inside the pre-roll window. The broker clock is America/New_York + 7h,
# so 20:45 UTC Friday is 23:45 Friday there — 15 min before broker midnight, inside
# the default 20-minute lead and outside the 3-minute grace.
FRIDAY = datetime(2026, 8, 7, 20, 45, tzinfo=timezone.utc)
THURSDAY = datetime(2026, 8, 6, 20, 45, tzinfo=timezone.utc)


@pytest.fixture
def armed(monkeypatch, tmp_path):
    monkeypatch.setattr(fr, 'WEEKEND_FLAT', True)
    monkeypatch.setattr(fr, 'WEEKEND_FLAT_INSTS', {'XAG_USD'})
    monkeypatch.setattr(fr, 'WEEKEND_FLAT_FILE', str(tmp_path / 'wf.json'))
    return tmp_path


class TestTheSurrender:
    def test_it_preserves_the_signal_so_nothing_re_enters(self, armed):
        """THE load-bearing assertion. FLAT(signal), never FLAT(0)."""
        state, ad = _held(signal=1), _Ad()
        out = fr.weekend_flat_close(state, _adapters(ad), True, now=FRIDAY)

        assert out is not None and out[1] == []
        assert [c[0] for c in ad.calls] == ['cancel', 'close']
        assert state['xag_x']['pos_id'] is None
        assert state['xag_x']['signal'] == 1, "signal cleared — would re-enter Monday"
        # ...and the runner therefore declines to act while the strategy agrees.
        assert fr.acts_on_signal(1, state['xag_x']) is False

    def test_a_genuine_flip_does_re_enter(self, armed):
        """Surrender is not a freeze: the sleeve is available again the moment the
        strategy says something new."""
        state, ad = _held(signal=1), _Ad()
        fr.weekend_flat_close(state, _adapters(ad), True, now=FRIDAY)
        assert fr.acts_on_signal(-1, state['xag_x']) is True
        assert fr.acts_on_signal(0, state['xag_x']) is True

    def test_a_short_preserves_its_own_signal_not_a_constant(self, armed):
        state, ad = _held(signal=-1), _Ad()
        fr.weekend_flat_close(state, _adapters(ad), True, now=FRIDAY)
        assert state['xag_x']['signal'] == -1
        assert fr.acts_on_signal(-1, state['xag_x']) is False


class TestWhenItFires:
    def test_it_does_not_fire_on_a_thursday(self, armed):
        state, ad = _held(), _Ad()
        assert fr.weekend_flat_close(state, _adapters(ad), True, now=THURSDAY) is None
        assert ad.calls == []
        assert state['xag_x']['pos_id'] == 'P-1'

    def test_it_does_not_fire_when_disarmed(self, armed, monkeypatch):
        monkeypatch.setattr(fr, 'WEEKEND_FLAT', False)
        state, ad = _held(), _Ad()
        assert fr.weekend_flat_close(state, _adapters(ad), True, now=FRIDAY) is None
        assert ad.calls == []

    def test_it_latches_so_it_fires_once_per_weekend(self, armed):
        state, ad = _held(), _Ad()
        fr.weekend_flat_close(state, _adapters(ad), True, now=FRIDAY)
        assert json.load(open(fr.WEEKEND_FLAT_FILE))['closed'] == ['xag_x']
        # Second poll in the same window: latched, so no second attempt.
        state2, ad2 = _held(), _Ad()
        assert fr.weekend_flat_close(state2, _adapters(ad2), True, now=FRIDAY) is None
        assert ad2.calls == []

    def test_it_leaves_instruments_outside_the_scope_alone(self, armed):
        state = _held(sid='nas_x', inst='NAS100_USD')
        ad = _Ad()
        out = fr.weekend_flat_close(state, {'fix': {'NAS100_USD': ad}}, True,
                                    now=FRIDAY)
        assert out == ([], [])
        assert ad.calls == []
        assert state['nas_x']['pos_id'] == 'P-1'


class TestRejection:
    def test_a_rejected_close_re_attaches_the_stop_and_does_not_latch(self, armed):
        """The 2026-08-10 defect, on this path: a cancel that is not followed by a
        close must be undone, or the position sits bare until the next daily pass."""
        state, ad = _held(), _Ad(close_ok=False)
        closed, failed = fr.weekend_flat_close(state, _adapters(ad), True,
                                               now=FRIDAY)

        assert closed == [] and len(failed) == 1
        assert 'stop re-attached' in failed[0][1]
        assert [c[0] for c in ad.calls] == ['cancel', 'close', 'stop']
        assert state['xag_x']['pos_id'] == 'P-1', "position dropped from state"
        assert state['xag_x']['stop_ref'] == {'ord_status': '0'}
        # No latch: the next poll inside the window retries.
        import os
        assert not os.path.exists(fr.WEEKEND_FLAT_FILE)


class TestTheOtherKindOfFlat:
    """preserve_signal must NOT leak into the paths that need a re-establish."""

    def test_the_guards_halt_still_clears_the_signal(self):
        state, ad = _held(signal=1), _Ad()
        fr.flatten_all(state, _adapters(ad), True, 'halt')
        assert state['xag_x']['signal'] == 0
        assert fr.acts_on_signal(1, state['xag_x']) is True, \
            "guard halt preserved the signal — the book would sit out for weeks"

    def test_roll_flat_still_clears_the_signal(self):
        state, ad = _held(signal=1), _Ad()
        fr.flatten_all(state, _adapters(ad), True, 'roll-flat x',
                       only={'XAG_USD'}, tag='roll-flat')
        assert state['xag_x']['signal'] == 0

    def test_the_dry_run_path_honours_it_too(self):
        """live=False takes a separate branch and once hardcoded FLAT(0)."""
        state, _ = _held(signal=1), None
        fr.flatten_all(state, None, False, 'weekend-flat x', preserve_signal=True)
        assert state['xag_x']['signal'] == 1
        state2 = _held(signal=1)
        fr.flatten_all(state2, None, False, 'halt')
        assert state2['xag_x']['signal'] == 0
