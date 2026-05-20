"""
Macro Data Fetcher: Downloads and caches FRED macroeconomic series.
Used by supplementary_data.py when archetype='macro'.

Required env var: FRED_API_KEY (free key at fred.stlouisfed.org/docs/api/api_key.html)

Supported columns per instrument:
  Rates    : fed_rate, ecb_rate, boe_rate, boj_rate, rba_rate
  Yields   : us10y, eu10y, uk10y, jp10y, au10y
  Real     : us_real_yield
  Inflation: us_cpi, eu_cpi, uk_cpi, jp_cpi, au_cpi, ch_cpi
  FX index : dxy

Monthly series (CPI, some yields, CB rates) are forward-filled to daily.
"""

import os
import sqlite3
import time
import numpy as np
import pandas as pd
import requests
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional, Dict

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

FRED_API_KEY = os.getenv('FRED_API_KEY', '')
FRED_BASE    = 'https://api.stlouisfed.org/fred/series/observations'
MACRO_DB     = Path(__file__).parent / 'macro_data.db'

CACHE_MAX_AGE_DAYS = 7   # re-fetch if cached data is older than this

# ---------------------------------------------------------------------------
# Instrument → { column_name: FRED_series_id }
# ---------------------------------------------------------------------------

_INSTRUMENT_COLS: Dict[str, Dict[str, str]] = {
    'EUR_USD': {
        'fed_rate':      'DFF',
        'ecb_rate':      'ECBDFR',
        'us10y':         'DGS10',
        'eu10y':         'IRLTLT01EZM156N',
        'us_real_yield': 'DFII10',
        'us_cpi':        'CPIAUCSL',
        'eu_cpi':        'CP0000EZ19M086NEST',
    },
    'GBP_USD': {
        'fed_rate':      'DFF',
        'boe_rate':      'BOERUKM',
        'us10y':         'DGS10',
        'uk10y':         'IRLTLT01GBM156N',
        'us_real_yield': 'DFII10',
        'us_cpi':        'CPIAUCSL',
        'uk_cpi':        'GBRCPIALLMINMEI',
    },
    'USD_JPY': {
        'fed_rate':      'DFF',
        'boj_rate':      'IRSTJPNM193N',
        'us10y':         'DGS10',
        'jp10y':         'IRLTLT01JPM156N',
        'us_real_yield': 'DFII10',
        'us_cpi':        'CPIAUCSL',
        'jp_cpi':        'JPNCPIALLMINMEI',
    },
    'USD_CHF': {
        'fed_rate':      'DFF',
        'us10y':         'DGS10',
        'us_real_yield': 'DFII10',
        'us_cpi':        'CPIAUCSL',
        'ch_cpi':        'CHECPIALLMINMEI',
    },
    'AUD_USD': {
        'fed_rate':      'DFF',
        'rba_rate':      'IRSTCB01AUM156N',
        'us10y':         'DGS10',
        'au10y':         'IRLTLT01AUM156N',
        'us_real_yield': 'DFII10',
        'us_cpi':        'CPIAUCSL',
        'au_cpi':        'AUSCPIALLMINMEI',
    },
    'NZD_USD': {
        'fed_rate':      'DFF',
        'us10y':         'DGS10',
        'us_real_yield': 'DFII10',
        'us_cpi':        'CPIAUCSL',
    },
    'EUR_GBP': {
        'ecb_rate':      'ECBDFR',
        'boe_rate':      'BOERUKM',
        'eu10y':         'IRLTLT01EZM156N',
        'uk10y':         'IRLTLT01GBM156N',
        'eu_cpi':        'CP0000EZ19M086NEST',
        'uk_cpi':        'GBRCPIALLMINMEI',
    },
    'EUR_JPY': {
        'ecb_rate':      'ECBDFR',
        'boj_rate':      'IRSTJPNM193N',
        'eu10y':         'IRLTLT01EZM156N',
        'jp10y':         'IRLTLT01JPM156N',
        'eu_cpi':        'CP0000EZ19M086NEST',
        'jp_cpi':        'JPNCPIALLMINMEI',
    },
    'GBP_JPY': {
        'boe_rate':      'BOERUKM',
        'boj_rate':      'IRSTJPNM193N',
        'uk10y':         'IRLTLT01GBM156N',
        'jp10y':         'IRLTLT01JPM156N',
        'uk_cpi':        'GBRCPIALLMINMEI',
        'jp_cpi':        'JPNCPIALLMINMEI',
    },
    'XAU_USD': {
        'fed_rate':      'DFF',
        'us10y':         'DGS10',
        'us_real_yield': 'DFII10',
        'us_cpi':        'CPIAUCSL',
        'dxy':           'DTWEXBGS',
    },
    'XAG_USD': {
        'fed_rate':      'DFF',
        'us10y':         'DGS10',
        'us_real_yield': 'DFII10',
        'us_cpi':        'CPIAUCSL',
    },
    'WTICO_USD': {
        'fed_rate':      'DFF',
        'us10y':         'DGS10',
        'us_cpi':        'CPIAUCSL',
    },
    'BCO_USD': {
        'fed_rate':      'DFF',
        'us10y':         'DGS10',
        'us_cpi':        'CPIAUCSL',
    },
    'BTC_USD': {
        'fed_rate':      'DFF',
        'us10y':         'DGS10',
        'us_real_yield': 'DFII10',
        'us_cpi':        'CPIAUCSL',
    },
    'ETH_USD': {
        'fed_rate':      'DFF',
        'us10y':         'DGS10',
        'us_real_yield': 'DFII10',
        'us_cpi':        'CPIAUCSL',
    },
}

# Fallback for instruments not in the map above
_UNIVERSAL_COLS: Dict[str, str] = {
    'fed_rate':      'DFF',
    'us10y':         'DGS10',
    'us_real_yield': 'DFII10',
    'us_cpi':        'CPIAUCSL',
}

# All column names exposed by this module (used for validation whitelist)
ALL_MACRO_COLS = frozenset(
    col
    for cols in _INSTRUMENT_COLS.values()
    for col in cols
) | frozenset(_UNIVERSAL_COLS)


# ---------------------------------------------------------------------------
# DB helpers
# ---------------------------------------------------------------------------

def _init_macro_db():
    conn = sqlite3.connect(str(MACRO_DB))
    conn.execute('''
        CREATE TABLE IF NOT EXISTS fred_series (
            series_id  TEXT NOT NULL,
            date       TEXT NOT NULL,
            value      REAL,
            fetched_at TEXT NOT NULL,
            PRIMARY KEY (series_id, date)
        )
    ''')
    conn.execute('''
        CREATE TABLE IF NOT EXISTS fred_meta (
            series_id    TEXT PRIMARY KEY,
            last_fetched TEXT NOT NULL,
            start_date   TEXT,
            end_date     TEXT
        )
    ''')
    conn.commit()
    conn.close()


def _needs_fetch(series_id: str, start_date: str, end_date: str) -> bool:
    conn = sqlite3.connect(str(MACRO_DB))
    row = conn.execute(
        'SELECT last_fetched, start_date, end_date FROM fred_meta WHERE series_id=?',
        (series_id,)
    ).fetchone()
    conn.close()

    if not row:
        return True
    last_fetched  = datetime.fromisoformat(row[0])
    cached_start  = row[1] or ''
    cached_end    = row[2] or ''
    if (datetime.utcnow() - last_fetched).days > CACHE_MAX_AGE_DAYS:
        return True
    if start_date < cached_start or end_date > cached_end:
        return True
    return False


def _fetch_fred_api(series_id: str, start_date: str, end_date: str) -> pd.Series:
    """Call FRED REST API. Returns a Series indexed by Timestamp, values float/NaN."""
    params = {
        'series_id':         series_id,
        'observation_start': start_date,
        'observation_end':   end_date,
        'api_key':           FRED_API_KEY,
        'file_type':         'json',
    }
    resp = requests.get(FRED_BASE, params=params, timeout=30)
    resp.raise_for_status()
    observations = resp.json().get('observations', [])

    dates  = []
    values = []
    for obs in observations:
        dates.append(pd.to_datetime(obs['date']))
        v = obs['value']
        values.append(float(v) if v != '.' else np.nan)

    if not dates:
        return pd.Series(dtype=float, name=series_id)
    return pd.Series(values, index=pd.DatetimeIndex(dates), name=series_id)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def get_fred_series(
    series_id:     str,
    start_date:    str,
    end_date:      str,
    force_refresh: bool = False,
) -> pd.Series:
    """
    Fetch a FRED series, using the local SQLite cache when fresh.

    Returns a pd.Series with DatetimeIndex (native FRED frequency).
    Caller is responsible for resampling/forward-filling to target frequency.
    """
    _init_macro_db()

    # Fetch 60 extra days so forward-fill has context before start_date
    expanded_start = (
        datetime.strptime(start_date, '%Y-%m-%d') - timedelta(days=60)
    ).strftime('%Y-%m-%d')

    # Fetch fresh only when a key is present AND the cache is stale/incomplete.
    # Without a key we still serve whatever is already cached — macro_data.db
    # is a cache, and a missing key must not block reads of data already in it.
    if FRED_API_KEY and (force_refresh or _needs_fetch(series_id, expanded_start, end_date)):
        print(f'  [FRED] Fetching {series_id} ({expanded_start}→{end_date})...', flush=True)
        try:
            series = _fetch_fred_api(series_id, expanded_start, end_date)
        except Exception as e:
            print(f'  [FRED] Warning: {series_id} fetch failed: {e}', flush=True)
            series = pd.Series(dtype=float, name=series_id)

        if not series.empty:
            conn  = sqlite3.connect(str(MACRO_DB))
            now   = datetime.utcnow().isoformat()
            for dt, val in series.items():
                conn.execute(
                    'INSERT OR REPLACE INTO fred_series '
                    '(series_id, date, value, fetched_at) VALUES (?,?,?,?)',
                    (series_id, dt.strftime('%Y-%m-%d'),
                     None if (val is None or np.isnan(val)) else val, now)
                )
            conn.execute(
                'INSERT OR REPLACE INTO fred_meta '
                '(series_id, last_fetched, start_date, end_date) VALUES (?,?,?,?)',
                (series_id, now, expanded_start, end_date)
            )
            conn.commit()
            conn.close()

        time.sleep(0.25)   # FRED rate-limit courtesy pause

    # Load from cache
    conn = sqlite3.connect(str(MACRO_DB))
    rows = conn.execute(
        'SELECT date, value FROM fred_series '
        'WHERE series_id=? AND date >= ? AND date <= ? ORDER BY date',
        (series_id, expanded_start, end_date)
    ).fetchall()
    conn.close()

    if not rows:
        return pd.Series(dtype=float, name=series_id)

    dates  = pd.to_datetime([r[0] for r in rows])
    values = [r[1] for r in rows]
    return pd.Series(values, index=pd.DatetimeIndex(dates), name=series_id)


def enrich_with_macro(
    df:         pd.DataFrame,
    instrument: str,
    start_date: Optional[str] = None,
    end_date:   Optional[str] = None,
) -> pd.DataFrame:
    """
    Inject macro columns into an OHLC DataFrame for the given instrument.

    Monthly series (CPI, some CB rates, non-US yields) are forward-filled to
    match the bar frequency. Columns with no FRED data are left as NaN.

    Returns a copy of df with macro columns appended. Macro values are served
    from the macro_data.db cache; a missing FRED_API_KEY only prevents fetching
    series the cache doesn't already cover (those columns come back NaN).
    """
    if not FRED_API_KEY:
        print(
            '  [Macro] FRED_API_KEY not set — serving cached macro data only '
            '(uncached series will be NaN).',
            flush=True
        )

    df = df.copy()

    # Resolve date range from df if not supplied
    if 'date' in df.columns:
        df['date'] = pd.to_datetime(df['date'])
        start_date = start_date or df['date'].min().strftime('%Y-%m-%d')
        end_date   = end_date   or df['date'].max().strftime('%Y-%m-%d')

    # Merge instrument-specific cols with universal fallback
    col_map = {**_UNIVERSAL_COLS, **_INSTRUMENT_COLS.get(instrument, {})}

    ohlc_dates = pd.DatetimeIndex(df['date'].values)
    added = []

    for col_name, series_id in col_map.items():
        try:
            raw = get_fred_series(series_id, start_date, end_date)
            if raw.empty:
                df[col_name] = np.nan
                continue
            # Reindex to all dates (OHLC + FRED), forward-fill gaps, then select OHLC dates
            combined_idx  = ohlc_dates.union(raw.index).sort_values()
            series_daily  = raw.reindex(combined_idx).ffill().reindex(ohlc_dates)
            df[col_name]  = series_daily.values
            added.append(col_name)
        except Exception as e:
            print(f'  [Macro] {col_name} ({series_id}) failed: {e}', flush=True)
            df[col_name] = np.nan

    if added:
        print(f'  [Macro] Injected {len(added)} columns: {added}', flush=True)

    return df


def list_available_columns(instrument: str) -> Dict[str, str]:
    """Return the {col_name: fred_series_id} map for an instrument."""
    return {**_UNIVERSAL_COLS, **_INSTRUMENT_COLS.get(instrument, {})}
