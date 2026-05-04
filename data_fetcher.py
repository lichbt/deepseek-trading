"""Data Fetcher with retry logic."""
import time

import os
from datetime import datetime, timedelta
from typing import Optional
import pandas as pd
import requests


OANDA_RETRIES = 3
OANDA_RETRY_DELAY = 1.0  # seconds


# Oanda API configuration
OANDA_ACCOUNT_ID = os.getenv('OANDA_ACCOUNT_ID', '')
OANDA_API_TOKEN = os.getenv('OANDA_API_TOKEN', '')
OANDA_BASE_URL = 'https://api-fxpractice.oanda.com'  # Practice environment


def get_candles(
    instrument: str,
    granularity: str = 'D',
    start: str = None,
    end: str = None,
    count: Optional[int] = None
) -> pd.DataFrame:
    """
    Fetch historical candles from Oanda v20 API.
    
    Args:
        instrument: e.g., 'EUR_USD', 'SPX500', 'XAU_USD'
        granularity: 'D' (daily), 'H1', 'M15', etc.
        start: ISO datetime string (e.g., '2015-01-01T00:00:00Z')
        end: ISO datetime string
        count: Optional max candles per request (default 5000)
    
    Returns:
        pd.DataFrame with columns: [date, open, high, low, close]
    
    Raises:
        Exception if API credentials missing or request fails
    """
    if not OANDA_ACCOUNT_ID or not OANDA_API_TOKEN:
        raise ValueError('OANDA_ACCOUNT_ID and OANDA_API_TOKEN env vars required')
    
    headers = {
        'Authorization': f'Bearer {OANDA_API_TOKEN}',
        'Content-Type': 'application/json',
    }
    
    if count is None:
        count = 5000

    all_candles = []
    current_from = start
    is_first = True
    
    while True:
        params = {
            'granularity': granularity,
            'price': 'M',
        }

        if current_from:
            params['from'] = current_from

        if end and is_first:
            params['to'] = end

        if not is_first or not end:
            params['count'] = count

        url = f'{OANDA_BASE_URL}/v3/instruments/{instrument}/candles'
        
        last_err = None
        for attempt in range(OANDA_RETRIES):
            try:
                response = requests.get(url, headers=headers, params=params, timeout=10)
                response.raise_for_status()
                break
            except Exception as e:
                last_err = e
                if attempt < OANDA_RETRIES - 1:
                    time.sleep(OANDA_RETRY_DELAY * (attempt + 1))
        else:
            raise Exception(f'Oanda API error: {last_err}')
        
        is_first = False
        
        data = response.json()
        candles = data.get('candles', [])
        
        if not candles:
            break
        
        # Extract mid prices (bid/ask average)
        for candle in candles:
            if candle.get('complete', True):  # Only complete candles
                all_candles.append({
                    'date': candle['time'],
                    'open': float(candle['mid']['o']),
                    'high': float(candle['mid']['h']),
                    'low': float(candle['mid']['l']),
                    'close': float(candle['mid']['c']),
                })
        
        # Check if we got a full page
        if len(candles) < count:
            break
        
        # Set next batch start to last candle's time
        last_time = candles[-1]['time']
        current_from = last_time
        
        # Safeguard: stop if we exceed end date
        if end and last_time >= end:
            break
    
    if not all_candles:
        # Return empty dataframe with correct columns
        return pd.DataFrame(columns=['date', 'open', 'high', 'low', 'close'])
    
    df = pd.DataFrame(all_candles)
    
    # Parse date to datetime
    df['date'] = pd.to_datetime(df['date'])
    
    # Sort by date ascending
    df = df.sort_values('date').reset_index(drop=True)
    
    return df


def get_candles_date_range(
    instrument: str,
    start_date: str,
    end_date: str,
    granularity: str = 'D'
) -> pd.DataFrame:
    """
    Convenience wrapper to fetch candles by date strings (YYYY-MM-DD).
    
    Args:
        instrument: e.g., 'EUR_USD'
        start_date: 'YYYY-MM-DD'
        end_date: 'YYYY-MM-DD'
        granularity: 'D'
    
    Returns:
        pd.DataFrame
    """
    # Parse dates and convert to ISO format with time component
    start_dt = datetime.strptime(start_date, '%Y-%m-%d')
    end_dt = datetime.strptime(end_date, '%Y-%m-%d')
    
    # For daily candles, use UTC midnight
    start_iso = start_dt.isoformat() + 'Z'
    end_iso = (end_dt + timedelta(days=1)).isoformat() + 'Z'
    
    return get_candles(
        instrument=instrument,
        granularity=granularity,
        start=start_iso,
        end=end_iso
    )
