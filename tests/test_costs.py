"""Tests for trading cost model in pipeline_utils.py."""

import pytest
import pandas as pd
import numpy as np

import pipeline_utils as pu


class TestCostConfig:
    def test_spread_known_instruments(self):
        assert pu.get_spread_pips('EUR_USD') == 1.2
        assert pu.get_spread_pips('XAU_USD') == 40.0
        assert pu.get_spread_pips('USD_JPY') == 0.12

    def test_spread_unknown_instrument(self):
        assert pu.get_spread_pips('EXOTIC_XYZ') == 2.0

    def test_pip_value_forex(self):
        assert pu.get_pip_value('EUR_USD') == 0.0001

    def test_pip_value_jpy(self):
        assert pu.get_pip_value('USD_JPY') == 0.01

    def test_commission_forex(self):
        assert pu.get_commission('EUR_USD') == 0.0

    def test_commission_commodity(self):
        assert pu.get_commission('XAU_USD') == 0.30
        assert pu.get_commission('CORN_USD') == 0.10

    def test_daily_swap(self):
        assert pu.get_daily_swap('EUR_USD') == pytest.approx(-0.00003)
        assert pu.get_daily_swap('BTC_USD') == 0.0  # default


class TestApplyTradingCosts:
    def _make_data_and_signals(self):
        """5 bars: flat → long → hold → exit → flat."""
        data = pd.DataFrame({
            'close': [1.0000, 1.0010, 1.0020, 1.0010, 1.0000]
        })
        signals = pd.Series([0, 1, 1, 0, 0])  # enter bar 1, exit bar 3
        return data, signals

    def test_no_trades_flat(self):
        """No trades → no spread or commission costs."""
        data = pd.DataFrame({'close': [1.0, 1.1, 1.2, 1.1, 1.0]})
        signals = pd.Series([0, 0, 0, 0, 0])
        raw = pu.compute_strategy_returns(data, signals)
        net = pu.apply_trading_costs(raw, signals, 'EUR_USD')
        assert np.allclose(raw, net)

    def test_entry_cost(self):
        """Entry deducts half spread plus swap on first return bar."""
        data, signals = self._make_data_and_signals()
        raw = pu.compute_strategy_returns(data, signals)
        net = pu.apply_trading_costs(raw, signals, 'EUR_USD')
        half_spread = pu.get_spread_pips('EUR_USD') * pu.get_pip_value('EUR_USD') * 0.5
        swap = pu.get_daily_swap('EUR_USD')
        # Entry bar: half spread + swap (position is held for first bar → overnight)
        expected = raw.iloc[0] - half_spread + swap
        assert net.iloc[0] == pytest.approx(expected)

    def test_hold_cost(self):
        """Holding incurs costs (swap, optionally spread on transition)."""
        data = pd.DataFrame({'close': [1.0] * 5})
        signals = pd.Series([1, 1, 1, 1, 0])
        raw = pu.compute_strategy_returns(data, signals)
        net = pu.apply_trading_costs(raw, signals, 'EUR_USD')
        swap = pu.get_daily_swap('EUR_USD')
        # Holding should always incur cost (swap is negative)
        assert (net <= raw).all()
        # Each held bar should have swap applied
        for i in range(len(net)):
            assert net.iloc[i] < raw.iloc[i]  # swap is cost

    def test_commission_commodity(self):
        """Commission deducted on entry with spread and swap."""
        data, signals = self._make_data_and_signals()
        raw = pu.compute_strategy_returns(data, signals)
        net = pu.apply_trading_costs(raw, signals, 'XAU_USD')
        comm = pu.get_commission('XAU_USD')
        half_spread = pu.get_spread_pips('XAU_USD') * pu.get_pip_value('XAU_USD') * 0.5
        swap = pu.get_daily_swap('XAU_USD')
        # Entry: half spread + commission + swap
        expected = raw.iloc[0] - half_spread - comm + swap
        assert net.iloc[0] == pytest.approx(expected)

    def test_reversal(self):
        """Reversal charges full spread plus swap on first return bar."""
        data = pd.DataFrame({'close': [1.0, 1.01, 1.0, 1.01, 1.0]})
        signals = pd.Series([1, -1, 0, 0, 0])
        raw = pu.compute_strategy_returns(data, signals)
        net = pu.apply_trading_costs(raw, signals, 'EUR_USD')
        full_spread = pu.get_spread_pips('EUR_USD') * pu.get_pip_value('EUR_USD')
        swap = pu.get_daily_swap('EUR_USD')
        # First return: full spread + swap
        expected = raw.iloc[0] - full_spread + swap
        assert net.iloc[0] == pytest.approx(expected)


class TestComputeNetStrategyReturns:
    def test_combined_pipeline(self):
        """compute_net_strategy_returns = raw + costs (full pipeline)."""
        data = pd.DataFrame({'close': [1.0000, 1.0010, 1.0020, 1.0010, 1.0000]})
        signals = pd.Series([0, 1, 1, 0, 0])

        net = pu.compute_net_strategy_returns(data, signals, 'EUR_USD')
        raw = pu.compute_strategy_returns(data, signals)

        # Costs should reduce returns
        assert (net <= raw).all()
        assert len(net) == len(raw)

    def test_empty_data(self):
        data = pd.DataFrame({'close': []})
        signals = pd.Series([], dtype=int)
        net = pu.compute_net_strategy_returns(data, signals, 'EUR_USD')
        assert len(net) == 0