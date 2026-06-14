"""Tests for the hard drawdown gate in validator.py.

A strategy can post a strong (risk-adjusted) walk-forward score while still
carrying a crater-deep peak-to-trough that would blow a prop account's static
limit (BTC 51%, Brent 36.5%, palladium 38% all cleared every other gate). The
gate reconstructs the CONTINUOUS full-history equity and rejects DD > 30%.
"""
import os
import sys

import numpy as np
import pandas as pd
import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
import validator as v


def _frame(closes, start="2020-01-01"):
    idx = pd.date_range(start, periods=len(closes), freq="D")
    c = pd.Series(np.asarray(closes, dtype=float))
    return pd.DataFrame({"date": idx, "open": c, "high": c, "low": c, "close": c})


def _always_long(df, params):
    return pd.Series(1, index=df.index)


class TestReconstructedMaxDrawdown:
    def test_deep_crash_detected(self):
        closes = (list(np.linspace(100, 130, 20))
                  + list(np.linspace(130, 78, 20))    # -40% from peak
                  + [78] * 20)
        dd = v.reconstructed_max_drawdown(_always_long, {}, _frame(closes), None, "EUR_USD", "D")
        assert dd > 0.35, f"expected ~40% DD, got {dd:.1%}"

    def test_gentle_uptrend_low_dd(self):
        dd = v.reconstructed_max_drawdown(_always_long, {}, _frame(np.linspace(100, 110, 60)),
                                          None, "EUR_USD", "D")
        assert dd < 0.05

    def test_boundary_crossing_drawdown_is_captured(self):
        # Peak (150) lives in full_data; trough (100) lives in the holdout. The
        # continuous splice must measure the full 150->100 (~33%) drawdown, NOT
        # the holdout-only 130->100 (~23%) that a peak-reset would report.
        full = _frame(list(np.linspace(100, 150, 30)) + list(np.linspace(150, 130, 10)))
        hold = _frame(np.linspace(130, 100, 20), start="2020-02-10")
        cont = v.reconstructed_max_drawdown(_always_long, {}, full, hold, "EUR_USD", "D")
        holdout_only = v.reconstructed_max_drawdown(_always_long, {}, hold, None, "EUR_USD", "D")
        assert cont > 0.30, f"continuous DD should capture cross-boundary crash, got {cont:.1%}"
        assert holdout_only < 0.30, f"holdout-only would miss it ({holdout_only:.1%}) — that's the point"
        assert cont > holdout_only

    def test_holdout_none_uses_full(self):
        closes = list(np.linspace(100, 120, 20)) + list(np.linspace(120, 84, 20))  # -30%
        dd = v.reconstructed_max_drawdown(_always_long, {}, _frame(closes), None, "EUR_USD", "D")
        assert dd > 0.25

    def test_reconstruction_error_fails_open(self):
        def boom(df, params):
            raise RuntimeError("bad strategy")
        dd = v.reconstructed_max_drawdown(boom, {}, _frame(np.linspace(100, 80, 60)),
                                          None, "EUR_USD", "D")
        assert dd == 0.0   # fail open — never reject on a reconstruction glitch

    def test_insufficient_data_returns_zero(self):
        dd = v.reconstructed_max_drawdown(_always_long, {}, _frame([100, 90, 80, 70]),
                                          None, "EUR_USD", "D")
        assert dd == 0.0


class TestDrawdownGateWiring:
    """Drive validate_on_timeframe with the upstream stages mocked to PASS, so
    only the DD gate decides."""

    def _patch_pass_upstream(self, monkeypatch):
        monkeypatch.setattr(v, "grid_search", lambda *a, **k: ({"stop_mult": 3.0}, 0.8))
        monkeypatch.setattr(v, "walk_forward", lambda *a, **k: {
            "combined_gt_score": 0.70, "min_window_score": 0.10,
            "num_valid_windows": 5, "total_windows": 5,
            "has_sufficient_windows": True,
            "windows_with_edge": 4, "per_window_gt_scores": [0.6] * 5,
            "per_window_trade_counts": [10] * 5,
        })

    def test_high_dd_is_rejected(self, monkeypatch):
        self._patch_pass_upstream(monkeypatch)
        monkeypatch.setattr(v, "reconstructed_max_drawdown", lambda *a, **k: 0.50)
        df = _frame(np.linspace(100, 110, 60))
        res = v.validate_on_timeframe(df, df, None, _always_long, {"n": [1]},
                                      "EUR_USD", "D", "sid_dd")
        assert res["passed"] is False
        assert "Max drawdown" in res["reason"] and "50.0%" in res["reason"]

    def test_acceptable_dd_passes_the_gate(self, monkeypatch):
        self._patch_pass_upstream(monkeypatch)
        monkeypatch.setattr(v, "reconstructed_max_drawdown", lambda *a, **k: 0.10)
        df = _frame(np.linspace(100, 110, 60))
        res = v.validate_on_timeframe(df, df, None, _always_long, {"n": [1]},
                                      "EUR_USD", "D", "sid_ok")
        # holdout is None -> no holdout gate -> passes through to PASS
        assert res["passed"] is True

    def test_threshold_is_thirty_percent(self):
        assert v.MAX_DRAWDOWN_HARD == 0.30
