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
