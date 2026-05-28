"""
Tests for the live ATR stop-loss modeling in the backtest return calc
(pipeline_utils.compute_returns_with_stop / compute_net_strategy_returns).
"""
import sys
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))
import pipeline_utils as pu


def _df(closes):
    closes = np.array(closes, dtype=float)
    return pd.DataFrame({
        'date': pd.date_range('2020-01-01', periods=len(closes)),
        'open': closes,
        'high': closes + 1.0,
        'low': closes - 1.0,
        'close': closes,
    })


class TestComputeReturnsWithStop:
    def test_long_stop_caps_loss_and_goes_flat(self):
        # Enter long at bar3 (close=100), crash at bar4, recover at bar5.
        df = _df([100, 100, 100, 100, 90, 100])
        signals = pd.Series([0, 0, 1, 1, 1, 0])
        gross, held = pu.compute_returns_with_stop(df, signals, stop_mult=2.0, atr_window=2)
        # ATR≈2, stop = 100 - 2*2 = 96; bar4 low=89 <= 96 -> stopped at 96
        # gross is length n-1 (bars 1..5)
        assert gross.iloc[3] == pytest.approx(-0.04, abs=1e-6)   # capped loss (bar4)
        assert gross.iloc[4] == pytest.approx(0.0)               # flat after stop (bar5)
        # held: position actually carried each bar; flat after stop
        assert held.iloc[3] == 1.0   # bar3 in position
        assert held.iloc[4] == 1.0   # bar4 in position (stop fires this bar)
        assert held.iloc[5] == 0.0   # bar5 flat after stop

    def test_short_stop_caps_loss(self):
        # Enter short at bar3 (close=100), spike up at bar4.
        df = _df([100, 100, 100, 100, 110, 100])
        signals = pd.Series([0, 0, -1, -1, -1, 0])
        gross, held = pu.compute_returns_with_stop(df, signals, stop_mult=2.0, atr_window=2)
        # short stop = 100 + 2*2 = 104; bar4 high=111 >= 104 -> stopped at 104
        # short return when stopped = (prev_c - stop)/prev_c = (100-104)/100 = -0.04
        assert gross.iloc[3] == pytest.approx(-0.04, abs=1e-6)
        assert gross.iloc[4] == pytest.approx(0.0)
        assert held.iloc[5] == 0.0

    def test_no_stop_when_not_breached(self):
        # Gentle uptrend, stop never hit -> matches plain long returns
        df = _df([100, 101, 102, 103, 104, 105])
        signals = pd.Series([1, 1, 1, 1, 1, 1])
        gross, _ = pu.compute_returns_with_stop(df, signals, stop_mult=5.0, atr_window=2)
        plain = pu.compute_strategy_returns(df, signals).reset_index(drop=True)
        # where in position and unstopped, returns equal plain long returns
        pd.testing.assert_series_equal(gross.iloc[2:], plain.iloc[2:], check_names=False)


class TestNetReturnsDispatch:
    def test_params_none_is_legacy_path(self):
        df = _df([100, 101, 100, 102, 101, 103])
        signals = pd.Series([1, 1, 1, 1, 1, 1])
        legacy = pu.compute_net_strategy_returns(df, signals, 'EUR_USD', 'D')
        explicit_none = pu.compute_net_strategy_returns(df, signals, 'EUR_USD', 'D', params=None)
        pd.testing.assert_series_equal(legacy, explicit_none)

    def test_params_without_stop_mult_is_legacy_path(self):
        df = _df([100, 101, 100, 102, 101, 103])
        signals = pd.Series([1, 1, 1, 1, 1, 1])
        legacy = pu.compute_net_strategy_returns(df, signals, 'EUR_USD', 'D')
        no_stop = pu.compute_net_strategy_returns(df, signals, 'EUR_USD', 'D', params={'foo': 1})
        pd.testing.assert_series_equal(legacy, no_stop)

    def test_stop_changes_returns(self):
        df = _df([100, 100, 100, 100, 90, 100])
        signals = pd.Series([0, 0, 1, 1, 1, 0])
        no_stop = pu.compute_net_strategy_returns(df, signals, 'EUR_USD', 'D')
        with_stop = pu.compute_net_strategy_returns(
            df, signals, 'EUR_USD', 'D', params={'stop_mult': 2.0, 'atr_window': 2})
        # The capped loss must be strictly less negative than the un-stopped crash
        assert with_stop.min() > no_stop.min()

    def test_stop_sweep_constant_is_loosest_first(self):
        assert pu.STOP_MULT_SWEEP[0] == max(pu.STOP_MULT_SWEEP)  # widest stop first
        assert len(pu.STOP_MULT_SWEEP) >= 2
