#!/usr/bin/env python3
"""Netting recovery: own_units (broker share) is truth; live_status follows it.
Guards the bug where _place_order_netting persisted own_units but not
live_status, so a restart adopted a stale flat position while the broker held
a real one (EUR_JPY +14323 shown flat, 2026-06-29)."""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent))
from live_test import netting_position_from_units, netting_delta


def test_position_from_units():
    assert netting_position_from_units(14323.3) == 1
    assert netting_position_from_units(-14323.3) == -1
    assert netting_position_from_units(0.0) == 0
    assert netting_position_from_units(1e-12) == 0          # below tol -> flat
    # the exact restart case: live_status said 0, own_units says long -> long wins
    db_pos = 0
    assert netting_position_from_units(14323.3) != db_pos


def test_delta_round_trips():
    assert netting_delta(0, 14323.3) == 14323.3            # flat -> long
    assert netting_delta(14323.3, 14323.3) == 0            # already there -> no order
    assert netting_delta(14323.3, -14323.3) == -28646.6    # long -> short crosses net


if __name__ == '__main__':
    test_position_from_units()
    test_delta_round_trips()
    print("ok")
