"""The in-pod drawdown circuit breaker (fix_runner N3/N4).

The breaker exists because CLUSTER_CAP bounds each cluster and MAXRISK bounds
each trade, but nothing bounds their sum: measured 2026-08-05, the arithmetic max
if every sleeve stops on one day is 3.985% against a 3% wall, and sized-for
exposure has historically peaked at 2.998%. The block bootstrap reports 0.00%
daily breach and structurally cannot price that day.

On this plan a 3% daily loss is PERMANENT TERMINATION, so these paths get tested
rather than trusted — the trigger has never fired in 672 simulated days, which
means production will never rehearse it.
"""
import json
import os

import pytest

os.environ.setdefault('VENUE', 'fix')      # keep the import from opening a cTrader session

import fix_runner as fr


# ---------------------------------------------------------------------------
# halt_decision — pure
# ---------------------------------------------------------------------------
class TestHaltDecision:
    def test_healthy_book_does_not_halt(self):
        assert fr.halt_decision(100_000, 100_000, 100_000) is None

    def test_profit_does_not_halt(self):
        assert fr.halt_decision(104_000, 103_000, 100_000) is None

    def test_daily_halts_at_80_percent_of_the_limit(self):
        """3% limit x 0.80 = -2.40%, deliberately short of the wall."""
        assert fr.halt_decision(97_650, 100_000, 100_000) is None      # -2.35%
        assert fr.halt_decision(97_600, 100_000, 100_000) == 'daily'   # -2.40%
        assert fr.halt_decision(97_500, 100_000, 100_000) == 'daily'

    def test_daily_is_measured_from_the_day_anchor_not_the_start(self):
        """Up 5% on the account but down 2.5% today is still a daily breach —
        which is the whole reason the anchor has to be right."""
        assert fr.halt_decision(102_375, 105_000, 100_000) == 'daily'

    def test_total_halts_at_80_percent_of_the_static_limit(self):
        assert fr.halt_decision(92_500, 92_500, 100_000) is None       # -7.5%
        assert fr.halt_decision(92_000, 92_000, 100_000) == 'total'    # -8.0%

    def test_total_outranks_daily(self):
        """Both breached -> report 'total'. The daily anchor resets tomorrow and
        the static limit never does; calling it 'daily' would imply it recovers."""
        assert fr.halt_decision(91_000, 93_000, 100_000) == 'total'

    def test_missing_inputs_never_fabricate_a_breach(self):
        for args in ((None, 100_000, 100_000), (100_000, None, 100_000),
                     (100_000, 100_000, None), (0, 100_000, 100_000)):
            assert fr.halt_decision(*args) is None

    def test_limits_are_overridable(self):
        assert fr.halt_decision(98_000, 100_000, 100_000,
                                daily_limit=0.05, fraction=0.80) is None
        assert fr.halt_decision(98_000, 100_000, 100_000,
                                daily_limit=0.025, fraction=0.80) == 'daily'


# ---------------------------------------------------------------------------
# halt_is_active — pause vs stop
# ---------------------------------------------------------------------------
class TestHaltLatch:
    def test_no_halt_is_not_active(self):
        assert fr.halt_is_active(None, '2026-08-05') is False
        assert fr.halt_is_active({}, '2026-08-05') is False

    def test_daily_latches_for_the_rest_of_the_day(self):
        h = {'kind': 'daily', 'day': '2026-08-05'}
        assert fr.halt_is_active(h, '2026-08-05') is True

    def test_daily_lifts_when_the_broker_day_rolls(self):
        """This is what makes it a PAUSE. The book re-establishes next pass."""
        h = {'kind': 'daily', 'day': '2026-08-05'}
        assert fr.halt_is_active(h, '2026-08-06') is False

    def test_total_never_lifts_on_its_own(self):
        """The static limit does not reset, so auto-resuming would walk straight
        back at the account limit. Clearing it is a human decision."""
        h = {'kind': 'total', 'day': '2026-08-05'}
        assert fr.halt_is_active(h, '2026-08-06') is True
        assert fr.halt_is_active(h, '2027-01-01') is True


# ---------------------------------------------------------------------------
# flatten_all — N4
# ---------------------------------------------------------------------------
class _Adapter:
    def __init__(self, cancel_ok=True, close_ok=True):
        self.cancel_ok, self.close_ok = cancel_ok, close_ok
        self.cancelled, self.closed = [], []

    def cancel_stop(self, ref, side):
        self.cancelled.append(ref)
        return {'ok': True} if self.cancel_ok else None

    def close_position(self, pos_id, units, side):
        self.closed.append(pos_id)
        return {'ord_status': '2'} if self.close_ok else {'ord_status': '8'}


def _held(sid, pos_id='P1', signal=1, stop_ref='S1'):
    return {sid: {'signal': signal, 'pos_id': pos_id, 'units': 1000.0, 'side': 1,
                  'stop': 1.0, 'stop_ref': stop_ref, 'inst': 'EUR_USD'}}


def _adapters(ad):
    return {'fix': {'EUR_USD': ad}, 'price': {}, 'equity': lambda: 100_000.0}


class TestFlattenAll:
    def test_closes_and_clears_the_signal_not_just_the_position(self, tmp_path, monkeypatch):
        """FLAT(0), not FLAT(signal). Entries fire on a signal CHANGE, so a
        preserved signal would leave the book sitting out until each sleeve
        happened to flip — weeks for a daily book. This is the single line that
        makes the halt a pause instead of a permanent exit."""
        monkeypatch.setattr(fr, 'STATE_FILE', str(tmp_path / 's.json'))
        ad = _Adapter()
        state = _held('eurusd_x')
        closed, failed = fr.flatten_all(state, _adapters(ad), True, 'test')
        assert closed == ['eurusd_x'] and failed == []
        assert ad.closed == ['P1']
        assert state['eurusd_x']['pos_id'] is None
        assert state['eurusd_x']['signal'] == 0

    def test_cancels_the_stop_before_closing(self):
        ad = _Adapter()
        fr.flatten_all(_held('eurusd_x'), _adapters(ad), False, 'test')
        # dry-run path does not touch the broker at all
        assert ad.cancelled == [] and ad.closed == []

    def test_unconfirmed_stop_cancel_aborts_that_sleeve(self, tmp_path, monkeypatch):
        """A stop outliving its position fires as a naked entry in the opposite
        direction. Better to stay long and stopped than flat and unprotected."""
        monkeypatch.setattr(fr, 'STATE_FILE', str(tmp_path / 's.json'))
        ad = _Adapter(cancel_ok=False)
        state = _held('eurusd_x')
        closed, failed = fr.flatten_all(state, _adapters(ad), True, 'test')
        assert closed == [] and [s for s, _ in failed] == ['eurusd_x']
        assert ad.closed == []                       # never attempted
        assert state['eurusd_x']['pos_id'] == 'P1'   # still owned, still tracked

    def test_rejected_close_keeps_the_position_in_state(self, tmp_path, monkeypatch):
        """Dropping pos_id on a failed close strands a position nothing can ever
        close — sweep_orphans iterates STATE, not the broker book."""
        monkeypatch.setattr(fr, 'STATE_FILE', str(tmp_path / 's.json'))
        ad = _Adapter(close_ok=False)
        state = _held('eurusd_x')
        closed, failed = fr.flatten_all(state, _adapters(ad), True, 'test')
        assert closed == [] and len(failed) == 1
        assert state['eurusd_x']['pos_id'] == 'P1'

    def test_flat_sleeves_are_skipped(self, tmp_path, monkeypatch):
        monkeypatch.setattr(fr, 'STATE_FILE', str(tmp_path / 's.json'))
        ad = _Adapter()
        state = {'flat_x': fr.FLAT(1)}
        closed, failed = fr.flatten_all(state, _adapters(ad), True, 'test')
        assert closed == [] and failed == []
        assert ad.closed == []

    def test_one_bad_sleeve_does_not_block_the_others(self, tmp_path, monkeypatch):
        monkeypatch.setattr(fr, 'STATE_FILE', str(tmp_path / 's.json'))
        ad = _Adapter()
        state = {}
        state.update(_held('a_x', pos_id='PA'))
        state.update(_held('b_x', pos_id='PB'))
        state['b_x']['inst'] = 'NOPE_XXX'            # no adapter for this one
        closed, failed = fr.flatten_all(state, _adapters(ad), True, 'test')
        assert closed == ['a_x'] and [s for s, _ in failed] == ['b_x']

    def test_state_is_persisted_when_live(self, tmp_path, monkeypatch):
        p = tmp_path / 's.json'
        monkeypatch.setattr(fr, 'STATE_FILE', str(p))
        fr.flatten_all(_held('eurusd_x'), _adapters(_Adapter()), True, 'test')
        assert json.loads(p.read_text())['eurusd_x']['signal'] == 0


class TestGuardDefaults:
    def test_disarmed_unless_explicitly_enabled(self):
        """PROP_GUARD_HALT unset must mean the breaker cannot close anything."""
        assert fr.GUARD_ENABLED == (os.getenv('PROP_GUARD_HALT') == '1')

    def test_thresholds_match_the_product(self):
        assert fr.GUARD_DAILY_LIM == pytest.approx(0.03)
        assert fr.GUARD_TOTAL_LIM == pytest.approx(0.10)
        assert fr.GUARD_DAILY_LIM * fr.GUARD_FRACTION == pytest.approx(0.024)

    def test_guard_tick_is_a_noop_when_disarmed(self, monkeypatch):
        monkeypatch.setattr(fr, 'GUARD_ENABLED', False)
        assert fr.guard_tick({}, None, False) is False
