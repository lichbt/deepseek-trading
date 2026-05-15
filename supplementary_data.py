"""
Supplementary Data Module: Economic calendar, session labels, and pair trading data.
Used by validator.py when archetype is set to 'news', 'session', or 'pair'.

No new API keys needed — uses existing OANDA credentials.
"""

import os
import time
from datetime import datetime, timedelta, timezone
from typing import Optional, List, Dict, Any
from pathlib import Path

import pandas as pd
import requests


# ============================================================================
# CONFIGURATION
# ============================================================================

OANDA_ACCOUNT_ID = os.getenv('OANDA_ACCOUNT_ID', '')
OANDA_API_TOKEN = os.getenv('OANDA_API_TOKEN', '')
OANDA_BASE_URL = 'https://api-fxpractice.oanda.com'

# Session definitions (UTC hours)
SESSION_HOURS = {
    'Asian': (0, 9),      # 00:00 - 09:00 UTC
    'London': (8, 17),    # 08:00 - 17:00 UTC (overlaps with Asian)
    'New_York': (13, 22), # 13:00 - 22:00 UTC (overlaps with London)
    # After New York close but before Asian open = closed
    # Full overlap: London + New York = 13:00-17:00 UTC
}


# ============================================================================
# ECONOMIC CALENDAR (OANDA ForexLabs)
# ============================================================================

def get_economic_calendar(
    instrument: str,
    start_date: Optional[str] = None,
    end_date: Optional[str] = None,
    period_seconds: int = 2592000  # 30 days default
) -> pd.DataFrame:
    """
    Fetch economic calendar from OANDA ForexLabs.

    Args:
        instrument: e.g., 'EUR_USD' - filters events to those affecting this pair
        start_date: ISO datetime string (optional)
        end_date: ISO datetime string (optional)
        period_seconds: seconds of history to fetch (default 30 days)

    Returns:
        DataFrame with columns:
        - timestamp: Unix epoch (datetime)
        - currency: EUR, USD, etc.
        - impact: 'low', 'medium', 'high'
        - actual: float or None
        - forecast: float or None
        - previous: float or None
    """
    if not OANDA_ACCOUNT_ID or not OANDA_API_TOKEN:
        print("  Warning: OANDA credentials not set, skipping calendar")
        return pd.DataFrame()

    # Map instrument to currencies
    currency_map = {
        'EUR_USD': ['EUR', 'USD'],
        'GBP_USD': ['GBP', 'USD'],
        'USD_JPY': ['USD', 'JPY'],
        'USD_CHF': ['USD', 'CHF'],
        'AUD_USD': ['AUD', 'USD'],
        'USD_CAD': ['USD', 'CAD'],
        'NZD_USD': ['NZD', 'USD'],
        'XAU_USD': ['USD', 'XAU'],  # Gold driven by USD
        'BCO_USD': ['USD', 'GBP'],  # Brent crude
        'WTICO_USD': ['USD'],
        'CORN_USD': ['USD'],
        'NATGAS_USD': ['USD'],
    }
    currencies = currency_map.get(instrument, ['USD'])

    # Build request params
    params = {
        'instrument': instrument,
        'period': period_seconds,
    }
    if start_date:
        params['from'] = start_date
    if end_date:
        params['to'] = end_date

    headers = {
        'Authorization': f'Bearer {OANDA_API_TOKEN}',
        'Content-Type': 'application/json',
    }

    url = f'{OANDA_BASE_URL}/labs/v1/calendar'

    try:
        response = requests.get(url, headers=headers, params=params, timeout=15)
        response.raise_for_status()
        data = response.json()

        events = data.get('calendar', [])
        if not events:
            return pd.DataFrame()

        # Parse and filter events
        parsed = []
        for event in events:
            currency = event.get('currency', '')
            # Filter to relevant currencies
            if currency not in currencies:
                continue

            # Parse timestamp
            timestamp = event.get('timestamp') or event.get('date')
            if timestamp:
                try:
                    # Handle both Unix timestamp and ISO string
                    if isinstance(timestamp, (int, float)):
                        dt = datetime.fromtimestamp(timestamp, tz=timezone.utc)
                    else:
                        dt = pd.to_datetime(timestamp)
                except Exception:
                    dt = None
            else:
                dt = None

            # Parse impact
            impact = event.get('impact', 'low').lower()
            if impact not in ['low', 'medium', 'high']:
                impact = 'low'

            parsed.append({
                'timestamp': dt,
                'currency': currency,
                'event_title': event.get('title', ''),
                'impact': impact,
                'actual': event.get('actual'),
                'forecast': event.get('forecast'),
                'previous': event.get('previous'),
            })

        if not parsed:
            return pd.DataFrame()

        df = pd.DataFrame(parsed)
        df = df.sort_values('timestamp').reset_index(drop=True)
        return df

    except Exception as e:
        print(f"  Warning: Calendar fetch failed: {e}")
        return pd.DataFrame()


def merge_calendar_into_data(df: pd.DataFrame, calendar_df: pd.DataFrame) -> pd.DataFrame:
    """
    Merge economic calendar events into OHLC dataframe as extra columns.

    For each bar, we add columns:
    - event_near: 1 if a high-impact event is within N bars ahead/behind
    - event_impact: 'high'/'medium'/'low'/'none' based on next major event

    Also adds cumulative surprise columns (actual - forecast) for recent events.
    """
    if calendar_df.empty:
        df['event_impact'] = 'none'
        return df

    # Ensure date column is datetime
    if 'date' in df.columns:
        df_dates = df['date']
    else:
        return df

    # Initialize columns
    df = df.copy()
    df['event_impact'] = 'none'
    df['event_surprise'] = 0.0

    # Find high-impact events
    high_impact = calendar_df[calendar_df['impact'] == 'high'].copy()
    if high_impact.empty:
        return df

    # Build a lookup: for each bar, find the latest high-impact event within past 2 bars.
    # Use vectorized merge instead of row-by-row Python loop.
    df_idx = df.copy().reset_index(drop=True)
    df_idx['_bar_time'] = df_dates.reset_index(drop=True)

    # Explode each bar to the last 2 indices for a windowed lookup
    df_idx['_window'] = df_idx.index.map(lambda i: list(range(max(0, i - 2), i + 1)))
    event_times = high_impact.set_index('timestamp')['impact']
    event_surprises = high_impact.set_index('timestamp')['actual']

    # Vectorized: for each bar, find latest event in window
    impact_vals = []
    surprise_vals = []
    bar_times_arr = df_idx['_bar_time'].values
    for i, bar_time in enumerate(bar_times_arr):
        if pd.isna(bar_time):
            impact_vals.append('none')
            surprise_vals.append(0.0)
            continue
        window_mask = high_impact['timestamp'].between(
            bar_times_arr[max(0, i - 2)], bar_time, inclusive='right'
        )
        window_events = high_impact[window_mask]
        if window_events.empty:
            impact_vals.append('none')
            surprise_vals.append(0.0)
        else:
            ev = window_events.iloc[-1]
            impact_vals.append(ev['impact'])
            if ev['actual'] is not None and ev['forecast'] is not None:
                try:
                    surprise_vals.append(float(ev['actual']) - float(ev['forecast']))
                except (ValueError, TypeError):
                    surprise_vals.append(0.0)
            else:
                surprise_vals.append(0.0)

    df['event_impact'] = impact_vals
    df['event_surprise'] = surprise_vals
    return df

    return df


# ============================================================================
# SESSION LABELS
# ============================================================================

def get_session_label(timestamp: datetime) -> str:
    """
    Return session name for a given UTC timestamp.

    Returns: 'Asian', 'London', 'New_York', 'Overlap', or 'Closed'
    """
    hour = timestamp.hour

    # Check overlap first (13:00-17:00 UTC)
    if 13 <= hour < 17:
        return 'Overlap'

    # London-only session: 08:00-13:00 UTC
    if 8 <= hour < 13:
        return 'London'

    # New York-only session: 17:00-22:00 UTC
    if 17 <= hour < 22:
        return 'New_York'

    # Asian session: 00:00-09:00 UTC
    if 0 <= hour < 9:
        return 'Asian'

    # Everything else: closed (22:00-00:00 UTC)
    return 'Closed'


def label_dataframe_sessions(df: pd.DataFrame) -> pd.DataFrame:
    """
    Add 'session' column to OHLC dataframe based on timestamp.

    Args:
        df: DataFrame with 'date' column (datetime)

    Returns:
        DataFrame with added 'session' column
    """
    df = df.copy()

    if 'date' not in df.columns:
        raise ValueError("DataFrame must have 'date' column")

    # Ensure date column is datetime
    df['date'] = pd.to_datetime(df['date'])

    # Apply session labeling
    df['session'] = df['date'].apply(get_session_label)

    return df


# ============================================================================
# PAIR TRADING DATA
# ============================================================================

def get_pair_spread(
    instrument1: str,
    instrument2: str,
    start_date: str,
    end_date: str,
    granularity: str = 'D',
    method: str = 'ratio'  # 'ratio', 'spread', 'log_ratio'
) -> pd.DataFrame:
    """
    Fetch two instruments and compute a spread/relation.

    Args:
        instrument1: Primary instrument (e.g., 'EUR_USD')
        instrument2: Second instrument (e.g., 'GBP_USD')
        start_date: 'YYYY-MM-DD'
        end_date: 'YYYY-MM-DD'
        granularity: 'D', 'H4', 'H1', etc.
        method: 'ratio' (leg1/leg2), 'spread' (leg1 - leg2 normalized), 'log_ratio'

    Returns:
        DataFrame with columns: date, open, high, low, close,
        and also close_leg2, spread, ratio
    """
    # Import here to avoid circular dependency
    from data_fetcher import get_candles_date_range

    # Fetch both legs
    df1 = get_candles_date_range(instrument1, start_date, end_date, granularity)
    df2 = get_candles_date_range(instrument2, start_date, end_date, granularity)

    if df1.empty or df2.empty:
        raise ValueError(f"Missing data for pair: {instrument1} or {instrument2}")

    # Merge on date
    df1 = df1.rename(columns={
        'open': 'open_leg1',
        'high': 'high_leg1',
        'low': 'low_leg1',
        'close': 'close_leg1',
    })
    df2 = df2.rename(columns={
        'open': 'open_leg2',
        'high': 'high_leg2',
        'low': 'low_leg2',
        'close': 'close_leg2',
    })

    # Merge
    df = pd.merge(df1, df2[['date', 'open_leg2', 'high_leg2', 'low_leg2', 'close_leg2']],
                  on='date', how='inner')

    if df.empty:
        raise ValueError(f"No overlapping data for {instrument1} and {instrument2}")

    # Compute spread based on method
    if method == 'ratio':
        df['spread'] = df['close_leg1'] / df['close_leg2']
    elif method == 'log_ratio':
        import numpy as np
        df['spread'] = np.log(df['close_leg1'] / df['close_leg2'])
    else:  # spread (normalized)
        # Normalize by leg2 to make comparable
        df['spread'] = (df['close_leg1'] - df['close_leg2']) / df['close_leg2']

    # Rename primary close to 'close' for consistent strategy interface
    df['close'] = df['close_leg1']
    df['open'] = df['open_leg1']
    df['high'] = df['high_leg1']
    df['low'] = df['low_leg1']

    # Reorder columns
    df = df[['date', 'open', 'high', 'low', 'close', 'close_leg2', 'spread']]

    return df.reset_index(drop=True)


# ============================================================================
# MAIN INJECTION FUNCTION
# ============================================================================

def inject_supplementary_data(
    df: pd.DataFrame,
    archetype: str,
    instrument: str,
    instrument2: Optional[str] = None,
    start_date: Optional[str] = None,
    end_date: Optional[str] = None,
    granularity: str = 'D',
) -> pd.DataFrame:
    """
    Main entry point: inject supplementary data based on archetype.

    Args:
        df: OHLC DataFrame
        archetype: 'news', 'session', 'pair', 'standard'
        instrument: primary instrument
        instrument2: second instrument for pair trading
        start_date: date range start
        end_date: date range end
        granularity: candle granularity

    Returns:
        DataFrame with supplementary columns added
    """
    if archetype == 'standard' or archetype is None:
        return df

    if archetype == 'news':
        calendar_df = get_economic_calendar(instrument)
        return merge_calendar_into_data(df, calendar_df)

    if archetype == 'session':
        return label_dataframe_sessions(df)

    if archetype == 'pair':
        if not instrument2:
            raise ValueError("Pair archetype requires instrument2 in candidate")
        return get_pair_spread(instrument, instrument2, start_date, end_date, granularity)

    if archetype == 'macro':
        from macro_fetcher import enrich_with_macro
        return enrich_with_macro(df, instrument, start_date, end_date)

    # Unknown archetype — return unchanged
    print(f"  Warning: Unknown archetype '{archetype}', skipping")
    return df