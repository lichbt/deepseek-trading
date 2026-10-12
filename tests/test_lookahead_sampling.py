"""Look-ahead gate must sample an even stride, not just the frame tail.

Regression: spx500usd_auto_20261005_185443_i30 read its entry day's FUTURE close
at the 09:00 bar. The dev-window tail (last 120 bars) flipped 0%; stride samples
across history flipped 5-12%. Tail-only sampling passed the leak.
"""
import numpy as np
import pandas as pd
import validator as V


def _frame(n=800):
    rng = np.random.default_rng(0)
    close = 100 * np.exp(np.cumsum(rng.normal(0, 0.01, n)))
    return pd.DataFrame({
        'date': pd.date_range('2020-01-01', periods=n, freq='4h'),
        'open': close, 'high': close * 1.001, 'low': close * 0.999, 'close': close,
    })


def _causal(df, params):
    return pd.Series(np.where(df['close'].pct_change().fillna(0) > 0, 1, -1),
                     index=df.index)


def _leak_first_half(df, params):
    """Reads NEXT bar close, but only in the first half of the frame.

    A tail-only sampler sees flat zeros over its window and reports a clean 0%.
    """
    close = df['close'].values
    nxt = np.r_[close[1:], close[-1]]
    sig = np.zeros(len(close))
    half = len(close) // 2
    sig[:half] = np.where(nxt[:half] > close[:half], 1, -1)
    return pd.Series(sig, index=df.index)


def test_causal_signal_flips_zero():
    rate, n = V.truncation_lookahead_flip_rate(_causal, _frame(), {})
    assert rate == 0.0 and n >= V.LOOKAHEAD_MIN_CHECKED


def test_era_local_leak_caught_by_stride_sampling():
    rate, n = V.truncation_lookahead_flip_rate(_leak_first_half, _frame(), {})
    assert rate is not None and n >= V.LOOKAHEAD_MIN_CHECKED
    assert rate > V.LOOKAHEAD_MAX_FLIP_RATE, f'era-local leak missed: {rate}'
