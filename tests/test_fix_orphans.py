"""A sleeve leaving the book must never strand its cTrader position.

fix_runner.load_sleeves() returns only status='paper_trading' and run_once()
iterates that list, so a retired sleeve's reconcile / software stop /
close-on-signal all stop running while the broker still holds its position.
The compact deployment DB is built from paper_trading only, so deploying a
retirement is the normal trigger for exactly that.
"""
import sys
from pathlib import Path
from unittest.mock import MagicMock

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))
import fix_runner as F


LIVE = {'sid': 'eurusd_auto_1_i9', 'inst': 'EUR_USD'}
HELD = {'signal': -1, 'pos_id': 'P7', 'units': 1000.0, 'side': -1,
        'stop': 1.11, 'stop_ref': {'clid': 'c1', 'order_id': 'o1'}}


def test_departed_sleeve_is_detected():
    state = {'eurusd_auto_1_i9': dict(HELD), 'hk33hkd_auto_2_i27': dict(HELD)}
    orphans = F.find_orphans([LIVE], state)
    assert [sid for sid, _ in orphans] == ['hk33hkd_auto_2_i27']


def test_flat_departed_sleeve_is_not_an_orphan():
    state = {'hk33hkd_auto_2_i27': {'signal': 0, 'pos_id': None, 'units': 0.0, 'side': 0}}
    assert F.find_orphans([LIVE], state) == []


def test_sweep_closes_and_cancels_the_stop(monkeypatch):
    monkeypatch.setenv('FIX_CLOSE_ORPHANS', '1')
    ad = MagicMock()
    ad.close_position.return_value = {'ok': True}
    state = {'hk33hkd_auto_2_i27': dict(HELD)}
    F.sweep_orphans([LIVE], state, live=True, adapters={'fix': {'HK33_HKD': ad}})
    ad.cancel_stop.assert_called_once()
    ad.close_position.assert_called_once_with('P7', 1000.0, -1)
    assert 'hk33hkd_auto_2_i27' not in state          # cleared only after a real ack


def test_unconfirmed_stop_cancel_blocks_the_close(monkeypatch):
    """Never close a position while its stop may still be working.

    The stop is a standalone opposite-side order, not an attached SL, so one left
    live behind a closed position is a naked entry that opens an unmanaged position
    when it triggers. The sweep used to call cancel_stop and discard the result,
    which is how AU200 7832089 and NATGAS 7832091 were stranded on 2026-07-27 —
    cTrader was rejecting every OrderCancelRequest (Symbol(55) is not allowed on
    35=F) so the cancel silently returned None. The other two close paths already
    guarded this; the sweep did not. Note MagicMock's default return is truthy,
    which is exactly why the original test passed while the bug was live.
    """
    monkeypatch.setenv('FIX_CLOSE_ORPHANS', '1')
    ad = MagicMock()
    ad.cancel_stop.return_value = None                # cTrader never confirmed
    state = {'hk33hkd_auto_2_i27': dict(HELD)}
    F.sweep_orphans([LIVE], state, live=True, adapters={'fix': {'HK33_HKD': ad}})
    ad.close_position.assert_not_called()
    assert state['hk33hkd_auto_2_i27']['pos_id'] == 'P7'   # retried next pass


def test_failed_close_keeps_the_state_entry(monkeypatch):
    """The state entry is the only record the exposure exists — never drop it on failure."""
    monkeypatch.setenv('FIX_CLOSE_ORPHANS', '1')
    ad = MagicMock()
    ad.close_position.side_effect = RuntimeError('MARKET_HALTED')
    state = {'hk33hkd_auto_2_i27': dict(HELD)}
    F.sweep_orphans([LIVE], state, live=True, adapters={'fix': {'HK33_HKD': ad}})
    assert state['hk33hkd_auto_2_i27']['pos_id'] == 'P7'


def test_no_ack_is_treated_as_failure(monkeypatch):
    monkeypatch.setenv('FIX_CLOSE_ORPHANS', '1')
    ad = MagicMock()
    ad.close_position.return_value = None
    state = {'hk33hkd_auto_2_i27': dict(HELD)}
    F.sweep_orphans([LIVE], state, live=True, adapters={'fix': {'HK33_HKD': ad}})
    assert 'hk33hkd_auto_2_i27' in state


def test_rejected_close_is_not_treated_as_closed(monkeypatch):
    """A reject comes back as a truthy ack, not None.

    fix._order returns whatever landed in _acks, and the 35=3/j handler stores
    {'ord_status': '8', 'reject': ...} — so `ack is None` alone reads a rejected
    close as success. On 2026-07-27 that reported AU200 4313903 "closed" while it
    stayed open at the broker, and dropped the state entry that was the only record
    of the exposure. Status must be validated the way both signal-close paths do.
    """
    monkeypatch.setenv('FIX_CLOSE_ORPHANS', '1')
    ad = MagicMock()
    ad.close_position.return_value = {'ord_status': '8', 'reject': 'MARKET_CLOSED'}
    state = {'hk33hkd_auto_2_i27': dict(HELD)}
    F.sweep_orphans([LIVE], state, live=True, adapters={'fix': {'HK33_HKD': ad}})
    assert state['hk33hkd_auto_2_i27']['pos_id'] == 'P7'   # kept, retried next pass


def test_dry_run_never_places_an_order():
    ad = MagicMock()
    state = {'hk33hkd_auto_2_i27': dict(HELD)}
    F.sweep_orphans([LIVE], state, live=False, adapters={'fix': {'HK33_HKD': ad}})
    ad.close_position.assert_not_called()
    assert 'hk33hkd_auto_2_i27' in state


def test_kill_switch_reports_without_closing(monkeypatch):
    monkeypatch.setenv('FIX_CLOSE_ORPHANS', '0')
    ad = MagicMock()
    state = {'hk33hkd_auto_2_i27': dict(HELD)}
    F.sweep_orphans([LIVE], state, live=True, adapters={'fix': {'HK33_HKD': ad}})
    ad.close_position.assert_not_called()
    assert 'hk33hkd_auto_2_i27' in state


def test_preflight_fails_when_the_deploy_would_strand_exposure(capsys):
    state = {'hk33hkd_auto_2_i27': dict(HELD)}
    rc = F.print_preflight([], equity=2500.0, state=state)
    assert rc == 1
    assert 'ORPHANED' in capsys.readouterr().out


def test_preflight_passes_on_a_clean_book():
    state = {'eurusd_auto_1_i9': dict(HELD)}
    assert F.print_preflight([LIVE], equity=2500.0, state=state) == 0
