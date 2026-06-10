"""
Tests for macro_fetcher — the keyless cache read.

macro_data.db is a cache; a missing FRED_API_KEY must not block reads of data
already in it. Previously get_fred_series raised and enrich_with_macro bailed
without a key, so the whole macro path was dead whenever the key was unset.
"""
import sys
import sqlite3
import tempfile
from pathlib import Path

import pandas as pd
import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

import macro_fetcher as mf


@pytest.fixture
def temp_macro_db(monkeypatch):
    with tempfile.NamedTemporaryFile(suffix='.db', delete=False) as f:
        tmp = Path(f.name)
    monkeypatch.setattr(mf, 'MACRO_DB', tmp)
    mf._init_macro_db()
    yield tmp
    tmp.unlink(missing_ok=True)


def _seed(db, series_id, dates_values, meta_start, meta_end):
    conn = sqlite3.connect(str(db))
    for d, v in dates_values:
        conn.execute(
            'INSERT OR REPLACE INTO fred_series (series_id, date, value, fetched_at) '
            'VALUES (?, ?, ?, ?)',
            (series_id, d, v, '2026-05-20T00:00:00'),
        )
    conn.execute(
        'INSERT OR REPLACE INTO fred_meta (series_id, last_fetched, start_date, end_date) '
        'VALUES (?, ?, ?, ?)',
        (series_id, '2026-05-20T00:00:00', meta_start, meta_end),
    )
    conn.commit()
    conn.close()


class TestKeylessCacheRead:
    def test_no_key_serves_cached_series(self, temp_macro_db, monkeypatch):
        """Regression: get_fred_series used to raise ValueError without a key.
        It must serve whatever is already cached instead."""
        monkeypatch.setattr(mf, 'FRED_API_KEY', '')
        _seed(temp_macro_db, 'DFF',
              [('2019-01-01', 2.4), ('2019-02-01', 2.5), ('2019-03-01', 2.5)],
              '2014-01-01', '2026-01-01')
        s = mf.get_fred_series('DFF', '2019-01-01', '2019-03-01')
        assert not s.empty
        assert len(s) == 3

    def test_no_key_uncached_series_returns_empty(self, temp_macro_db, monkeypatch):
        """No key + nothing cached → empty Series, not an exception."""
        monkeypatch.setattr(mf, 'FRED_API_KEY', '')
        s = mf.get_fred_series('NOTCACHED', '2019-01-01', '2019-03-01')
        assert s.empty

    def test_enrich_no_key_does_not_bail(self, temp_macro_db, monkeypatch):
        """enrich_with_macro must proceed and serve cached columns, not return
        the df unchanged the moment the key is absent."""
        monkeypatch.setattr(mf, 'FRED_API_KEY', '')
        _seed(temp_macro_db, 'DFF',
              [('2019-01-01', 2.4), ('2019-06-01', 2.5)],
              '2014-01-01', '2026-01-01')
        df = pd.DataFrame({
            'date': pd.to_datetime(['2019-03-01', '2019-04-01', '2019-05-01']),
            'open': 1.0, 'high': 1.0, 'low': 1.0, 'close': 1.0,
        })
        out = mf.enrich_with_macro(df, 'EUR_USD', '2019-01-01', '2019-06-01')
        # fed_rate (DFF) is a universal column — present and forward-filled from cache
        assert 'fed_rate' in out.columns
        assert out['fed_rate'].notna().any()


class TestIntradayEnrich:
    """Regression: enrich_with_macro used to fail on intraday timeframes with
    'cannot reindex on an axis with duplicate labels' — intraday bars repeat the
    same calendar day, and the daily macro reindex could not handle the dupes."""

    def test_h4_data_does_not_raise_and_fills_every_bar(self, temp_macro_db, monkeypatch):
        monkeypatch.setattr(mf, 'FRED_API_KEY', '')
        _seed(temp_macro_db, 'DFF',
              [('2019-01-01', 2.4), ('2019-01-15', 2.5), ('2019-02-01', 2.6)],
              '2014-01-01', '2026-01-01')
        # 6 H4 bars per day across 20 days — every day appears 6x in 'date'
        days = pd.date_range('2019-01-05', periods=20, freq='D')
        ts = pd.DatetimeIndex(
            [d + pd.Timedelta(hours=h) for d in days for h in (0, 4, 8, 12, 16, 20)]
        )
        df = pd.DataFrame({'date': ts, 'open': 1.0, 'high': 1.0, 'low': 1.0, 'close': 1.0})
        out = mf.enrich_with_macro(df, 'NZD_USD', '2019-01-01', '2019-02-01')
        assert len(out) == len(df)               # no rows dropped
        assert 'fed_rate' in out.columns
        # every intraday bar gets its calendar day's macro value
        assert out['fed_rate'].notna().all()


class TestPublicationLag:
    """Look-ahead regression guard (2026-06-09 audit): a bar must only see
    macro values already PUBLISHED at bar time. The previous same-day join
    let every bar of day D see day-D values — intraday bars effectively saw
    end-of-day data, and the entire measured edge of every H4/H1 macro
    strategy turned out to be that leak."""

    def test_every_mapped_series_has_explicit_lag(self):
        """New series must get a deliberate publication-lag entry, not the
        silent default (the cost-table lesson, applied to macro)."""
        ids = set(mf._UNIVERSAL_COLS.values())
        for m in mf._INSTRUMENT_COLS.values():
            ids |= set(m.values())
        missing = sorted(i for i in ids if i not in mf._PUBLICATION_LAG_DAYS)
        assert not missing, f"no publication lag declared for: {missing}"

    def _seed_daily(self, db, series_id):
        # value == day-of-month, strictly increasing → trivially shows which
        # observation date a bar's value came from
        vals = [(f'2019-01-{d:02d}', float(d)) for d in range(1, 31)]
        _seed(db, series_id, vals, '2014-01-01', '2026-01-01')

    def test_daily_series_visible_next_day(self, temp_macro_db, monkeypatch):
        monkeypatch.setattr(mf, 'FRED_API_KEY', '')
        self._seed_daily(temp_macro_db, 'DGS10')   # us10y, lag 1
        df = pd.DataFrame({
            'date': pd.date_range('2019-01-10', '2019-01-20', freq='D'),
            'open': 1.0, 'high': 1.0, 'low': 1.0, 'close': 1.0,
        })
        out = mf.enrich_with_macro(df, 'NZD_USD', '2019-01-01', '2019-01-31')
        for ts, v in zip(out['date'], out['us10y']):
            assert v == float(ts.day - 1), \
                f"bar {ts.date()} should see the {ts.day - 1}th's value, got {v}"

    def test_intraday_bar_never_sees_same_day_value(self, temp_macro_db, monkeypatch):
        """The exact leak shape: H4 bars of day D used to see day-D values."""
        monkeypatch.setattr(mf, 'FRED_API_KEY', '')
        self._seed_daily(temp_macro_db, 'DGS10')
        days = pd.date_range('2019-01-10', periods=10, freq='D')
        ts = pd.DatetimeIndex(
            [d + pd.Timedelta(hours=h) for d in days for h in (1, 5, 9, 13, 17, 21)]
        )
        df = pd.DataFrame({'date': ts, 'open': 1.0, 'high': 1.0, 'low': 1.0, 'close': 1.0})
        out = mf.enrich_with_macro(df, 'NZD_USD', '2019-01-01', '2019-01-31')
        for t, v in zip(out['date'], out['us10y']):
            assert v < float(t.day), f"bar {t} sees same-day/future value {v}"

    def test_weekly_dxy_lagged_seven_days(self, temp_macro_db, monkeypatch):
        monkeypatch.setattr(mf, 'FRED_API_KEY', '')
        self._seed_daily(temp_macro_db, 'DTWEXBGS')   # dxy, weekly H.10 → lag 7
        df = pd.DataFrame({
            'date': pd.date_range('2019-01-15', '2019-01-25', freq='D'),
            'open': 1.0, 'high': 1.0, 'low': 1.0, 'close': 1.0,
        })
        out = mf.enrich_with_macro(df, 'NZD_USD', '2019-01-01', '2019-01-31')
        for ts, v in zip(out['date'], out['dxy']):
            assert v == float(ts.day - 7), \
                f"dxy on {ts.date()} should be the {ts.day - 7}th's value, got {v}"

    def test_monthly_cpi_lagged_45_days(self, temp_macro_db, monkeypatch):
        monkeypatch.setattr(mf, 'FRED_API_KEY', '')
        _seed(temp_macro_db, 'CPIAUCSL',
              [('2018-12-01', 0.5), ('2019-01-01', 1.0), ('2019-02-01', 2.0)],
              '2014-01-01', '2026-01-01')
        df = pd.DataFrame({
            'date': pd.date_range('2019-02-10', '2019-02-20', freq='D'),
            'open': 1.0, 'high': 1.0, 'low': 1.0, 'close': 1.0,
        })
        out = mf.enrich_with_macro(df, 'NZD_USD', '2019-01-01', '2019-02-28')
        for ts, v in zip(out['date'], out['us_cpi']):
            if ts < pd.Timestamp('2019-02-15'):      # Jan obs publishes Jan-1+45d = Feb 15
                assert v == 0.5, f"{ts.date()}: December CPI (0.5) expected, got {v}"
            else:
                assert v == 1.0, f"{ts.date()}: January CPI (1.0) expected, got {v}"
