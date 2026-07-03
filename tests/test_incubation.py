"""Tests for incubation.py — live-vs-validation reconstruction tracking.

The guard this module provides: a deployed sleeve whose LIVE bar returns
don't match what its own strategy code reconstructs over the same window is
flagged, regardless of P&L. This is the generic detector for the class of
bug found in the 2026-06-09 look-ahead audit (validation evidence corrupted
in a way live trading can't reproduce).
"""
import os
import sys

import numpy as np
import pandas as pd
import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
import incubation


def _days(n, start="2026-05-01"):
    return pd.date_range(start, periods=n, freq="D")


ROW = {"id": "test_sleeve", "timeframe": "D", "code": "", "best_params": "{}"}


def _patch(monkeypatch, live, expected):
    monkeypatch.setattr(incubation, "parse_log_returns", lambda sid: live)
    monkeypatch.setattr(incubation, "build_strategy_returns", lambda r, s, e: expected)


class TestSleeveTracking:
    def test_perfect_match_is_tracking(self, monkeypatch):
        idx = _days(20)
        arr = np.where(np.arange(20) % 6 == 0, 0.004,
                       np.where(np.arange(20) % 3 == 0, 0.008, 0.0))
        rets = pd.Series(arr, index=idx)
        _patch(monkeypatch, rets.copy(), rets.copy())
        t = incubation.sleeve_tracking(ROW, "2026-05-01")
        assert t["status"] == "tracking"
        assert t["corr"] > 0.99
        assert abs(t["gap"]) < 1e-9

    def test_live_flat_while_expected_trades_is_flagged(self, monkeypatch):
        """The leak shape: validation says the sleeve should be earning,
        live does nothing of the sort."""
        idx = _days(20)
        expected = pd.Series(np.where(np.arange(20) % 2 == 0, 0.01, 0.0), index=idx)
        live = pd.Series(0.0, index=idx)
        _patch(monkeypatch, live, expected)
        t = incubation.sleeve_tracking(ROW, "2026-05-01")
        assert t["status"] == "mismatch"          # corr forced to 0 + gap -10%
        assert t["gap"] < -incubation.GAP_FAIL

    def test_uncorrelated_live_is_mismatch(self, monkeypatch):
        rng = np.random.RandomState(7)
        idx = _days(40)
        expected = pd.Series(rng.normal(0, 0.01, 40), index=idx)
        live = pd.Series(rng.normal(0, 0.01, 40), index=idx)  # independent draw
        _patch(monkeypatch, live, expected)
        t = incubation.sleeve_tracking(ROW, "2026-05-01")
        assert t["status"] in ("mismatch", "diverging")
        assert t["corr"] < incubation.WARN_CORR

    def test_insufficient_active_days_is_incubating(self, monkeypatch):
        idx = _days(10)
        rets = pd.Series(0.0, index=idx)
        rets.iloc[0] = 0.002   # one active day only
        _patch(monkeypatch, rets.copy(), rets.copy())
        t = incubation.sleeve_tracking(ROW, "2026-05-01")
        assert t["status"] == "incubating"
        assert t["n_active"] < incubation.MIN_OVERLAP_ACTIVE

    def test_no_live_log_is_incubating(self, monkeypatch):
        _patch(monkeypatch, None, pd.Series(0.01, index=_days(10)))
        t = incubation.sleeve_tracking(ROW, "2026-05-01")
        assert t["status"] == "incubating"
        assert "no live bar returns" in t["note"]

    def test_reconstruction_failure_never_raises(self, monkeypatch):
        monkeypatch.setattr(incubation, "parse_log_returns",
                            lambda sid: pd.Series(0.01, index=_days(10)))
        def boom(r, s, e):
            raise RuntimeError("data fetch failed")
        monkeypatch.setattr(incubation, "build_strategy_returns", boom)
        t = incubation.sleeve_tracking(ROW, "2026-05-01")
        assert t["status"] == "incubating"
        assert "tracking error" in t["note"]

    def test_deploy_date_slices_history(self, monkeypatch):
        """Returns from before the deploy date must not be judged."""
        idx = _days(30, start="2026-04-15")
        expected = pd.Series(0.005, index=idx)         # active every day
        live = expected.copy()
        live[idx < pd.Timestamp("2026-05-01")] = -0.05  # garbage pre-deploy
        _patch(monkeypatch, live, expected)
        t = incubation.sleeve_tracking(ROW, "2026-05-01")
        assert t["status"] == "tracking"               # pre-deploy garbage ignored


class TestReportSection:
    def test_report_renders_and_never_raises(self, monkeypatch):
        idx = _days(20)
        arr = np.where(np.arange(20) % 6 == 0, 0.004,
                       np.where(np.arange(20) % 3 == 0, 0.008, 0.0))
        rets = pd.Series(arr, index=idx)
        monkeypatch.setattr(incubation, "load_strategies",
                            lambda *a, **k: [dict(ROW)])
        monkeypatch.setattr(incubation, "_deploy_dates",
                            lambda: {"test_sleeve": "2026-05-01T00:00:00"})
        # report now lists only sleeves currently holding a live position
        monkeypatch.setattr(incubation, "_live_positions",
                            lambda: {"test_sleeve": 1})
        _patch(monkeypatch, rets.copy(), rets.copy())
        out = incubation.report_section()
        assert "Incubation" in out
        assert "test_sleeve" in out
        assert "✅" in out

    def test_report_skips_flat_sleeves(self, monkeypatch):
        idx = _days(20)
        rets = pd.Series(0.005, index=idx)
        monkeypatch.setattr(incubation, "load_strategies",
                            lambda *a, **k: [dict(ROW)])
        monkeypatch.setattr(incubation, "_deploy_dates",
                            lambda: {"test_sleeve": "2026-05-01T00:00:00"})
        monkeypatch.setattr(incubation, "_live_positions",
                            lambda: {"test_sleeve": 0})   # flat → excluded
        _patch(monkeypatch, rets.copy(), rets.copy())
        assert incubation.report_section() == ""

    def test_report_empty_book_is_empty(self, monkeypatch):
        monkeypatch.setattr(incubation, "load_strategies", lambda *a, **k: [])
        assert incubation.report_section() == ""
