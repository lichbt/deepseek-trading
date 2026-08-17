"""Does a roll-flat reopen resume the SAME trade, or silently start a new one?

Roll-flat is one trade with a gap in it. oanda_book_simulator says so outright —
"the POSITION is deliberately left intact: signal state, stop and entry are
unchanged" — and every roll-flat figure in the repo is measured that way.

The runner used to disagree. FLAT(0) nulls `stop`, so the reopen computed a fresh
stop off the new entry price and a fresh size off the current ATR. That is a
RE-ANCHORED stop, not a trailing one, and it leaves the validated stream in BOTH
directions — which is why both are pinned here rather than just the loud one:

  after a FAVOURABLE move   the reopen's stop is TIGHTER in absolute terms, so the
                            sleeve stops out on a day the backtest never exits.
  after an ADVERSE move     the reopen's stop is LOOSER, so the backtest exits and
                            live keeps bleeding.

The case that makes it faithful rather than merely stop-preserving is the third
one: if price passed the carried stop while the sleeve was flat, the model already
exited during that gap, so the runner must NOT reopen.
"""
import json
from datetime import datetime, timezone, timedelta

import pytest

import fix_runner as fr


FRIDAY = datetime(2026, 8, 7, 20, 45, tzinfo=timezone.utc)   # inside the pre-roll window


class _Ad:
    symbol = 'NAS100'

    def __init__(self):
        self.calls = []

    def cancel_stop(self, ref, side):
        self.calls.append(('cancel', ref, side)); return {'ord_status': '4'}

    def close_position(self, pos_id, units, side):
        self.calls.append(('close', pos_id, units, side)); return {'ord_status': '2'}

    def place_stop(self, pid, units, side, px):
        self.calls.append(('stop', pid, units, side, px)); return {'ord_status': '0'}

    def session_intervals(self):
        return []


def _held(signal=1, stop=95.0, units=10.0):
    return {'nas_x': {'signal': signal, 'pos_id': 'P-1', 'units': units,
                      'side': signal, 'stop': stop, 'stop_ref': 'S-1',
                      'inst': 'NAS100_USD', 'entry': 100.0}}


@pytest.fixture
def armed(monkeypatch, tmp_path):
    monkeypatch.setattr(fr, 'ROLL_FLAT', True)
    monkeypatch.setattr(fr, 'ROLL_FLAT_INSTS', {'NAS100_USD'})
    monkeypatch.setattr(fr, 'ROLL_FLAT_FILE', str(tmp_path / 'rf.json'))
    monkeypatch.setattr(fr, 'STATE_FILE', str(tmp_path / 'state.json'))
    return tmp_path


class TestTheCloseCarries:
    def test_it_hands_stop_units_and_side_to_the_reopen(self, armed):
        state = _held(stop=95.0, units=10.0)
        fr.roll_flat_close(state, {'fix': {'NAS100_USD': _Ad()}}, True, now=FRIDAY)
        st = state['nas_x']
        assert st['signal'] == 0, "roll-flat must still clear the signal, or nothing reopens"
        assert st['pos_id'] is None
        assert st['carry_stop'] == 95.0 and st['carry_units'] == 10.0
        assert st['carry_side'] == 1 and st['carry_day']

    def test_the_guards_halt_carries_NOTHING(self, armed):
        """A halt is not a gap in one trade — it is the book being taken off. It must
        not resurrect an old stop when the book comes back."""
        state = _held()
        fr.flatten_all(state, {'fix': {'NAS100_USD': _Ad()}}, True, 'halt')
        assert 'carry_stop' not in state['nas_x']

    def test_weekend_flat_carries_nothing_either(self, armed):
        state = _held()
        fr.flatten_all(state, {'fix': {'NAS100_USD': _Ad()}}, True, 'weekend-flat x',
                       only={'NAS100_USD'}, tag='weekend-flat', preserve_signal=True)
        assert 'carry_stop' not in state['nas_x']


class TestFreshness:
    def test_yesterdays_carry_is_honoured(self):
        y = (fr.datetime.now(timezone.utc) - timedelta(days=1)).strftime('%Y-%m-%d')
        assert fr._carry_is_fresh(y) is True

    def test_a_stale_carry_is_refused(self):
        old = (fr.datetime.now(timezone.utc) - timedelta(days=6)).strftime('%Y-%m-%d')
        assert fr._carry_is_fresh(old) is False

    def test_missing_or_malformed_is_refused(self):
        assert fr._carry_is_fresh(None) is False
        assert fr._carry_is_fresh('not-a-date') is False


def _carried(stop=95.0, units=10.0, side=1, day=None):
    day = day or fr.datetime.now(timezone.utc).strftime('%Y-%m-%d')
    return {'signal': 0, 'pos_id': None, 'units': 0.0, 'side': 0, 'stop': None,
            'stop_ref': None, 'carry_stop': stop, 'carry_units': units,
            'carry_side': side, 'carry_day': day}


class TestTheResumeDecision:
    """Long from 100, ATR stop at 95. The two divergences the old code produced are
    the first two tests; each asserts the stop live uses is the MODEL's, not one
    re-anchored to the reopen price."""

    def test_after_a_favourable_move_it_keeps_the_ORIGINAL_stop(self):
        # Price rose to 110. Re-anchoring would stop at ~105, so a fall to 104 would
        # stop the sleeve out on a day the backtest never exits.
        v, stop, units = fr.roll_flat_resume(_carried(), 1, 110.0)
        assert (v, stop, units) == ('resume', 95.0, 10.0)

    def test_after_an_adverse_move_it_keeps_the_ORIGINAL_stop(self):
        # Price fell to 96. Re-anchoring would stop at ~91, so the backtest exits at
        # 95 while live keeps bleeding.
        v, stop, units = fr.roll_flat_resume(_carried(), 1, 96.0)
        assert (v, stop, units) == ('resume', 95.0, 10.0)

    def test_a_stop_passed_during_the_gap_does_NOT_reopen(self):
        v, stop, units = fr.roll_flat_resume(_carried(), 1, 94.0)
        assert v == 'stopped' and stop == 95.0 and units is None

    def test_touching_the_stop_exactly_counts_as_stopped(self):
        assert fr.roll_flat_resume(_carried(), 1, 95.0)[0] == 'stopped'

    def test_the_short_side_is_mirrored(self):
        short = _carried(stop=105.0, side=-1)
        assert fr.roll_flat_resume(short, -1, 96.0)[:2] == ('resume', 105.0)
        assert fr.roll_flat_resume(short, -1, 106.0)[0] == 'stopped'

    def test_a_genuine_flip_starts_a_FRESH_trade(self):
        """long -> short is a new trade. Inheriting the long's stop would put it on
        the wrong side of the market and the broker would reject it."""
        assert fr.roll_flat_resume(_carried(side=1), -1, 96.0) == ('fresh', None, None)

    def test_a_stale_carry_is_ignored(self):
        old = (fr.datetime.now(timezone.utc) - timedelta(days=9)).strftime('%Y-%m-%d')
        assert fr.roll_flat_resume(_carried(day=old), 1, 110.0) == ('fresh', None, None)

    def test_an_ordinary_entry_with_no_carry_is_fresh(self):
        assert fr.roll_flat_resume(fr.FLAT(0), 1, 110.0) == ('fresh', None, None)


class TestEndToEnd:
    def test_close_then_resume_round_trips_the_trade(self, armed):
        state = _held(stop=95.0, units=10.0)
        fr.roll_flat_close(state, {'fix': {'NAS100_USD': _Ad()}}, True, now=FRIDAY)
        # ...next pass, price has moved up but the trade is the same trade.
        nxt = FRIDAY + timedelta(days=3)            # the Monday pass
        assert fr.roll_flat_resume(state['nas_x'], 1, 110.0, now=nxt) == ('resume', 95.0, 10.0)

    def test_close_then_stop_passed_overnight(self, armed):
        state = _held(stop=95.0, units=10.0)
        fr.roll_flat_close(state, {'fix': {'NAS100_USD': _Ad()}}, True, now=FRIDAY)
        nxt = FRIDAY + timedelta(days=1)            # the next broker day
        assert fr.roll_flat_resume(state['nas_x'], 1, 93.0, now=nxt)[0] == 'stopped'


class TestTheFridayGap:
    """Roll-flat fires on Friday night too, so its reopen is MONDAY — a three-day
    gap. A one-day freshness window silently refused the carry every Monday and
    re-anchored the stop weekly, which is the defect the carry exists to remove."""

    def test_a_friday_close_still_resumes_on_monday(self, armed):
        state = _held(stop=95.0, units=10.0)
        fr.roll_flat_close(state, {'fix': {'NAS100_USD': _Ad()}}, True, now=FRIDAY)
        monday = FRIDAY + timedelta(days=3)
        assert fr.roll_flat_resume(state['nas_x'], 1, 110.0, now=monday) \
            == ('resume', 95.0, 10.0)

    def test_a_holiday_monday_four_days_out_still_resumes(self, armed):
        state = _held(stop=95.0, units=10.0)
        fr.roll_flat_close(state, {'fix': {'NAS100_USD': _Ad()}}, True, now=FRIDAY)
        assert fr.roll_flat_resume(state['nas_x'], 1, 110.0,
                                   now=FRIDAY + timedelta(days=4))[0] == 'resume'

    def test_but_a_week_later_is_still_refused(self, armed):
        state = _held(stop=95.0, units=10.0)
        fr.roll_flat_close(state, {'fix': {'NAS100_USD': _Ad()}}, True, now=FRIDAY)
        assert fr.roll_flat_resume(state['nas_x'], 1, 110.0,
                                   now=FRIDAY + timedelta(days=7))[0] == 'fresh'
