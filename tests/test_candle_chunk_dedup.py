"""Chunked intraday fetches must not duplicate the candle on a chunk boundary.

get_candles_date_range walks intraday history in fixed-day chunks and starts
chunk N+1 on chunk N's end timestamp. A candle that STRADDLES that boundary is
returned by both requests. H4 hits this on every boundary in production (bars
sit at the 21:00/22:00 NY close, boundaries land on midnight), which put 9
duplicate rows into every full-history ETH_USD H4 frame and made
evaluate_strategy raise "cannot reindex on an axis with duplicate labels".

These fail against the pre-dedup data_fetcher.
"""
import pandas as pd
import pytest

import data_fetcher


def _bars(start, periods, freq='4H'):
    idx = pd.date_range(start, periods=periods, freq=freq)
    return pd.DataFrame({
        'date': idx,
        'open': range(len(idx)), 'high': range(len(idx)),
        'low': range(len(idx)), 'close': range(len(idx)),
    })


@pytest.fixture
def straddling_chunks(monkeypatch):
    """Serve each chunk INCLUDING the bar that straddles its far boundary."""
    def fake_get_candles(instrument, granularity, start, end, with_spread=False):
        s, e = pd.Timestamp(start).tz_localize(None), pd.Timestamp(end).tz_localize(None)
        # bars offset from midnight, as OANDA's NY-close-aligned H4 bars are
        all_bars = _bars('2020-01-01 21:00', 4000)
        m = (all_bars['date'] >= s - pd.Timedelta('4H')) & (all_bars['date'] < e)
        return all_bars[m].reset_index(drop=True)

    monkeypatch.setattr(data_fetcher, 'get_candles', fake_get_candles)
    monkeypatch.setattr(data_fetcher, '_load_cached_dataframe', lambda *a, **k: None)
    monkeypatch.setattr(data_fetcher, '_store_cached_dataframe', lambda *a, **k: None)


def test_boundary_candle_is_not_duplicated(straddling_chunks):
    df = data_fetcher.get_candles_date_range('ETH_USD', '2020-01-01', '2021-06-01', 'H4')
    dups = df['date'][df['date'].duplicated()]
    assert dups.empty, f'duplicate bars at chunk boundaries: {list(dups)}'


def test_result_is_usable_as_a_reindex_target(straddling_chunks):
    """The exact operation that crashed: reindex onto the frame's own index."""
    df = data_fetcher.get_candles_date_range('ETH_USD', '2020-01-01', '2021-06-01', 'H4')
    idx = pd.DatetimeIndex(df['date'])
    pd.Series(range(len(idx)), index=idx).reindex(idx)  # raises on duplicate labels


def test_dedup_keeps_the_first_copy_and_preserves_order(straddling_chunks):
    df = data_fetcher.get_candles_date_range('ETH_USD', '2020-01-01', '2021-06-01', 'H4')
    assert df['date'].is_monotonic_increasing
    assert list(df.index) == list(range(len(df)))  # reset, not a gappy concat index


def test_no_real_bars_are_dropped(straddling_chunks):
    """Dedup must remove only the redundant copies, never a distinct bar."""
    df = data_fetcher.get_candles_date_range('ETH_USD', '2020-01-01', '2021-06-01', 'H4')
    assert len(df) == df['date'].nunique()
    expected = _bars('2020-01-01 21:00', 4000)
    expected = expected[(expected['date'] >= '2020-01-01') & (expected['date'] < '2021-06-01')]
    assert set(df['date']) == set(expected['date'])
