"""Tests for fingerprint.py — measured-structure probe + contradiction gate.

The probe FAILS SOFT everywhere (returns None / no contradiction) so it can
never break thesis generation; these tests pin both the happy path and that
soft-failure contract.
"""
import os
import sys

import numpy as np
import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
import fingerprint as fp


def _fp(ac1=0.0, er=0.25, skew=0.0, kurt=0.0, vol_ac1=0.0, cal=None):
    d = {'instrument': 'EUR_USD', 'granularity': 'D', 'window': ('2015-01-01', '2024-01-01'),
         'n': 2000, 'ac1': ac1, 'ac5': 0.0, 'ac20': 0.0, 'vol_ac1': vol_ac1, 'er': er,
         'skew': skew, 'kurt': kurt, 'calendar': cal or {'dow': None, 'monthend': None}}
    d['character'] = fp._character(d)
    return d


class TestContradiction:
    def test_reversion_thesis_on_momentum_series_contradicts(self):
        thesis = {'strategy_family': 'mean_reversion',
                  'rationale': 'price reverts after an oversold extreme'}
        out = fp.contradiction(thesis, _fp(ac1=0.10))   # strongly positive autocorr
        assert out and 'mean-reversion' in out and 'positively autocorrelated' in out

    def test_momentum_thesis_on_reverting_series_contradicts(self):
        thesis = {'strategy_family': 'trend',
                  'rationale': 'the trend persists once momentum establishes'}
        out = fp.contradiction(thesis, _fp(ac1=-0.10))
        assert out and 'trend/momentum' in out and 'negatively autocorrelated' in out

    def test_reversion_thesis_on_reverting_series_is_consistent(self):
        thesis = {'strategy_family': 'mean_reversion', 'rationale': 'fade the oversold bounce'}
        assert fp.contradiction(thesis, _fp(ac1=-0.10)) is None

    def test_weak_autocorr_never_contradicts(self):
        # below the AC_CONTRADICTION threshold -> regime gating can still work
        thesis = {'strategy_family': 'mean_reversion', 'rationale': 'reversion'}
        assert fp.contradiction(thesis, _fp(ac1=0.03)) is None

    def test_ambiguous_thesis_does_not_fire(self):
        # mentions BOTH reversion and momentum -> don't guess, don't reject
        thesis = {'strategy_family': 'regime',
                  'rationale': 'momentum breakout that then reverts'}
        assert fp.contradiction(thesis, _fp(ac1=0.10)) is None

    def test_none_fingerprint_fails_soft(self):
        assert fp.contradiction({'strategy_family': 'mean_reversion'}, None) is None

    def test_threshold_is_exposed_and_conservative(self):
        assert fp.AC_CONTRADICTION >= 0.05  # daily |ac1| is usually < 0.05


class TestFormatting:
    def test_compact_contains_key_stats_and_handles_none(self):
        s = fp.format_compact(_fp(ac1=0.04, er=0.45, skew=-0.6))
        assert 'STRUCTURE[' in s and 'ac1 +0.04' in s and '→trending' in s
        assert fp.format_compact(None) == ''

    def test_full_block_renders_and_handles_none(self):
        s = fp.format_full(_fp(ac1=-0.07, er=0.2, kurt=5.0, skew=-0.8))
        assert 'MEASURED STRUCTURE' in s and 'character:' in s
        assert 'mean-reverting' in s   # character reflects negative ac1
        assert fp.format_full(None) == ''

    def test_character_flags_fat_tails_and_calendar(self):
        c = fp._character(_fp(ac1=0.0, er=0.5, kurt=6.0, skew=-0.9,
                              cal={'dow': {'day': 'Tue', 'mean_bps': 5.0, 't': 2.4}, 'monthend': None}))
        assert 'fat-tailed' in c and 'downside-asymmetric' in c
        assert 'trending' in c and 'calendar effect present' in c


class TestComputeFingerprintSynthetic:
    def _frame(self, returns, start='2015-01-01'):
        idx = pd.bdate_range(start, periods=len(returns))
        close = 100 * np.cumprod(1 + np.asarray(returns))
        return pd.DataFrame({'date': idx, 'open': close, 'high': close,
                             'low': close, 'close': close})

    def _ar1(self, phi, n=1500, seed=0):
        rng = np.random.default_rng(seed)
        eps = rng.normal(0, 0.01, n)
        r = np.zeros(n)
        for i in range(1, n):
            r[i] = phi * r[i - 1] + eps[i]
        return r

    def _patch(self, monkeypatch, df):
        import validator, data_fetcher
        monkeypatch.setattr(validator, 'get_dev_window', lambda inst: ('2015-01-01', '2024-01-01'))
        monkeypatch.setattr(data_fetcher, 'get_candles_date_range',
                            lambda *a, **k: df)
        fp.compute_fingerprint.cache_clear()

    def test_positive_ar1_reads_as_positive_autocorr(self, monkeypatch):
        self._patch(monkeypatch, self._frame(self._ar1(0.25)))
        out = fp.compute_fingerprint('SYN_POS', 'D')
        assert out is not None and out['ac1'] > 0.10
        # a reversion thesis on this series must now be flagged
        assert fp.contradiction({'strategy_family': 'mean_reversion',
                                 'rationale': 'reverts'}, out) is not None

    def test_negative_ar1_reads_as_mean_reverting(self, monkeypatch):
        self._patch(monkeypatch, self._frame(self._ar1(-0.25)))
        out = fp.compute_fingerprint('SYN_NEG', 'D')
        assert out is not None and out['ac1'] < -0.10
        assert 'mean-reverting' in out['character']

    def test_too_few_bars_returns_none(self, monkeypatch):
        self._patch(monkeypatch, self._frame(self._ar1(0.2, n=50)))
        assert fp.compute_fingerprint('SYN_SHORT', 'D') is None

    def test_fetch_error_fails_soft(self, monkeypatch):
        import validator, data_fetcher
        monkeypatch.setattr(validator, 'get_dev_window', lambda inst: ('2015-01-01', '2024-01-01'))
        def boom(*a, **k):
            raise RuntimeError('oanda down')
        monkeypatch.setattr(data_fetcher, 'get_candles_date_range', boom)
        fp.compute_fingerprint.cache_clear()
        assert fp.compute_fingerprint('SYN_ERR', 'D') is None
