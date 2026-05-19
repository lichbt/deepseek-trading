"""
Tests for data_fetcher.py — focused on TZ handling.

OANDA returns ISO-8601 timestamps with a 'Z' suffix; pd.to_datetime turns
those into datetime64[ns, UTC]. TZ-aware values can't be used as a numpy
dtype, so strategy code doing df['date'].values crashes. _parse_naive_datetime
strips the tz at the source so the whole pipeline gets naive timestamps.
"""
import sys
from pathlib import Path

import pandas as pd
import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

from data_fetcher import _parse_naive_datetime


class TestParseNaiveDatetime:
    def test_tz_aware_iso_string_stripped(self):
        """OANDA-style 'Z'-suffixed ISO strings → TZ-naive datetime64."""
        s = pd.Series(['2019-01-01T00:00:00Z', '2019-01-02T00:00:00Z'])
        out = _parse_naive_datetime(s)
        assert out.dtype == 'datetime64[ns]'
        assert getattr(out.dtype, 'tz', None) is None

    def test_explicit_utc_offset_stripped(self):
        s = pd.Series(['2019-01-01T00:00:00+00:00'])
        out = _parse_naive_datetime(s)
        assert getattr(out.dtype, 'tz', None) is None

    def test_naive_string_unchanged(self):
        """Already-naive strings parse fine and stay naive."""
        s = pd.Series(['2019-01-01', '2019-01-02'])
        out = _parse_naive_datetime(s)
        assert out.dtype == 'datetime64[ns]'
        assert getattr(out.dtype, 'tz', None) is None

    def test_values_usable_as_numpy_dtype(self):
        """Regression: TZ-aware .values crashed numpy with
        'Cannot interpret datetime64[ns, UTC] as a data type'."""
        s = pd.Series(['2019-01-01T00:00:00Z'] * 5)
        out = _parse_naive_datetime(s)
        # This is the operation that crashed inside strategy code
        arr = out.values
        # day-of-week extraction (what day-of-week strategies do)
        dow = out.dt.dayofweek
        assert len(arr) == 5
        assert len(dow) == 5

    def test_preserves_chronological_values(self):
        s = pd.Series(['2019-03-15T12:00:00Z'])
        out = _parse_naive_datetime(s)
        assert out.iloc[0].year == 2019
        assert out.iloc[0].month == 3
        assert out.iloc[0].day == 15
        assert out.iloc[0].hour == 12
