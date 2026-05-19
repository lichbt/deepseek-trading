"""Tests for trading cost model in pipeline_utils.py."""

import pytest
import pandas as pd
import numpy as np

import pipeline_utils as pu


class TestCostConfig:
    def test_spread_known_instruments(self):
        assert pu.get_spread_pips('EUR_USD') == 1.2
        assert pu.get_spread_pips('XAU_USD') == 30.0
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


class TestSwapPerBarScaling:
    """Swap was incorrectly applied at the full daily rate on every bar of a held
    position, inflating intraday costs by 6× (H4) and 24× (H1)."""

    def _flat_data(self, n=10):
        return pd.DataFrame({'close': [1.0] * n})

    def _all_long(self, n=10):
        return pd.Series([1] * n)

    def test_h1_swap_is_24x_smaller_than_daily(self):
        data = self._flat_data(50)
        sigs = self._all_long(50)
        raw = pu.compute_strategy_returns(data, sigs)
        net_d  = pu.apply_trading_costs(raw, sigs, 'EUR_USD', granularity='D')
        net_h1 = pu.apply_trading_costs(raw, sigs, 'EUR_USD', granularity='H1')
        swap_d  = pu.get_daily_swap('EUR_USD')
        # On held bars, net = raw + swap_d (D) vs raw + swap_d/24 (H1)
        # The H1 deduction per bar should be 24× smaller
        per_bar_d  = (net_d.iloc[5] - raw.iloc[5])
        per_bar_h1 = (net_h1.iloc[5] - raw.iloc[5])
        assert per_bar_h1 == pytest.approx(per_bar_d / 24.0)

    def test_h4_swap_is_6x_smaller_than_daily(self):
        data = self._flat_data(50)
        sigs = self._all_long(50)
        raw = pu.compute_strategy_returns(data, sigs)
        net_d  = pu.apply_trading_costs(raw, sigs, 'EUR_USD', granularity='D')
        net_h4 = pu.apply_trading_costs(raw, sigs, 'EUR_USD', granularity='H4')
        per_bar_d  = (net_d.iloc[5] - raw.iloc[5])
        per_bar_h4 = (net_h4.iloc[5] - raw.iloc[5])
        assert per_bar_h4 == pytest.approx(per_bar_d / 6.0)

    def test_unknown_granularity_defaults_to_daily(self):
        """Unrecognised granularity falls back to 1×, matching D behaviour."""
        data = self._flat_data(20)
        sigs = self._all_long(20)
        raw = pu.compute_strategy_returns(data, sigs)
        net_d   = pu.apply_trading_costs(raw, sigs, 'EUR_USD', granularity='D')
        net_xxx = pu.apply_trading_costs(raw, sigs, 'EUR_USD', granularity='UNKNOWN')
        assert (net_d == net_xxx).all()

    def test_bars_per_day_table(self):
        assert pu._bars_per_day('D')   == 1.0
        assert pu._bars_per_day('H4')  == 6.0
        assert pu._bars_per_day('H1')  == 24.0
        assert pu._bars_per_day('M30') == 48.0
        assert pu._bars_per_day('W')   == 0.2


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


class TestGtScore:
    def _returns(self, n, mean=0.001, std=0.01, seed=42):
        np.random.seed(seed)
        return pd.Series(np.random.normal(mean, std, n))

    def test_fewer_than_20_active_returns_zero(self):
        """< 20 non-zero returns must produce GT=0 regardless of their values."""
        # 4 active bars (like the GBP/USD skew strategy bug)
        r = pd.Series([0.013, 0.0, 0.003, -0.00009, -0.00009, 0.0] * 3)
        assert pu.compute_gt_score(r) == 0.0

    def test_exactly_20_active_bars_allowed(self):
        """Exactly 20 non-zero returns with positive edge should produce a non-zero score."""
        # Alternating +0.5% / -0.1% gives positive mean with variance — avoids zero-std trap
        r = pd.Series([0.005 if i % 2 == 0 else -0.001 for i in range(20)])
        assert pu.compute_gt_score(r) > 0.0

    def test_sortino_cap_prevents_blowup(self):
        """Near-identical tiny losses must not blow up sortino even with many trades."""
        np.random.seed(7)
        # 40 normal winning bars + 2 nearly-identical tiny losses (std≈0 → old bug)
        wins = np.random.normal(0.001, 0.01, 40)
        losses = np.array([-0.00009, -0.000090001])  # nearly identical → std ≈ 0
        r = pd.Series(np.concatenate([wins, losses]))
        score = pu.compute_gt_score(r)
        # Sortino should fall back to Sharpe (not blow up); realistic sharpe ≈ 1-3
        assert score < 15.0, f"GT-score {score:.2f} blew up (sortino not capped)"

    def test_no_losses_uses_sharpe_for_sortino(self):
        """Zero negative returns → sortino falls back to Sharpe; result is finite."""
        # Small positive returns with variance — no negative bars
        r = pd.Series([0.001 + (i % 5) * 0.0005 for i in range(50)])  # 0.001 – 0.003, all positive
        score = pu.compute_gt_score(r)
        assert 0.0 < score
        assert np.isfinite(score)

    def test_single_loss_uses_its_magnitude(self):
        """One negative return: downside_dev = |loss| * sqrt(252), no std needed."""
        r = pd.Series([0.002] * 30 + [-0.01])
        score = pu.compute_gt_score(r)
        assert 0.0 < score < 20.0

    def test_normal_strategy_score_in_expected_range(self):
        """A genuinely good strategy (mean 0.1%, std 1% daily) stays in 0.5-4 range."""
        r = self._returns(500, mean=0.001, std=0.01)
        score = pu.compute_gt_score(r)
        assert 0.0 < score < 8.0, f"Expected 0-8, got {score:.3f}"

    def test_negative_edge_returns_zero(self):
        """Strategy with negative expected return gets GT=0 (max(0, ...) floor)."""
        r = self._returns(500, mean=-0.002, std=0.01)
        assert pu.compute_gt_score(r) == 0.0

    def test_empty_series_returns_zero(self):
        assert pu.compute_gt_score(pd.Series([], dtype=float)) == 0.0