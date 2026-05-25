"""
Tests for data_fetcher.py — TZ handling + bid/ask spread fetch.

OANDA returns ISO-8601 timestamps with a 'Z' suffix; pd.to_datetime turns
those into datetime64[ns, UTC]. TZ-aware values can't be used as a numpy
dtype, so strategy code doing df['date'].values crashes. _parse_naive_datetime
strips the tz at the source so the whole pipeline gets naive timestamps.

with_spread=True triggers a price='BA' fetch and exposes a `spread` column
computed locally from (ask.c - bid.c). Mid OHLC is averaged from bid/ask.
"""
import sys
from pathlib import Path
from unittest.mock import patch, MagicMock

import pandas as pd
import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

from data_fetcher import _parse_naive_datetime, get_candles


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


class TestWithSpread:
    """get_candles(with_spread=True) must request price='BA', synthesise mid
    OHLC from (bid+ask)/2, and expose a `spread` column = ask.c - bid.c.
    Without these the new microstructure archetype produces no spread signal
    and silently falls back to OHLC-only."""

    def _mock_response(self, with_ba: bool):
        """Build a fake OANDA candles response."""
        if with_ba:
            candle = {
                'time': '2026-05-20T00:00:00Z',
                'complete': True,
                'bid': {'o': '1.0995', 'h': '1.1005', 'l': '1.0985', 'c': '1.0998'},
                'ask': {'o': '1.0997', 'h': '1.1007', 'l': '1.0987', 'c': '1.1002'},
            }
        else:
            candle = {
                'time': '2026-05-20T00:00:00Z',
                'complete': True,
                'mid': {'o': '1.0996', 'h': '1.1006', 'l': '1.0986', 'c': '1.1000'},
            }
        resp = MagicMock()
        resp.json.return_value = {'candles': [candle]}
        resp.raise_for_status.return_value = None
        return resp

    @patch('data_fetcher.OANDA_ACCOUNT_ID', 'fake')
    @patch('data_fetcher.OANDA_API_TOKEN', 'fake')
    @patch('data_fetcher.requests.get')
    def test_with_spread_requests_BA_and_returns_spread_column(self, mock_get):
        mock_get.return_value = self._mock_response(with_ba=True)
        df = get_candles('EUR_USD', granularity='D',
                         start='2026-05-20T00:00:00Z', end='2026-05-21T00:00:00Z',
                         with_spread=True)
        # Verify the request asked for BA
        called_params = mock_get.call_args.kwargs['params']
        assert called_params['price'] == 'BA', f'expected BA, got {called_params["price"]!r}'
        # Verify the dataframe has spread + correct mid OHLC
        assert 'spread' in df.columns
        assert len(df) == 1
        row = df.iloc[0]
        # mid close = (1.0998 + 1.1002) / 2 = 1.1000
        assert abs(row['close'] - 1.1000) < 1e-9
        # spread = ask.c - bid.c = 1.1002 - 1.0998 = 0.0004
        assert abs(row['spread'] - 0.0004) < 1e-9
        # spread must always be >= 0 (ask >= bid)
        assert row['spread'] >= 0

    @patch('data_fetcher.OANDA_ACCOUNT_ID', 'fake')
    @patch('data_fetcher.OANDA_API_TOKEN', 'fake')
    @patch('data_fetcher.requests.get')
    def test_default_still_requests_mid(self, mock_get):
        """Backwards compat: existing callers (no with_spread arg) get mid only."""
        mock_get.return_value = self._mock_response(with_ba=False)
        df = get_candles('EUR_USD', granularity='D',
                         start='2026-05-20T00:00:00Z', end='2026-05-21T00:00:00Z')
        called_params = mock_get.call_args.kwargs['params']
        assert called_params['price'] == 'M', f'expected M, got {called_params["price"]!r}'
        assert 'spread' not in df.columns
        assert len(df) == 1
        assert abs(df.iloc[0]['close'] - 1.1000) < 1e-9


class TestSpreadArchetypeInference:
    """The strategy archetype must be inferred as 'spread' when the code
    references df['spread'], so inject_supplementary_data re-fetches with
    bid/ask. Same regression shape as macro: without inference, the live
    runner KeyErrors on every signal because 'spread' isn't in the standard
    OHLC frame."""

    def test_auto_research_infers_spread_archetype(self):
        import auto_research as ar
        code = ("def generate_signals(df, params):\n"
                "    wide = df['spread'] > df['spread'].rolling(20).median()\n"
                "    return wide.astype(int)\n")
        assert ar._infer_archetype(code) == 'spread'

    def test_live_test_infers_spread_archetype(self):
        from live_test import _infer_archetype
        code = "x = df['spread']"
        assert _infer_archetype(code) == 'spread'


class TestEmptyCalendarBug:
    """Regression: merge_calendar_into_data used to set event_impact on the
    empty-calendar path but FORGOT event_surprise, causing KeyError on any
    strategy reading both columns. With the OANDA Labs Calendar dead (403
    audit 2026-05-25), the empty path is hit every time so this fix matters."""

    def test_empty_calendar_sets_both_event_columns(self):
        from supplementary_data import merge_calendar_into_data
        df = pd.DataFrame({
            'date': pd.to_datetime(['2026-05-01', '2026-05-02']),
            'open': 1.0, 'high': 1.0, 'low': 1.0, 'close': 1.0,
        })
        empty_cal = pd.DataFrame()
        out = merge_calendar_into_data(df, empty_cal)
        assert 'event_impact' in out.columns
        assert 'event_surprise' in out.columns
        assert (out['event_impact'] == 'none').all()
        assert (out['event_surprise'] == 0.0).all()
