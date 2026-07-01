"""FRED economic-event calendar → event-timing columns for the event-driven family.

Replaces the dead OANDA ForexLabs calendar (Cloudflare-blocked since 2026-05-25).
Uses the FRED releases API (FRED_API_KEY, already set for macro) to fetch the
RELEASE DATES of a curated set of high-impact US releases, caches them in
macro_data.db, and injects per-bar timing columns.

NO LOOK-AHEAD: an economic release schedule is published in ADVANCE, so "days to
the next CPI" is legitimately known at bar time. Only a release's *value/surprise*
is unknown until it prints (surprise columns are a deliberate v2 — timing only here).
"""
import os
import sqlite3
import time
from pathlib import Path
from typing import Dict, List

import numpy as np
import pandas as pd
import requests

import env_loader  # noqa: F401 — loads .env (FRED_API_KEY etc.)

FRED_API_KEY = os.getenv('FRED_API_KEY', '')
_DB = Path(__file__).parent / 'macro_data.db'
_RELEASES_URL = 'https://api.stlouisfed.org/fred/release/dates'
_RELEASES_LIST_URL = 'https://api.stlouisfed.org/fred/releases'

# Curated high-impact US releases (name substrings matched against FRED release
# names). Kept small + big-mover on daily FX/commodities/indices.
HIGH_IMPACT = [
    'Consumer Price Index',        # CPI
    'Employment Situation',        # NFP + unemployment
    'Gross Domestic Product',      # GDP
    'Producer Price Index',        # PPI
    'Personal Income and Outlays', # PCE (Fed's preferred inflation gauge)
    # NOTE: 'FOMC Press Release' deliberately excluded — that FRED release bundles
    # statements + minutes + speeches (~3.5k dates), so it fires ~daily and would
    # swamp event_window. FOMC rate-decision dates are a fixed ~8/yr calendar; add
    # them from a curated list later if wanted, not from this noisy release.
]
_CAL_START = '2014-11-01'


def _resolve_release_ids() -> Dict[str, int]:
    """Map each curated release name to its FRED release_id (exact-name preferred)."""
    r = requests.get(_RELEASES_LIST_URL,
                     params={'api_key': FRED_API_KEY, 'file_type': 'json', 'limit': 1000},
                     timeout=30)
    r.raise_for_status()
    all_rel = r.json().get('releases', [])
    out = {}
    for want in HIGH_IMPACT:
        exact = [x for x in all_rel if x['name'] == want]
        cand = exact or [x for x in all_rel if want.lower() in x['name'].lower()]
        if cand:
            out[want] = cand[0]['id']
    return out


def refresh_release_dates(end_date: str = None) -> int:
    """Fetch + cache release dates for the curated releases. Returns rows stored."""
    if not FRED_API_KEY:
        print('  [events] FRED_API_KEY not set — cannot refresh release dates')
        return 0
    end_date = end_date or time.strftime('%Y-%m-%d')
    con = sqlite3.connect(str(_DB))
    con.execute('CREATE TABLE IF NOT EXISTS fred_release_dates('
                'release TEXT, date TEXT, PRIMARY KEY(release, date))')
    ids = _resolve_release_ids()
    n = 0
    for name, rid in ids.items():
        try:
            r = requests.get(_RELEASES_URL, params={
                'api_key': FRED_API_KEY, 'file_type': 'json', 'release_id': rid,
                'realtime_start': _CAL_START, 'realtime_end': end_date,
                'include_release_dates_with_no_data': 'false', 'limit': 10000,
                'sort_order': 'asc'}, timeout=30)
            r.raise_for_status()
            for d in r.json().get('release_dates', []):
                con.execute('INSERT OR IGNORE INTO fred_release_dates VALUES (?,?)',
                            (name, d['date']))
                n += 1
        except Exception as e:
            print(f'  [events] {name}: fetch failed — {str(e)[:80]}')
    con.commit()
    con.close()
    return n


def get_release_dates() -> pd.DataFrame:
    """Cached release dates as a DataFrame(release, date[datetime]). Empty if none."""
    if not _DB.exists():
        return pd.DataFrame(columns=['release', 'date'])
    con = sqlite3.connect(str(_DB))
    try:
        df = pd.read_sql_query('SELECT release, date FROM fred_release_dates', con)
    except Exception:
        return pd.DataFrame(columns=['release', 'date'])
    finally:
        con.close()
    if len(df):
        df['date'] = pd.to_datetime(df['date'])
    return df


def inject_event_columns(df: pd.DataFrame, cap: int = 60) -> pd.DataFrame:
    """Add event-TIMING columns to an OHLC df (must have a 'date' column).

    Columns (all look-ahead-safe — the release schedule is public in advance):
      - days_to_event   : calendar days to the NEXT high-impact release (capped)
      - days_since_event: calendar days since the LAST one (capped)
      - event_window    : 1 on the release day and the bar after (the reaction window)

    A missing/empty calendar leaves the columns present but neutral (cap / 0) so
    downstream `df['days_to_event']` never KeyErrors (the dead-feed lesson).
    """
    df = df.copy()
    df['days_to_event'] = cap
    df['days_since_event'] = cap
    df['event_window'] = 0
    cal = get_release_dates()
    if not len(cal) or 'date' not in df.columns:
        return df
    ev = np.sort(cal['date'].values.astype('datetime64[ns]'))
    bars = pd.to_datetime(df['date']).values.astype('datetime64[ns]')
    # next release >= bar, and last release <= bar (searchsorted on sorted events)
    nxt = np.searchsorted(ev, bars, side='left')
    prv = np.searchsorted(ev, bars, side='right') - 1
    dte = np.full(len(bars), cap, dtype=float)
    dse = np.full(len(bars), cap, dtype=float)
    has_next = nxt < len(ev)
    has_prev = prv >= 0
    dte[has_next] = (ev[nxt[has_next]] - bars[has_next]) / np.timedelta64(1, 'D')
    dse[has_prev] = (bars[has_prev] - ev[prv[has_prev]]) / np.timedelta64(1, 'D')
    df['days_to_event'] = np.clip(dte, 0, cap)
    df['days_since_event'] = np.clip(dse, 0, cap)
    df['event_window'] = ((df['days_since_event'] <= 1) | (df['days_to_event'] <= 0)).astype(int)
    return df


if __name__ == '__main__':
    # self-check: timing columns are causal (days_to_event decreases toward an
    # event, days_since_event increases after it), and never KeyError on empty.
    ev = pd.to_datetime(['2020-01-15', '2020-02-15'])
    import sqlite3 as s
    # inject on a synthetic daily frame using a monkeypatched calendar
    days = pd.date_range('2020-01-01', '2020-02-28', freq='D')
    frame = pd.DataFrame({'date': days, 'close': 1.0})
    globals()['get_release_dates'] = lambda: pd.DataFrame({'release': ['CPI', 'CPI'], 'date': ev})
    out = inject_event_columns(frame)
    r = out.set_index('date')
    assert r.loc['2020-01-14', 'days_to_event'] == 1, 'day before event -> 1'
    assert r.loc['2020-01-15', 'event_window'] == 1, 'event day in window'
    assert r.loc['2020-01-16', 'days_since_event'] == 1, 'day after -> since=1'
    assert (out[['days_to_event', 'days_since_event', 'event_window']].notna().all().all())
    print('ok')
