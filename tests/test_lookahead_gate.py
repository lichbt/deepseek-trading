"""Tests for the truncation look-ahead gate (validator.truncation_lookahead_flip_rate).

A causal signal recomputed on data truncated at bar t must equal the full-series
signal at t. A clean (rolling/ewm/shift) strategy flips 0%; a look-ahead strategy
(scan-and-fill exit, shift(-k), retroactive editing) flips materially — real leaks
measured 14-91%. The gate FAILs above LOOKAHEAD_MAX_FLIP_RATE (5%).
"""
import os
import sys

import numpy as np
import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
from validator import (create_strategy_function, truncation_lookahead_flip_rate,
                       LOOKAHEAD_MAX_FLIP_RATE)

# Deterministic synthetic OHLC (no Math.random in scripts): a wandering price.
def _data(n=400):
    idx = pd.date_range('2015-01-01', periods=n, freq='D')
    steps = np.sin(np.arange(n) / 7.0) + np.cos(np.arange(n) / 3.0)
    close = 100 + np.cumsum(steps) * 0.5
    high = close + 0.5
    low = close - 0.5
    return pd.DataFrame({'date': idx, 'open': close, 'high': high, 'low': low,
                         'close': close})


CLEAN_CODE = '''
import pandas as pd, numpy as np
def generate_signals(df, params):
    lb = params.get("lb", 10)
    ma = df["close"].rolling(lb).mean()
    # causal: position from a backward-looking MA only
    pos = np.where(df["close"] > ma, 1, np.where(df["close"] < ma, -1, 0))
    return pd.Series(pos, index=df.index)
'''

# scan-and-fill exit: look FORWARD from each entry to find the exit bar, then
# back-fill the hold span. Reads causal but the hold at t depends on a FUTURE exit.
LEAK_SCANFILL_CODE = '''
import pandas as pd, numpy as np
def generate_signals(df, params):
    lb = params.get("lb", 10)
    ma = df["close"].rolling(lb).mean()
    raw = np.where(df["close"] > ma, 1, np.where(df["close"] < ma, -1, 0))
    pos = np.zeros(len(df), dtype=int)
    for i in np.flatnonzero(raw):
        d = raw[i]
        target = df["close"].iloc[i] + d * 2.0
        if d == 1:
            cond = (df["close"].iloc[i:] >= target)
        else:
            cond = (df["close"].iloc[i:] <= target)
        ex = int(np.argmax(cond.values))
        ex = (len(df) - i) if (ex == 0 and not cond.iloc[0]) else ex + i
        pos[i:ex] = d
    return pd.Series(pos, index=df.index)
'''

# blatant future peek: shift(-1)
LEAK_SHIFT_CODE = '''
import pandas as pd, numpy as np
def generate_signals(df, params):
    fut = df["close"].shift(-1)
    pos = np.where(fut > df["close"], 1, -1)
    return pd.Series(pos, index=df.index)
'''


class TestLookaheadGate:
    def test_clean_strategy_zero_flips(self):
        fn = create_strategy_function(CLEAN_CODE)
        rate, n = truncation_lookahead_flip_rate(fn, _data(), {"lb": 10})
        assert rate is not None and n >= 30
        assert rate <= LOOKAHEAD_MAX_FLIP_RATE          # a causal strategy passes
        assert rate == 0.0                              # in fact exactly 0

    def test_scanfill_exit_is_flagged(self):
        fn = create_strategy_function(LEAK_SCANFILL_CODE)
        rate, n = truncation_lookahead_flip_rate(fn, _data(), {"lb": 10})
        assert rate is not None
        assert rate > LOOKAHEAD_MAX_FLIP_RATE           # scan-and-fill look-ahead caught

    def test_future_shift_is_flagged(self):
        fn = create_strategy_function(LEAK_SHIFT_CODE)
        rate, n = truncation_lookahead_flip_rate(fn, _data(), {})
        assert rate is not None
        assert rate > LOOKAHEAD_MAX_FLIP_RATE           # shift(-1) caught

    def test_failsoft_on_too_little_data(self):
        fn = create_strategy_function(CLEAN_CODE)
        rate, n = truncation_lookahead_flip_rate(fn, _data(50), {"lb": 10})
        assert rate is None                             # not testable → gate skips, never fails
