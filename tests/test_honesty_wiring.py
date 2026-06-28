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


def _seed_junk_pool(db, monkeypatch):
    monkeypatch.setattr(V, "TRIALS_DB", db)
    rng = np.random.default_rng(0)
    for i in range(120):
        H.record_trial(db, f"eurusd_auto_{i}", float(rng.normal(0, 0.06)), 0, 0, None, meta={"tf": "D"})
    return rng.normal(0.0005, 0.011, 750)   # a weak edge ~ the search's luck bar


def test_observe_only_computes_dsr_but_never_rejects(tmp_path, monkeypatch):
    weak = _seed_junk_pool(str(tmp_path / "t.db"), monkeypatch)
    monkeypatch.setattr(V, "DSR_GATE_ENABLED", False)            # the default
    ok, dsr = V._dsr_gate(weak, "eurusd_auto_x", "D")
    assert ok is True                                            # observe-only: promotes anyway
    assert dsr is not None and dsr < V.DSR_MIN                   # but the number is computed & low


def test_gate_on_rejects_low_dsr(tmp_path, monkeypatch):
    weak = _seed_junk_pool(str(tmp_path / "t.db"), monkeypatch)
    monkeypatch.setattr(V, "DSR_GATE_ENABLED", True)
    ok, dsr = V._dsr_gate(weak, "eurusd_auto_x", "D")
    assert ok is False and dsr < V.DSR_MIN


def test_concentration_flags_regime_beta():
    # gains only in the first 2 of 5 year-chunks -> rally-concentrated -> high
    conc = np.concatenate([np.full(252, 0.004), np.full(252, 0.004),
                           np.full(252, -0.0005), np.full(252, -0.0005),
                           np.full(252, 0.0001)])
    c = V._concentration(conc)
    assert c is not None and c > 0.85
    # equal positive gains across 5 years -> deconcentrated (2/5 ~ 0.4)
    spread = V._concentration(np.full(252 * 5, 0.002))
    assert spread is not None and spread < 0.6
    # too short / None -> observe-only, never raises
    assert V._concentration(np.full(100, 0.01)) is None
    assert V._concentration(None) is None
