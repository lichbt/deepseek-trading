"""Tests for the honesty layer wired into validator.py.

Two things that must hold for the gate to be honest:
  - failure strings map to the right fixed tag (esp. 'too few ... trades' must
    NOT become ho_decay just because it contains 'holdout').
  - the deflated-Sharpe gate actually rejects the luckiest draw of a big search
    and passes a clear edge from a small one.
"""
import numpy as np

import strategy_honesty as H
import validator as V


def test_failure_tag_mapping():
    assert V._failure_tag("PASS (D)") is None
    assert V._failure_tag("FAIL: Max drawdown 65% > 30%") == "dd_breach"
    assert V._failure_tag("FAIL: ho_decay — deflated Sharpe 0.40 < 0.95") == "ho_decay"
    assert V._failure_tag("FAIL: too few holdout trades (5 < 10)") == "low_sample"   # not ho_decay
    assert V._failure_tag("FAIL: directional_bias — trend-riding") == "regime_fragile"
    assert V._failure_tag("FAIL: only 2/5 windows") == "insufficient_folds"


def test_dsr_gate_rejects_luckiest_of_big_search():
    rng = np.random.default_rng(0)
    junk = list(rng.normal(0, 0.06, 200))           # 200 zero-edge trials
    weak = rng.normal(0.0005, 0.011, 750)           # tiny edge ~ the max draw of the search
    dsr = H.deflated_sharpe_ratio(weak, junk)
    assert dsr < V.DSR_MIN                            # gate would reject -> ho_decay


def test_dsr_gate_passes_clear_edge_small_search():
    rng = np.random.default_rng(1)
    strong = rng.normal(0.0025, 0.011, 1000)        # daily Sharpe ~0.23, a real edge
    dsr = H.deflated_sharpe_ratio(strong, [0.05, 0.06])  # tiny trial pool
    assert dsr > V.DSR_MIN


def test_gate_fails_open_on_none():
    # no reconstructed returns -> never blocks validation
    ok, dsr = V._dsr_gate(None)
    assert ok is True and dsr is None
