"""Tests for risk.py."""

import pytest
import numpy as np
import pandas as pd

import risk


def _pos_returns(n=252, seed=42):
    """Generate positively-skewed return series."""
    rng = np.random.default_rng(seed)
    return pd.Series(rng.normal(0.001, 0.02, n))


def _neg_returns(n=252, seed=42):
    """Generate negatively-skewed return series."""
    rng = np.random.default_rng(seed)
    return pd.Series(rng.normal(-0.001, 0.02, n))


def _zero_returns(n=100):
    return pd.Series(np.zeros(n))


class TestKelly:
    def test_positive_returns(self):
        rets = _pos_returns()
        f = risk.compute_kelly_fraction(rets)
        assert 0 < f <= 1.0

    def test_negative_returns(self):
        rets = _neg_returns()
        f = risk.compute_kelly_fraction(rets)
        assert f == 0.0

    def test_zero_returns(self):
        f = risk.compute_kelly_fraction(_zero_returns())
        assert f == 0.0

    def test_empty_returns(self):
        f = risk.compute_kelly_fraction(pd.Series([], dtype=float))
        assert f == 0.0

    def test_half_kelly(self):
        rets = _pos_returns()
        full = risk.compute_kelly_fraction(rets)
        half = risk.compute_half_kelly(rets)
        assert half == 0.5 * full

    def test_clamped_at_one(self):
        rng = np.random.default_rng(42)
        rets = pd.Series(rng.normal(0.05, 0.01, 252))
        f = risk.compute_kelly_fraction(rets)
        assert f <= 1.0

    def test_kelly_allocations_sum_le_one(self):
        r1 = _pos_returns(seed=42)
        r2 = _pos_returns(seed=99)
        r3 = _pos_returns(seed=7)
        allocs = risk.compute_kelly_allocations(
            {'a': r1, 'b': r2, 'c': r3}, mode='half', total_capital=1.0
        )
        total = sum(allocs.values())
        assert total <= 1.0 + 1e-9

    def test_kelly_all_zero_when_all_negative(self):
        r1 = _neg_returns()
        r2 = _neg_returns(seed=99)
        allocs = risk.compute_kelly_allocations({'a': r1, 'b': r2})
        assert sum(allocs.values()) == 0.0


class TestDrawdown:
    def test_max_drawdown_positive(self):
        rets = pd.Series([0.01, 0.02, -0.05, 0.01])
        dd_val = risk.compute_max_drawdown(rets)
        assert dd_val > 0

    def test_max_drawdown_zero_on_flat_up(self):
        rets = pd.Series([0.01, 0.02, 0.01, 0.005])
        dd_val = risk.compute_max_drawdown(rets)
        assert dd_val == pytest.approx(0.0, abs=0.01)

    def test_drawdown_duration(self):
        rets = pd.Series([0.01, -0.03, -0.01, -0.02, 0.05])
        dur = risk.compute_max_drawdown_duration(rets)
        assert dur >= 2

    def test_drawdown_duration_zero(self):
        rets = pd.Series([0.01, 0.02, 0.01])
        dur = risk.compute_max_drawdown_duration(rets)
        assert dur == 0


class TestDrawdownCircuitBreaker:
    def test_active_by_default(self):
        cb = risk.DrawdownCircuitBreaker()
        assert cb.is_active

    def test_trigger_on_big_loss(self):
        cb = risk.DrawdownCircuitBreaker(
            risk.DrawdownLimits(max_drawdown_pct=0.05, min_observations=5)
        )
        for _ in range(20):
            cb.feed_return(0.001)
        result = cb.feed_return(-0.10)
        assert result['action'] == 'halt'
        assert not cb.is_active

    def test_no_trigger_on_small_loss(self):
        cb = risk.DrawdownCircuitBreaker(
            risk.DrawdownLimits(max_drawdown_pct=0.5)
        )
        for _ in range(20):
            cb.feed_return(0.001)
        result = cb.feed_return(-0.02)
        assert result['action'] == 'continue'
        assert cb.is_active

    def test_recovery(self):
        cb = risk.DrawdownCircuitBreaker(
            risk.DrawdownLimits(
                max_drawdown_pct=0.05,
                recovery_threshold_pct=0.02,
                min_observations=5
            )
        )
        for _ in range(20):
            cb.feed_return(0.001)
        cb.feed_return(-0.10)  # trigger halt
        assert not cb.is_active
        for _ in range(10):
            result = cb.feed_return(0.02)
        assert result['action'] in ('resume', 'continue')
        assert cb.is_active

    def test_reset(self):
        cb = risk.DrawdownCircuitBreaker(
            risk.DrawdownLimits(max_drawdown_pct=0.05, min_observations=5)
        )
        for _ in range(20):
            cb.feed_return(0.001)
        cb.feed_return(-0.10)
        assert not cb.is_active
        cb.reset()
        assert cb.is_active


class TestCorrelation:
    def test_correlation_matrix(self):
        rng = np.random.default_rng(42)
        r1 = pd.Series(rng.normal(0.001, 0.02, 100))
        r2 = pd.Series(rng.normal(0.001, 0.02, 100))
        corr = risk.compute_correlation_matrix({'a': r1, 'b': r2})
        assert corr.shape == (2, 2)
        assert corr.iloc[0, 0] == 1.0
        assert corr.iloc[1, 1] == 1.0

    def test_effective_n_uncorrelated(self):
        rng = np.random.default_rng(42)
        r1 = pd.Series(rng.normal(0.001, 0.02, 200))
        r2 = pd.Series(rng.normal(0.001, 0.02, 200))
        r3 = pd.Series(rng.normal(0.001, 0.02, 200))
        eff_n = risk.compute_effective_n({'a': r1, 'b': r2, 'c': r3})
        assert eff_n > 1.5

    def test_effective_n_perfectly_correlated(self):
        r1 = pd.Series([0.01, -0.02, 0.03, 0.01] * 50)
        r2 = r1 * 1.0
        eff_n = risk.compute_effective_n({'a': r1, 'b': r2})
        assert eff_n < 2.0

    def test_diversification_score(self):
        rng = np.random.default_rng(42)
        r1 = pd.Series(rng.normal(0.001, 0.02, 200))
        r2 = r1 + pd.Series(rng.normal(0, 0.005, 200))
        score = risk.diversification_score({'a': r1, 'b': r2})
        assert 0 <= score <= 1

    def test_find_correlated_pairs(self):
        r1 = pd.Series([0.01, -0.01] * 100)
        r2 = pd.Series([0.01, -0.01] * 100)  # perfectly correlated
        r3 = pd.Series([-0.01, 0.01] * 100)  # perfectly anti-correlated
        pairs = risk.find_correlated_pairs({'a': r1, 'b': r2, 'c': r3}, threshold=0.9)
        assert len(pairs) >= 1

    def test_empty_correlation(self):
        corr = risk.compute_correlation_matrix({})
        assert corr.empty
        eff_n = risk.compute_effective_n({})
        assert eff_n == 0.0


class TestPortfolioMetrics:
    def test_portfolio_returns(self):
        r1 = pd.Series([0.01, -0.02, 0.03])
        r2 = pd.Series([-0.01, 0.01, 0.02])
        port = risk.compute_portfolio_returns({'a': r1, 'b': r2}, {'a': 0.5, 'b': 0.5})
        expected = r1 * 0.5 + r2 * 0.5
        pd.testing.assert_series_equal(port, expected)

    def test_portfolio_drawdown(self):
        r1 = pd.Series([0.01, -0.02, 0.03])
        r2 = pd.Series([-0.01, 0.01, 0.02])
        dd = risk.compute_portfolio_drawdown({'a': r1, 'b': r2}, {'a': 0.5, 'b': 0.5})
        assert dd >= 0

    def test_calmar_ratio(self):
        rets = _pos_returns()
        calmar = risk.compute_calmar_ratio(rets)
        assert calmar > 0

    def test_ulcer_index(self):
        rets = _pos_returns()
        ui = risk.compute_ulcer_index(rets)
        assert ui > 0
