"""Tests for live_test.order_decision — the flip/startup-alignment matrix.

The 2026-06-10 incubation finding: a sleeve deployed while its strategy was
mid-position never entered (the loop only traded on signal FLIPS), so live
sat flat for 12 days while validation said short +4.3%. order_decision adds
a one-time startup alignment WITHOUT breaking the mid-run stop-out semantics
(stop fires -> stay flat until the signal changes, as validation models).
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
from live_test import order_decision


class TestOrderDecision:
    def test_normal_flip_fires(self):
        assert order_decision(+1, 0, 0, False, False) == 'flip'
        assert order_decision(0, -1, -1, False, False) == 'flip'
        assert order_decision(-1, +1, +1, False, True) == 'flip'  # flip wins over align

    def test_startup_mid_position_aligns(self):
        """Deploy while strategy already short: signal -1 == prev -1, flat broker."""
        assert order_decision(-1, -1, 0, False, True) == 'align'
        assert order_decision(+1, +1, 0, False, True) == 'align'

    def test_restart_with_open_position_does_nothing(self):
        """Respawn with the broker position already matching: no churn."""
        assert order_decision(+1, +1, +1, False, True) is None
        assert order_decision(-1, -1, -1, False, True) is None

    def test_mid_run_stop_out_stays_flat(self):
        """Stop fired between bars (position 0, signal persists): the
        validated stream models flat-until-signal-change — must NOT re-enter."""
        assert order_decision(-1, -1, 0, False, False) is None
        assert order_decision(+1, +1, 0, False, False) is None

    def test_halted_never_aligns(self):
        assert order_decision(-1, -1, 0, True, True) is None

    def test_flat_signal_at_startup_never_orders(self):
        assert order_decision(0, 0, 0, False, True) is None
