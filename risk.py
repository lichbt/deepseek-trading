"""
Risk Management: Kelly position sizing, max drawdown circuit breaker,
and multi-strategy correlation analysis.

All functions operate on either pd.Series of returns or lists of return series.
"""

import numpy as np
import pandas as pd
from typing import List, Dict, Tuple, Optional
from dataclasses import dataclass, field


# ============================================================================
# KELLY POSITION SIZING
# ============================================================================

def compute_kelly_fraction(returns: pd.Series, risk_free: float = 0.0) -> float:
    """
    Compute the Kelly fraction for a return stream.

    f* = (mu - rf) / sigma^2
    where mu = mean return, sigma^2 = variance, rf = risk-free rate.

    If f* <= 0, returns 0 (don't bet).
    Clamped to [0, 1] for no-leverage constraint.

    Args:
        returns: Series of periodic returns
        risk_free: per-period risk-free rate (default 0)

    Returns:
        Kelly fraction in [0, 1]
    """
    returns = returns.dropna()
    if len(returns) < 2:
        return 0.0

    mu = returns.mean()
    var = returns.var()

    if var < 1e-10:
        return 0.0

    f_star = (mu - risk_free) / var

    return float(np.clip(f_star, 0.0, 1.0))


def compute_half_kelly(returns: pd.Series, risk_free: float = 0.0) -> float:
    """
    Half-Kelly: more conservative, reduces volatility while retaining most growth.

    f_half = 0.5 * f_kelly
    """
    return 0.5 * compute_kelly_fraction(returns, risk_free)


def compute_kelly_allocations(
    return_series: Dict[str, pd.Series],
    mode: str = 'half',
    total_capital: float = 1.0
) -> Dict[str, float]:
    """
    Compute Kelly allocation across multiple strategies.

    If sum of individual fractions > 1.0, scale down proportionally
    to respect no-leverage constraint.

    Args:
        return_series: {strategy_id: returns_series}
        mode: 'full' or 'half' (default: half for safety)
        total_capital: total capital to allocate (default 1.0)

    Returns:
        {strategy_id: capital_allocation}
    """
    fn = compute_half_kelly if mode == 'half' else compute_kelly_fraction
    raw_allocations = {}

    for sid, r in return_series.items():
        f = fn(r)
        raw_allocations[sid] = f

    total = sum(raw_allocations.values())

    allocations = {}
    if total > 1.0:
        for sid, f in raw_allocations.items():
            allocations[sid] = (f / total) * total_capital
    elif total == 0.0:
        for sid in raw_allocations:
            allocations[sid] = 0.0
    else:
        for sid, f in raw_allocations.items():
            allocations[sid] = f * total_capital

    return allocations


# ============================================================================
# MAX DRAWDOWN CIRCUIT BREAKER
# ============================================================================

@dataclass
class DrawdownLimits:
    """Configuration for drawdown circuit breaker."""
    max_drawdown_pct: float = 0.20      # halt if drawdown exceeds 20%
    recovery_threshold_pct: float = 0.10  # resume only if drawdown recovers above 10%
    lookback_days: int = 252            # rolling window for drawdown calc
    min_observations: int = 20          # need at least this many returns


def compute_drawdown_series(returns: pd.Series) -> pd.Series:
    """
    Compute drawdown series from returns.

    Args:
        returns: Period returns (can be cumulative or standalone)

    Returns:
        Drawdown as positive fraction (0.05 = 5% drawdown)
    """
    returns = returns.dropna()
    if len(returns) < 2:
        return pd.Series(0.0, index=returns.index)

    cumulative = (1.0 + returns).cumprod()
    running_max = cumulative.cummax()
    drawdown = (running_max - cumulative) / running_max
    return drawdown


def compute_current_drawdown(returns: pd.Series) -> float:
    """
    Compute the CURRENT drawdown (from peak to latest value).
    Returns 0 if at or above the all-time high.
    """
    returns = returns.dropna()
    if len(returns) < 2:
        return 0.0
    cumulative = (1.0 + returns).cumprod()
    running_max = cumulative.cummax()
    return float((running_max.iloc[-1] - cumulative.iloc[-1]) / running_max.iloc[-1])


def compute_max_drawdown(returns: pd.Series) -> float:
    """Compute the maximum drawdown from a return stream."""
    dd = compute_drawdown_series(returns)
    return float(dd.max()) if len(dd) > 0 else 0.0


def compute_max_drawdown_duration(returns: pd.Series) -> int:
    """
    Compute maximum drawdown duration in periods (days).

    Returns the longest consecutive stretch below the previous peak.
    """
    returns = returns.dropna()
    if len(returns) < 2:
        return 0

    cumulative = (1.0 + returns).cumprod()
    running_max = cumulative.cummax()
    underwater = cumulative < running_max

    max_duration = 0
    current_duration = 0
    for is_under in underwater:
        if is_under:
            current_duration += 1
            max_duration = max(max_duration, current_duration)
        else:
            current_duration = 0

    return max_duration


class DrawdownCircuitBreaker:
    """
    Monitors drawdown and halts trading when limits are breached.

    States: ACTIVE -> HALTED -> RECOVERY -> ACTIVE
    """

    def __init__(self, limits: DrawdownLimits = None):
        self.limits = limits or DrawdownLimits()
        self._state = 'ACTIVE'
        self._returns = []
        self._halt_reason = None

    @property
    def state(self) -> str:
        return self._state

    @property
    def is_active(self) -> bool:
        return self._state == 'ACTIVE'

    def feed_return(self, ret: float) -> Dict[str, any]:
        """
        Feed a new daily return. Returns dict with state, drawdown, action.

        Returns:
            {'state': str, 'current_drawdown': float, 'action': str, 'reason': str|None}
        """
        self._returns.append(ret)
        rets = pd.Series(self._returns)

        if len(rets) < self.limits.min_observations:
            return {'state': self._state, 'current_drawdown': 0.0, 'action': 'hold', 'reason': None}

        current_dd = compute_current_drawdown(rets)

        if self._state == 'ACTIVE':
            if current_dd >= self.limits.max_drawdown_pct:
                self._state = 'HALTED'
                self._halt_reason = f'drawdown {current_dd:.2%} >= {self.limits.max_drawdown_pct:.0%}'
                return {'state': self._state, 'current_drawdown': current_dd, 'action': 'halt', 'reason': self._halt_reason}
            return {'state': self._state, 'current_drawdown': current_dd, 'action': 'continue', 'reason': None}

        elif self._state == 'HALTED':
            if current_dd <= self.limits.recovery_threshold_pct:
                self._state = 'ACTIVE'
                self._halt_reason = None
                return {'state': self._state, 'current_drawdown': current_dd, 'action': 'resume', 'reason': None}
            return {'state': self._state, 'current_drawdown': current_dd, 'action': 'halted', 'reason': self._halt_reason}

        return {'state': self._state, 'current_drawdown': current_dd, 'action': 'hold', 'reason': None}

    def reset(self):
        self._state = 'ACTIVE'
        self._returns = []
        self._halt_reason = None


# ============================================================================
# MULTI-STRATEGY CORRELATION
# ============================================================================

def compute_correlation_matrix(
    return_series: Dict[str, pd.Series]
) -> pd.DataFrame:
    """
    Compute pairwise correlation matrix between strategies.

    Args:
        return_series: {strategy_id: returns_series}

    Returns:
        DataFrame with correlation coefficients
    """
    if not return_series:
        return pd.DataFrame()

    combined = pd.DataFrame(return_series)
    return combined.corr()


def compute_effective_n(
    return_series: Dict[str, pd.Series],
    min_correlation: float = 0.0
) -> float:
    """
    Compute effective number of independent strategies
    (diversification-adjusted strategy count).

    eff_n = 1 / (average squared correlation)
    If strategies are perfectly uncorrelated, eff_n = actual_n.

    Args:
        return_series: {strategy_id: returns_series}
        min_correlation: floor for correlation (prevents division by zero)

    Returns:
        Effective strategy count
    """
    corr = compute_correlation_matrix(return_series)
    if corr.empty:
        return 0.0

    n = len(corr)
    if n <= 1:
        return float(n)

    upper_tri = corr.values[np.triu_indices(n, k=1)]
    if len(upper_tri) == 0:
        return float(n)

    avg_sq_corr = np.mean(upper_tri ** 2)
    avg_sq_corr = max(avg_sq_corr, min_correlation)

    denom = 1.0 / n + (1.0 - 1.0 / n) * avg_sq_corr
    if denom < 1e-10:
        return float(n)

    return 1.0 / denom


def diversification_score(
    return_series: Dict[str, pd.Series]
) -> float:
    """
    Diversification score from 0 (perfectly correlated) to 1 (perfectly uncorrelated).

    score = 1 - mean(|off_diagonal_correlations|)
    """
    corr = compute_correlation_matrix(return_series)
    if corr.empty:
        return 0.0

    n = len(corr)
    if n <= 1:
        return 1.0

    upper_tri = corr.values[np.triu_indices(n, k=1)]
    if len(upper_tri) == 0:
        return 1.0

    mean_abs_corr = np.mean(np.abs(upper_tri))
    return float(1.0 - mean_abs_corr)


def find_correlated_pairs(
    return_series: Dict[str, pd.Series],
    threshold: float = 0.7
) -> List[Tuple[str, str, float]]:
    """
    Find pairs of strategies with correlation above threshold.

    Returns:
        List of (id1, id2, correlation) tuples sorted by |corr| descending
    """
    corr = compute_correlation_matrix(return_series)
    pairs = []
    ids = list(corr.index)
    for i in range(len(ids)):
        for j in range(i + 1, len(ids)):
            val = corr.iloc[i, j]
            if abs(val) >= threshold:
                pairs.append((ids[i], ids[j], val))
    pairs.sort(key=lambda x: abs(x[2]), reverse=True)
    return pairs


# ============================================================================
# PORTFOLIO-LEVEL METRICS
# ============================================================================

def compute_portfolio_returns(
    return_series: Dict[str, pd.Series],
    allocations: Dict[str, float]
) -> pd.Series:
    """
    Compute weighted portfolio returns.

    Args:
        return_series: {strategy_id: period_returns}
        allocations: {strategy_id: weight} (should sum <= 1 for no leverage)

    Returns:
        Combined portfolio return series
    """
    if not return_series:
        return pd.Series(dtype=float)

    aligned = pd.DataFrame(return_series).dropna()
    if aligned.empty:
        return pd.Series(dtype=float)

    port_rets = pd.Series(0.0, index=aligned.index)
    for sid, weight in allocations.items():
        if sid in aligned.columns and weight > 0:
            port_rets += aligned[sid] * weight

    return port_rets


def compute_portfolio_drawdown(
    return_series: Dict[str, pd.Series],
    allocations: Dict[str, float]
) -> float:
    """Compute max drawdown of a multi-strategy portfolio."""
    port_rets = compute_portfolio_returns(return_series, allocations)
    if len(port_rets) < 2:
        return 0.0
    return compute_max_drawdown(port_rets)


def compute_rolling_correlation(
    return_series: Dict[str, pd.Series],
    window: int = 60
) -> pd.DataFrame:
    """
    Compute rolling pairwise correlations.

    Returns multi-level DataFrame with (id1, id2) columns of rolling correlations.
    """
    if not return_series:
        return pd.DataFrame()

    combined = pd.DataFrame(return_series)
    if len(combined.columns) < 2:
        return pd.DataFrame()

    ids = list(combined.columns)
    result = pd.DataFrame(index=combined.index)

    for i in range(len(ids)):
        for j in range(i + 1, len(ids)):
            col = f'{ids[i]}_{ids[j]}'
            result[col] = combined[ids[i]].rolling(window).corr(combined[ids[j]])

    return result


# ============================================================================
# EQUITY CURVE RISK METRICS
# ============================================================================

def compute_calmar_ratio(returns: pd.Series) -> float:
    """
    Calmar Ratio = annualized return / max drawdown.

    Higher is better. Typical threshold: > 0.5 is acceptable.
    """
    returns = returns.dropna()
    if len(returns) < 2:
        return 0.0

    ann_return = returns.mean() * 252
    max_dd = compute_max_drawdown(returns)
    if max_dd < 1e-6:
        return 0.0
    return ann_return / max_dd


def compute_ulcer_index(returns: pd.Series) -> float:
    """
    Ulcer Index: measures depth and duration of drawdowns.

    UI = sqrt(mean(drawdown^2))

    Lower is better.
    """
    returns = returns.dropna()
    if len(returns) < 2:
        return 0.0

    dd = compute_drawdown_series(returns)
    return float(np.sqrt(np.mean(dd ** 2)))
