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
    ok, dsr = V._dsr_gate(None, "eurusd_auto_1", "D")
    assert ok is True and dsr is None


def test_dsr_pool_is_per_instrument_and_timeframe(tmp_path, monkeypatch):
    db = str(tmp_path / "trials.db")
    monkeypatch.setattr(V, "TRIALS_DB", db)
    for i in range(50):
        H.record_trial(db, f"eurusd_auto_{i}", 0.05, 0, 0, None, meta={"tf": "D"})
    H.record_trial(db, "eurusd_auto_h1", 0.05, 0, 0, None, meta={"tf": "H1"})  # same inst, diff tf
    H.record_trial(db, "btcusd_auto_1", 0.20, 1, 1, None, meta={"tf": "D"})    # diff inst
    assert len(V._matching_trial_sharpes("eurusd_auto_x", "D")) == 50   # not 52
    assert len(V._matching_trial_sharpes("eurusd_auto_x", "H1")) == 1
    assert len(V._matching_trial_sharpes("btcusd_auto_x", "D")) == 1
