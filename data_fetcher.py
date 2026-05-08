"""Data Fetcher with retry logic and local caching."""
import hashlib
import json
import time
import os
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional
import pandas as pd
import requests


OANDA_RETRIES = 3
OANDA_RETRY_DELAY = 1.0  # seconds
OANDA_MAX_CANDLES = 5000  # API limit

# Cache configuration
OANDA_CACHE_DIR = Path(__file__).parent / '.cache' / 'oanda'
OANDA_CACHE_TTL_HOURS = int(os.getenv('OANDA_CACHE_TTL_HOURS', '24'))  # default 24h

# Intraday chunk size (in days)
INTRADAY_CHUNK_DAYS = {  # granularity -> max days per request
    'H4': 180,   # ~720 candles (6 months)
    'H1': 90,    # ~540 candles (3 months)
    'M30': 60,    # ~960 candles (2 months)
    'M15': 30,    # ~960 candles (1 month)
    'M5': 14,     # ~1344 candles (2 weeks)
}


# Oanda API configuration
OANDA_ACCOUNT_ID = os.getenv('OANDA_ACCOUNT_ID', '')
OANDA_API_TOKEN = os.getenv('OANDA_API_TOKEN', '')
OANDA_BASE_URL = 'https://api-fxpractice.oanda.com'  # Practice environment


def _cache_key(*parts: str) -> str:
    digest = hashlib.sha256('::'.join(parts).encode()).hexdigest()
    return digest


def _cache_path(*parts: str) -> Path:
    OANDA_CACHE_DIR.mkdir(parents=True, exist_ok=True)
    return OANDA_CACHE_DIR / f'{_cache_key(*parts)}.json'


def _load_cached_dataframe(*parts: str) -> Optional[pd.DataFrame]:
    path = _cache_path(*parts)
    if not path.exists():
        return None
    age_seconds = time.time() - path.stat().st_mtime
    if age_seconds > OANDA_CACHE_TTL_HOURS * 3600:
        return None
    try:
        payload = json.loads(path.read_text())
        df = pd.DataFrame(payload['rows'])
        if 'date' in df.columns:
            df['date'] = pd.to_datetime(df['date'])
        return df
    except Exception:
        return None


def _store_cached_dataframe(df: pd.DataFrame, *parts: str) -> None:
    path = _cache_path(*parts)
    payload = {'rows': df.to_dict(orient='records')}
    path.write_text(json.dumps(payload, default=str))


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
    """
    cached = _load_cached_dataframe('mid', instrument, granularity, start_date, end_date)
    if cached is not None:
        return cached

    start_dt = datetime.strptime(start_date, '%Y-%m-%d')
    end_dt = datetime.strptime(end_date, '%Y-%m-%d')

    if granularity in INTRADAY_CHUNK_DAYS:
        max_days = INTRADAY_CHUNK_DAYS.get(granularity, 60)
        all_chunks = []
        current_start = start_dt
        while current_start < end_dt:
            chunk_end = current_start + timedelta(days=max_days)
            if chunk_end > end_dt:
                chunk_end = end_dt
            chunk_df = get_candles(
                instrument=instrument,
                granularity=granularity,
                start=current_start.isoformat() + 'Z',
                end=chunk_end.isoformat() + 'Z'
            )
            all_chunks.append(chunk_df)
            current_start = chunk_end
        if all_chunks:
            df = pd.concat(all_chunks, ignore_index=True)
        else:
            df = pd.DataFrame(columns=['date', 'open', 'high', 'low', 'close'])
    else:
        start_iso = start_dt.isoformat() + 'Z'
        end_iso = (end_dt + timedelta(days=1)).isoformat() + 'Z'
        df = get_candles(
            instrument=instrument,
            granularity=granularity,
            start=start_iso,
            end=end_iso
        )

    _store_cached_dataframe(df, 'mid', instrument, granularity, start_date, end_date)
    return df


def get_live_spreads(instruments: list) -> dict:
    """
    Fetch real-time spreads from OANDA pricing API.
    
    Args:
        instruments: List of instrument names, e.g., ['EUR_USD', 'XAU_USD']
        
    Returns:
        dict: Mapping of instrument to spread in pips.
    """
    if not OANDA_ACCOUNT_ID or not OANDA_API_TOKEN:
        print("Warning: OANDA credentials not set, cannot fetch live spreads")
        return {}

    url = f"{OANDA_BASE_URL}/v3/accounts/{OANDA_ACCOUNT_ID}/pricing"
    params = {'instruments': ','.join(instruments)}
    headers = {'Authorization': f'Bearer {OANDA_API_TOKEN}'}
    
    try:
        resp = requests.get(url, headers=headers, params=params, timeout=10)
        resp.raise_for_status()
        data = resp.json()
        
        spreads = {}
        for p in data.get('prices', []):
            instrument = p.get('instrument')
            bids = p.get('bids', [{}])
            asks = p.get('asks', [{}])
            bid = bids[0].get('price') if bids else None
            ask = asks[0].get('price') if asks else None
            
            if bid and ask:
                # OANDA prices are strings
                # 1 pip = 0.0001 for most, but 0.01 for JPY pairs
                # The pipeline_utils.get_pip_value(instrument) could be used,
                # but to avoid circular imports, we'll calculate it based on the price format
                
                bid_f = float(bid)
                ask_f = float(ask)
                raw_spread = ask_f - bid_f
                
                # Determine pip multiplier based on instrument
                if 'JPY' in instrument or instrument in ['XAU_USD', 'SPX500', 'US30']:
                    # For JPY pairs and indices/gold, pip is usually 2nd decimal place
                    # Except XAU_USD where $0.01 price move = 1 pip? Actually our pipeline says 1 pip = 0.01 for JPY.
                    if 'JPY' in instrument:
                        pip_val = 0.01
                    else:
                        pip_val = 0.01 # simplified
                else:
                    pip_val = 0.0001
                    
                # To be precise, we should just let pipeline_utils handle the multiplier.
                # Let's return raw spread, and let pipeline_utils convert to pips using its get_pip_value() logic
                spreads[instrument] = raw_spread
                
        return spreads
    except Exception as e:
        print(f"Warning: Failed to fetch live spreads: {e}")
        return {}



def get_candles_with_spread(
    instrument: str,
    granularity: str = 'D',
    start: str = None,
    end: str = None,
    count: Optional[int] = None
) -> pd.DataFrame:
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
            'price': 'BBA',  # Get both bid and ask
        }
        
        if current_from:
            params['from'] = current_from
            
        # OANDA doesn't support 'to' with BBA price. Only from + count
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
        
        # Extract bid/ask prices and calculate spread
        for candle in candles:
            if candle.get('complete', True):
                bid = candle.get('bid', {})
                ask = candle.get('ask', {})
                
                mid_o = float(bid.get('o', 0)) + float(ask.get('o', 0))
                mid_h = float(bid.get('h', 0)) + float(ask.get('h', 0))
                mid_l = float(bid.get('l', 0)) + float(ask.get('l', 0))
                mid_c = float(bid.get('c', 0)) + float(ask.get('c', 0))
                
                all_candles.append({
                    'date': candle['time'],
                    'open': mid_o / 2,
                    'high': mid_h / 2,
                    'low': mid_l / 2,
                    'close': mid_c / 2,
                    'spread_price': (float(ask.get('c', 0)) - float(bid.get('c', 0)))
                })
        
        if len(candles) < count:
            break
        
        last_time = candles[-1]['time']
        current_from = last_time
        
        if end and last_time >= end:
            break
            
    if not all_candles:
        return pd.DataFrame(columns=['date', 'open', 'high', 'low', 'close', 'spread_price'])
    
    df = pd.DataFrame(all_candles)
    df['date'] = pd.to_datetime(df['date'])
    df = df.sort_values('date').reset_index(drop=True)
    
    return df

def get_candles_date_range_with_spread(
    instrument: str,
    start_date: str,
    end_date: str,
    granularity: str = 'D'
) -> pd.DataFrame:
    """Fetch candles by date range with spread data, with chunking for large ranges."""
    cached = _load_cached_dataframe('bba', instrument, granularity, start_date, end_date)
    if cached is not None:
        return cached

    start_dt = datetime.strptime(start_date, '%Y-%m-%d')
    end_dt = datetime.strptime(end_date, '%Y-%m-%d')

    max_days = INTRADAY_CHUNK_DAYS.get(granularity, 60)
    all_chunks = []
    current_start = start_dt
    while current_start < end_dt:
        chunk_end = current_start + timedelta(days=max_days)
        if chunk_end > end_dt:
            chunk_end = end_dt

        try:
            chunk_df = get_candles_with_spread(
                instrument=instrument,
                granularity=granularity,
                start=current_start.isoformat() + 'Z',
                count=5000
            )
        except Exception as e:
            # BBA fetch failed — fallback to midpoint candles + static spread
            print(f"  Warning: BBA spread fetch failed ({e}), using midpoint + static spread")
            try:
                chunk_df = get_candles(
                    instrument=instrument,
                    granularity=granularity,
                    start=current_start.isoformat() + 'Z',
                    count=5000
                )
                from pipeline_utils import get_spread_pips
                spread_pips = get_spread_pips(instrument)
                chunk_df['spread_price'] = spread_pips
            except Exception:
                chunk_df = pd.DataFrame(columns=['date', 'open', 'high', 'low', 'close', 'spread_price'])

        if 'spread_price' not in chunk_df.columns:
            from pipeline_utils import get_spread_pips
            spread_pips = get_spread_pips(instrument)
            chunk_df['spread_price'] = spread_pips

        all_chunks.append(chunk_df)
        current_start = chunk_end

    if not all_chunks:
        return pd.DataFrame(columns=['date', 'open', 'high', 'low', 'close', 'spread_price'])

    df = pd.concat(all_chunks, ignore_index=True)

    pip_val_map = {
        'default': 0.0001,
        'USD_JPY': 0.01,
        'XAU_USD': 0.01,
        'XAG_USD': 0.01,
        'BTC_USD': 0.01,
        'ETH_USD': 0.01,
        'LTC_USD': 0.01,
        'BCO_USD': 0.01,
        'WTICO_USD': 0.01,
        'CORN_USD': 0.01,
        'NATGAS_USD': 0.01,
    }
    pip_val = pip_val_map.get(instrument, 0.0001)

    if 'spread_price' in df.columns and len(df) > 0:
        df['spread_price'] = df['spread_price'] / pip_val
        static_spread = 2.0
        df['spread_price'] = df['spread_price'].fillna(static_spread)

    end_cutoff = pd.Timestamp(end_dt + timedelta(days=1), tz='UTC')
    df = df[df['date'] < end_cutoff].reset_index(drop=True)
    df = df.sort_values('date').reset_index(drop=True)

    _store_cached_dataframe(df, 'bba', instrument, granularity, start_date, end_date)
    return df
