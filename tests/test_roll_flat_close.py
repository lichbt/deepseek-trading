"""The pre-roll close pass (roll-flat ticket 03).

The close itself is the easy half — it reuses `flatten_all`, which already
cancels the stop first, aborts the sleeve on an unconfirmed cancel, and writes
FLAT(0). The half that needs pinning is WHEN it fires.

The broker's day rolls at 00:00 on its own clock, which is 21:00 UTC in US summer
and 22:00 UTC in US winter (server = America/New_York + 7h). A window expressed
as a UTC constant is therefore correct for one regime and silently pays the
entire carry in the other — for about four and a half months a year, with nothing
to report it. So the window is defined backwards from the broker's midnight, and
these tests run it in both regimes.
"""
import json
from datetime import datetime, timezone

import pytest

import fix_runner as fr
import prop_guard


def utc(s):
    return datetime.fromisoformat(s).replace(tzinfo=timezone.utc)


# 2026-08-11 is US DST (broker UTC+3, roll 21:00 UTC).
# 2026-12-15 is US standard (broker UTC+2, roll 22:00 UTC).
SUMMER_ROLL = '2026-08-11T21:00:00'
WINTER_ROLL = '2026-12-15T22:00:00'


class TestTheWindowFollowsTheBrokerClock:
    def test_summer_fires_in_the_ten_minutes_before_2100_utc(self):
        assert fr.roll_flat_due(utc('2026-08-11T20:52:00'), None)[0] is True
        assert fr.roll_flat_due(utc('2026-08-11T20:59:00'), None)[0] is True

    def test_summer_does_not_fire_an_hour_early(self):
        assert fr.roll_flat_due(utc('2026-08-11T19:52:00'), None)[0] is False

    def test_winter_fires_an_hour_later_in_utc(self):
        """The whole point. A fixed 20:50 UTC close would run 70 minutes before
        the winter roll, the book would be reopened by then, and the carry would
        be paid in full."""
        assert fr.roll_flat_due(utc('2026-12-15T21:52:00'), None)[0] is True
        assert fr.roll_flat_due(utc('2026-12-15T20:52:00'), None)[0] is False

    def test_it_never_fires_after_the_roll(self):
        """Past the roll the carry is already charged; closing then pays the
        round trip on top of it."""
        for t in (SUMMER_ROLL, '2026-08-11T21:05:00'):
            assert fr.roll_flat_due(utc(t), None)[0] is False

    def test_the_day_label_is_the_broker_day_not_the_utc_date(self):
        """In summer the window sits just before 21:00 UTC, where the UTC date
        and the broker date agree — but one minute later they do not. The latch
        has to key on the same label the window is measured in."""
        due, day = fr.roll_flat_due(utc('2026-08-11T20:52:00'), None)
        assert due and day == '2026-08-11'
        assert prop_guard._trading_day(utc('2026-08-11T21:05:00')) == '2026-08-12'

    @pytest.mark.parametrize('roll', [SUMMER_ROLL, WINTER_ROLL])
    def test_the_lead_is_measured_from_the_roll_in_both_regimes(self, roll):
        r = utc(roll)
        just_inside = r.timestamp() - 60 * (fr.ROLL_FLAT_LEAD - 1)
        just_outside = r.timestamp() - 60 * (fr.ROLL_FLAT_LEAD + 1)
        assert fr.roll_flat_due(
            datetime.fromtimestamp(just_inside, timezone.utc), None)[0] is True
        assert fr.roll_flat_due(
            datetime.fromtimestamp(just_outside, timezone.utc), None)[0] is False


class TestTheLatch:
    def test_a_closed_day_does_not_fire_again(self):
        assert fr.roll_flat_due(utc('2026-08-11T20:52:00'), '2026-08-11')[0] is False

    def test_yesterdays_latch_does_not_block_today(self):
        assert fr.roll_flat_due(utc('2026-08-11T20:52:00'), '2026-08-10')[0] is True


class TestTheClosePass:
    def _adapters(self, ok=True):
        class _Ad:
            def cancel_stop(self, ref, side):
                return {'ok': True}

            def close_position(self, pos_id, units, side):
                return {'ord_status': '2'} if ok else {'ord_status': '8'}
        return {'fix': {i: _Ad() for i in ('NAS100_USD', 'XAG_USD')},
                'price': {}, 'equity': lambda: 100_000.0}

    def _book(self):
        return {
            'nas100_x': {'signal': 1, 'pos_id': 'P1', 'units': 1.0, 'side': 1,
                         'stop': 1.0, 'stop_ref': 'S1', 'inst': 'NAS100_USD'},
            'xag_y': {'signal': -1, 'pos_id': 'P2', 'units': 5.0, 'side': -1,
                      'stop': 9.0, 'stop_ref': 'S2', 'inst': 'XAG_USD'},
        }

    @pytest.fixture(autouse=True)
    def _armed(self, tmp_path, monkeypatch):
        monkeypatch.setattr(fr, 'ROLL_FLAT', True)
        monkeypatch.setattr(fr, 'STATE_FILE', str(tmp_path / 'state.json'))
        monkeypatch.setattr(fr, 'ROLL_FLAT_FILE', str(tmp_path / 'latch.json'))

    def test_it_closes_the_covered_instrument_and_leaves_the_rest(self):
        book = self._book()
        closed, failed = fr.roll_flat_close(book, self._adapters(), True,
                                            now=utc('2026-08-11T20:52:00'))
        assert closed == ['nas100_x'] and failed == []
        assert book['nas100_x']['signal'] == 0        # FLAT(0) -> reopens
        assert book['xag_y']['pos_id'] == 'P2'        # untouched, still carrying

    def test_it_is_a_noop_outside_the_window(self):
        book = self._book()
        assert fr.roll_flat_close(book, self._adapters(), True,
                                  now=utc('2026-08-11T19:00:00')) is None
        assert book['nas100_x']['pos_id'] == 'P1'

    def test_it_is_inert_when_the_flag_is_off(self, monkeypatch):
        monkeypatch.setattr(fr, 'ROLL_FLAT', False)
        assert fr.roll_flat_close(self._book(), self._adapters(), True,
                                  now=utc('2026-08-11T20:52:00')) is None

    def test_a_successful_close_latches_the_day(self):
        fr.roll_flat_close(self._book(), self._adapters(), True,
                           now=utc('2026-08-11T20:52:00'))
        assert json.load(open(fr.ROLL_FLAT_FILE))['day'] == '2026-08-11'

    def test_a_rejected_close_keeps_the_position_and_does_not_latch(self):
        """THE REJECTION PATH. The broker refuses at 20:52; the position stays
        open, stays in state and stays stopped, and the day is NOT latched — so
        the next poll retries while the window lasts. The cost of the miss is one
        night of carry, which is strictly cheaper than a close after the roll."""
        book = self._book()
        closed, failed = fr.roll_flat_close(book, self._adapters(ok=False), True,
                                            now=utc('2026-08-11T20:52:00'))
        assert closed == [] and [s for s, _ in failed] == ['nas100_x']
        assert book['nas100_x']['pos_id'] == 'P1'
        assert book['nas100_x']['signal'] == 1        # NOT FLAT(0): still owned
        assert fr._read_roll_flat_latch() is None

    def test_the_retry_succeeds_on_the_next_poll(self):
        book = self._book()
        fr.roll_flat_close(book, self._adapters(ok=False), True,
                           now=utc('2026-08-11T20:52:00'))
        closed, failed = fr.roll_flat_close(book, self._adapters(), True,
                                            now=utc('2026-08-11T20:53:00'))
        assert closed == ['nas100_x'] and failed == []

    def test_a_dry_run_places_no_orders_but_shows_the_intent(self):
        """How this gets rehearsed against the real venue without moving the
        book: live=False takes the same path and reports the same sleeves."""
        book = self._book()
        closed, failed = fr.roll_flat_close(book, self._adapters(), False,
                                            now=utc('2026-08-11T20:52:00'))
        assert closed == ['nas100_x'] and failed == []
        assert fr._read_roll_flat_latch() is None     # dry runs never latch


class TestItDoesNotFightTheGuard:
    def test_a_halt_that_already_flattened_leaves_nothing_to_close(self, tmp_path,
                                                                   monkeypatch):
        """Guard halts at 19:00 and writes FLAT(0) everywhere; the 20:52 pass
        finds no pos_id and closes nothing. The two writers agree because they
        write the same thing."""
        monkeypatch.setattr(fr, 'ROLL_FLAT', True)
        monkeypatch.setattr(fr, 'STATE_FILE', str(tmp_path / 'state.json'))
        monkeypatch.setattr(fr, 'ROLL_FLAT_FILE', str(tmp_path / 'latch.json'))
        book = {'nas100_x': fr.FLAT(0)}
        closed, failed = fr.roll_flat_close(book, {'fix': {}, 'price': {}}, True,
                                            now=utc('2026-08-11T20:52:00'))
        assert closed == [] and failed == []
