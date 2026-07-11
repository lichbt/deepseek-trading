"""
cTrader Open API adapter — drop-in replacement for OANDA execution in live_test.py.

Provides a BrokerAdapter ABC and two implementations (OandaAdapter, CTraderAdapter)
so live_test.py can run on either broker without changing signal/portfolio logic.

cTrader protocol: Protobuf over TCP via the `ctrader-open-api` SDK.
Auth: OAuth 2.0 (app auth → account auth) with long-lived refresh tokens.

Usage:
    adapter = get_broker_adapter(instrument)
    adapter.execute_order(units=100, comment="my_sleeve", stop_loss=1.2345)
    price = adapter.get_current_price()
    summary = adapter.get_account_summary()
    candles = adapter.fetch_candles(count=500, granularity='D')
"""

import os
import json
import time
import threading
from abc import ABC, abstractmethod
from datetime import datetime, timedelta, timezone
from typing import Optional, Dict, List, Tuple
from pathlib import Path

import pandas as pd
import requests


# ---------------------------------------------------------------------------
# Abstract broker interface
# ---------------------------------------------------------------------------

class BrokerAdapter(ABC):
    """Minimal broker interface used by LiveTrader."""

    def __init__(self, instrument: str):
        self.instrument = instrument

    @abstractmethod
    def execute_order(self, units: float, comment: str,
                      stop_loss: Optional[float] = None) -> Optional[str]:
        """Place a market order. Returns trade/position ID or None."""

    @abstractmethod
    def get_current_price(self) -> Optional[float]:
        """Live mid price. None on failure."""

    @abstractmethod
    def get_account_summary(self) -> Dict:
        """Return {'equity': float, 'positions': list}."""

    @abstractmethod
    def get_current_units(self, min_units: float = 1.0) -> float:
        """Absolute open units for this instrument."""

    @abstractmethod
    def fetch_candles(self, count: int = 500, granularity: str = 'D',
                      since_time: Optional[str] = None) -> pd.DataFrame:
        """Return DataFrame with columns [date, open, high, low, close]."""

    @abstractmethod
    def get_trade_state(self, trade_id: str) -> Optional[Dict]:
        """Return trade info dict or None if trade doesn't exist / is closed."""

    @abstractmethod
    def quote_to_usd_rate(self) -> float:
        """Value of 1 unit of quote currency in USD."""


# ---------------------------------------------------------------------------
# OANDA adapter (extracts existing logic from live_test.py)
# ---------------------------------------------------------------------------

OANDA_ACCOUNT_ID = os.getenv('OANDA_ACCOUNT_ID', '')
OANDA_API_TOKEN = os.getenv('OANDA_API_TOKEN', '')
OANDA_BASE_URL = 'https://api-fxpractice.oanda.com'


class OandaAdapter(BrokerAdapter):
    """OANDA v20 REST adapter — wraps the existing live_test.py broker calls."""

    _QUOTE_TO_USD_PAIR = {
        'JPY': ('USD_JPY', True,  1.0 / 150.0),
        'CHF': ('USD_CHF', True,  1.0 / 0.91),
        'CAD': ('USD_CAD', True,  1.0 / 1.37),
        'HKD': ('USD_HKD', True,  1.0 / 7.80),
        'SGD': ('USD_SGD', True,  1.0 / 1.34),
        'EUR': ('EUR_USD', False, 1.08),
        'GBP': ('GBP_USD', False, 1.25),
        'AUD': ('AUD_USD', False, 0.66),
        'NZD': ('NZD_USD', False, 0.59),
    }

    def __init__(self, instrument: str):
        super().__init__(instrument)
        self.headers = {
            'Authorization': f'Bearer {OANDA_API_TOKEN}',
            'Content-Type': 'application/json',
        }

    def execute_order(self, units, comment, stop_loss=None):
        from pipeline_utils import get_price_decimals
        from live_test import _get_instrument_sizing
        url = f'{OANDA_BASE_URL}/v3/accounts/{OANDA_ACCOUNT_ID}/orders'
        decimals = get_price_decimals(self.instrument)
        unit_precision = _get_instrument_sizing(self.instrument)['unit_precision']
        units_str = f'{units:.{unit_precision}f}'

        order = {
            'instrument': self.instrument,
            'units': units_str,
            'type': 'MARKET',
            'timeInForce': 'FOK',
            'positionFill': 'DEFAULT',
            'tradeClientExtensions': {'comment': comment},
        }
        if stop_loss is not None:
            order['stopLossOnFill'] = {'price': f'{stop_loss:.{decimals}f}'}

        r = requests.post(url, headers=self.headers, json={'order': order}, timeout=5)
        try:
            r.raise_for_status()
        except Exception:
            print(f"  Order error detail: {r.text[:400]}")
            raise

        data = r.json()
        fill_txn = data.get('orderFillTransaction', {})
        trade_id = fill_txn.get('tradeOpened', {}).get('tradeID')

        if trade_id is None and data.get('orderCancelTransaction'):
            reason = data['orderCancelTransaction'].get('reason', 'UNKNOWN')
            raise RuntimeError(f'Order cancelled by OANDA (reason={reason})')

        if stop_loss is not None and data.get('stopLossOrderRejectTransaction'):
            reason = data['stopLossOrderRejectTransaction'].get('reason', 'UNKNOWN')
            print(f"  [WARN] Stop-loss REJECTED (reason={reason})", flush=True)

        return trade_id

    def get_current_price(self):
        try:
            url = f'{OANDA_BASE_URL}/v3/accounts/{OANDA_ACCOUNT_ID}/pricing'
            r = requests.get(url, headers=self.headers,
                             params={'instruments': self.instrument}, timeout=5)
            r.raise_for_status()
            prices = r.json().get('prices', [])
            if prices:
                bid = float(prices[0]['bids'][0]['price'])
                ask = float(prices[0]['asks'][0]['price'])
                return (bid + ask) / 2.0
        except Exception as e:
            print(f"  Price fetch error: {e}")
        return None

    def get_account_summary(self):
        try:
            url = f'{OANDA_BASE_URL}/v3/accounts/{OANDA_ACCOUNT_ID}'
            r = requests.get(url, headers=self.headers, timeout=5)
            r.raise_for_status()
            account = r.json()['account']
            equity = float(account.get('NAV', account.get('balance', 0.0)))
            return {'equity': equity, 'positions': account.get('positions') or []}
        except Exception as e:
            print(f"  Warning: Could not fetch account: {e}")
            return {'equity': 0.0, 'positions': []}

    def get_current_units(self, min_units=1.0):
        summary = self.get_account_summary()
        for pos in summary.get('positions', []):
            if pos.get('instrument') == self.instrument:
                long_u = float(pos.get('long', {}).get('units', 0) or 0)
                short_u = float(pos.get('short', {}).get('units', 0) or 0)
                net = long_u + short_u
                return abs(net) if net != 0 else min_units
        return min_units

    def fetch_candles(self, count=500, granularity='D', since_time=None):
        params = {'granularity': granularity, 'price': 'M', 'count': count}
        if since_time:
            params['from'] = since_time
        url = f'{OANDA_BASE_URL}/v3/instruments/{self.instrument}/candles'
        r = requests.get(url, headers=self.headers, params=params, timeout=5)
        r.raise_for_status()
        candles = r.json().get('candles', [])
        if not candles:
            return pd.DataFrame(columns=['date', 'open', 'high', 'low', 'close'])
        rows = []
        for c in candles:
            if c.get('complete', True):
                rows.append({
                    'date': c['time'],
                    'open': float(c['mid']['o']),
                    'high': float(c['mid']['h']),
                    'low': float(c['mid']['l']),
                    'close': float(c['mid']['c']),
                })
        df = pd.DataFrame(rows)
        df['date'] = pd.to_datetime(df['date'])
        return df.sort_values('date').reset_index(drop=True)

    def get_trade_state(self, trade_id):
        try:
            url = f'{OANDA_BASE_URL}/v3/accounts/{OANDA_ACCOUNT_ID}/trades/{trade_id}'
            r = requests.get(url, headers=self.headers, timeout=5)
            if r.status_code == 200:
                trade = r.json().get('trade', {})
                if trade.get('state') == 'OPEN':
                    return trade
            return None
        except Exception:
            return None

    def quote_to_usd_rate(self):
        parts = self.instrument.split('_')
        if len(parts) < 2:
            return 1.0
        quote = parts[1].upper()
        if quote == 'USD':
            return 1.0
        info = self._QUOTE_TO_USD_PAIR.get(quote)
        if info is None:
            return 1.0
        pair, invert, fallback = info
        try:
            url = f'{OANDA_BASE_URL}/v3/accounts/{OANDA_ACCOUNT_ID}/pricing'
            r = requests.get(url, headers=self.headers,
                             params={'instruments': pair}, timeout=5)
            r.raise_for_status()
            prices = r.json().get('prices', [])
            if prices:
                bid = float(prices[0]['bids'][0]['price'])
                ask = float(prices[0]['asks'][0]['price'])
                mid = (bid + ask) / 2.0
                return 1.0 / mid if invert else mid
        except Exception as e:
            print(f"  [Sizing] FX rate fetch failed: {e} — using fallback")
        return fallback


# ---------------------------------------------------------------------------
# cTrader adapter
# ---------------------------------------------------------------------------

# cTrader instrument symbol mapping (OANDA format → cTrader symbol)
# cTrader uses symbols like "EURUSD", "XAUUSD", "US100" etc.
# This maps our internal OANDA-style names to cTrader conventions.
_CTRADER_SYMBOL_MAP = {
    'EUR_USD': 'EURUSD', 'GBP_USD': 'GBPUSD', 'USD_CHF': 'USDCHF',
    'USD_JPY': 'USDJPY', 'GBP_JPY': 'GBPJPY', 'EUR_JPY': 'EURJPY',
    'EUR_GBP': 'EURGBP', 'AUD_USD': 'AUDUSD', 'NZD_USD': 'NZDUSD',
    'USD_CAD': 'USDCAD',
    'XAU_USD': 'XAUUSD', 'XAG_USD': 'XAGUSD',
    'XCU_USD': 'COPPER',  # varies by broker
    'XPT_USD': 'XPTUSD', 'XPD_USD': 'XPDUSD',
    'NAS100_USD': 'US100', 'SPX500_USD': 'US500',
    'DE30_EUR': 'DE40',   # DAX is now DE40 on most brokers
    'AU200_AUD': 'AUS200',
    'BTC_USD': 'BTCUSD',
    'WHEAT_USD': 'WHEAT', 'SOYBN_USD': 'SOYBEAN',
    'WTICO_USD': 'XTIUSD',
}

# cTrader timeframe mapping (our granularity → cTrader ProtoOATrendbarPeriod values)
_CTRADER_TF_MAP = {
    'M1': 1, 'M5': 2, 'M15': 3, 'M30': 4,
    'H1': 5, 'H4': 6, 'D': 7, 'W': 8, 'MN': 9,
}

# Token file path
_TOKEN_FILE = os.path.join(os.path.dirname(__file__), '.ctrader_tokens.json')


def _load_ctrader_tokens() -> Dict:
    """Load OAuth tokens from disk."""
    if not os.path.exists(_TOKEN_FILE):
        return {}
    with open(_TOKEN_FILE) as f:
        return json.load(f)


def _save_ctrader_tokens(tokens: Dict):
    """Persist OAuth tokens."""
    with open(_TOKEN_FILE, 'w') as f:
        json.dump(tokens, f, indent=2)


def _refresh_access_token(client_id: str, client_secret: str,
                           refresh_token: str) -> Dict:
    """Exchange refresh token for a new access token via cTrader OAuth."""
    url = 'https://openapi.ctrader.com/apps/token'
    payload = {
        'grant_type': 'refresh_token',
        'client_id': client_id,
        'client_secret': client_secret,
        'refresh_token': refresh_token,
    }
    r = requests.post(url, data=payload, timeout=10)
    r.raise_for_status()
    data = r.json()
    return {
        'access_token': data['access_token'],
        'refresh_token': data.get('refresh_token', refresh_token),
        'expires_at': time.time() + data.get('expires_in', 2592000),
    }


class CTraderAdapter(BrokerAdapter):
    """cTrader Open API adapter using Protobuf TCP via ctrader-open-api SDK.

    Env vars:
        CTRADER_CLIENT_ID      — OAuth app client ID
        CTRADER_CLIENT_SECRET  — OAuth app client secret
        CTRADER_ACCOUNT_ID     — ctidTraderAccountId (numeric)
        CTRADER_ENV            — 'demo' or 'live' (default: demo)
    """

    def __init__(self, instrument: str):
        super().__init__(instrument)

        self.client_id = os.environ['CTRADER_CLIENT_ID']
        self.client_secret = os.environ['CTRADER_CLIENT_SECRET']
        self.account_id = int(os.environ['CTRADER_ACCOUNT_ID'])
        self.env = os.environ.get('CTRADER_ENV', 'demo')
        self.host = f'{self.env}.ctraderapi.com'
        self.port = 5035

        self.symbol = _CTRADER_SYMBOL_MAP.get(instrument, instrument.replace('_', ''))
        self._symbol_id = None  # resolved on first use
        self._digits = None     # price decimal places for this symbol
        self._pip_position = None
        self._lot_size = None   # contract size (e.g. 100000 for forex)

        self._client = None
        self._connected = False
        self._lock = threading.Lock()
        self._response_events = {}

        self._ensure_tokens()

    # --- Token management ---

    def _ensure_tokens(self):
        tokens = _load_ctrader_tokens()
        if not tokens.get('access_token'):
            raise RuntimeError(
                'No cTrader tokens found. Run: python ctrader_auth.py '
                'to complete the OAuth flow first.')
        if tokens.get('expires_at', 0) < time.time() + 300:
            print("[cTrader] Access token expired, refreshing...", flush=True)
            tokens = _refresh_access_token(
                self.client_id, self.client_secret, tokens['refresh_token'])
            _save_ctrader_tokens(tokens)
        self._access_token = tokens['access_token']

    def _get_access_token(self) -> str:
        tokens = _load_ctrader_tokens()
        if tokens.get('expires_at', 0) < time.time() + 300:
            tokens = _refresh_access_token(
                self.client_id, self.client_secret, tokens['refresh_token'])
            _save_ctrader_tokens(tokens)
            self._access_token = tokens['access_token']
        return self._access_token

    # --- Connection ---

    def _connect(self):
        """Establish Protobuf TCP connection and authenticate."""
        if self._connected:
            return

        try:
            from ctrader_open_api import Client, Protobuf, TcpProtocol, EndPoints
            from ctrader_open_api.messages.OpenApiCommonMessages_pb2 import (
                ProtoHeartbeatEvent,
            )
            from ctrader_open_api.messages.OpenApiMessages_pb2 import (
                ProtoOAApplicationAuthReq,
                ProtoOAApplicationAuthRes,
                ProtoOAAccountAuthReq,
                ProtoOAAccountAuthRes,
                ProtoOASymbolsListReq,
                ProtoOASymbolsListRes,
                ProtoOASymbolByIdReq,
                ProtoOASymbolByIdRes,
            )
        except ImportError:
            raise ImportError(
                'ctrader-open-api not installed. Run: pip install ctrader-open-api')

        host = EndPoints.PROTOBUF_LIVE_HOST if self.env == 'live' \
            else EndPoints.PROTOBUF_DEMO_HOST
        port = EndPoints.PROTOBUF_PORT

        self._client = Client(host, port, TcpProtocol)

        # Blocking connect + app auth + account auth
        self._client.connect()

        # App auth
        req = ProtoOAApplicationAuthReq()
        req.clientId = self.client_id
        req.clientSecret = self.client_secret
        deferred = self._client.send(req)
        deferred.addCallback(lambda _: None)

        # Account auth
        token = self._get_access_token()
        req2 = ProtoOAAccountAuthReq()
        req2.ctidTraderAccountId = self.account_id
        req2.accessToken = token
        deferred2 = self._client.send(req2)
        deferred2.addCallback(lambda _: None)

        self._connected = True
        print(f"[cTrader] Connected to {host}:{port} "
              f"account={self.account_id}", flush=True)

    def _ensure_symbol_id(self):
        """Resolve our instrument name to a cTrader symbolId (cached)."""
        if self._symbol_id is not None:
            return
        self._connect()

        from ctrader_open_api.messages.OpenApiMessages_pb2 import (
            ProtoOASymbolsListReq,
        )

        req = ProtoOASymbolsListReq()
        req.ctidTraderAccountId = self.account_id
        resp = self._send_sync(req)

        for sym in resp.symbol:
            if sym.symbolName == self.symbol:
                self._symbol_id = sym.symbolId
                self._digits = sym.digits
                self._pip_position = getattr(sym, 'pipPosition', None)
                self._lot_size = sym.lotSize if hasattr(sym, 'lotSize') else 100000
                break

        if self._symbol_id is None:
            available = [s.symbolName for s in resp.symbol[:20]]
            raise ValueError(
                f'Symbol "{self.symbol}" not found on cTrader. '
                f'First 20 available: {available}')

    def _send_sync(self, req, timeout=10):
        """Send a Protobuf request and wait for the response synchronously."""
        import queue
        q = queue.Queue()

        def on_response(resp):
            q.put(resp)

        deferred = self._client.send(req)
        deferred.addCallback(on_response)
        deferred.addErrback(lambda err: q.put(err))

        try:
            result = q.get(timeout=timeout)
        except queue.Empty:
            raise TimeoutError(f'cTrader request timed out after {timeout}s')

        if isinstance(result, Exception):
            raise result
        return result

    # --- BrokerAdapter interface ---

    def execute_order(self, units, comment, stop_loss=None):
        self._ensure_symbol_id()

        from ctrader_open_api.messages.OpenApiMessages_pb2 import (
            ProtoOANewOrderReq,
        )
        from ctrader_open_api.messages.OpenApiCommonMessages_pb2 import (
            ProtoOAOrderType,
            ProtoOATradeSide,
        )

        volume = int(abs(units) * (self._lot_size or 100000))

        req = ProtoOANewOrderReq()
        req.ctidTraderAccountId = self.account_id
        req.symbolId = self._symbol_id
        req.orderType = ProtoOAOrderType.MARKET
        req.tradeSide = (ProtoOATradeSide.BUY if units > 0
                         else ProtoOATradeSide.SELL)
        req.volume = volume
        if comment:
            req.comment = comment[:50]

        if stop_loss is not None and self._digits is not None:
            req.stopLoss = round(stop_loss, self._digits)
            req.stopLossTriggerMethod = 1  # TRADE

        try:
            resp = self._send_sync(req, timeout=15)
        except Exception as e:
            raise RuntimeError(f'cTrader order failed: {e}')

        position_id = getattr(resp, 'positionId', None)
        if position_id:
            return str(position_id)

        error_code = getattr(resp, 'errorCode', None)
        if error_code:
            raise RuntimeError(
                f'cTrader order rejected: code={error_code} '
                f'desc={getattr(resp, "description", "")}')

        return None

    def get_current_price(self):
        self._ensure_symbol_id()

        from ctrader_open_api.messages.OpenApiMessages_pb2 import (
            ProtoOASubscribeSpotsReq,
            ProtoOASpotEvent,
        )

        # For a one-shot price, use the symbol quote endpoint
        try:
            from ctrader_open_api.messages.OpenApiMessages_pb2 import (
                ProtoOAGetTickDataReq,
            )
            req = ProtoOAGetTickDataReq()
            req.ctidTraderAccountId = self.account_id
            req.symbolId = self._symbol_id
            req.type = 3  # TRADE ticks
            req.fromTimestamp = int((time.time() - 5) * 1000)
            req.toTimestamp = int(time.time() * 1000)

            resp = self._send_sync(req, timeout=5)
            if resp.tickData:
                last_tick = resp.tickData[-1]
                price = last_tick.tick / (10 ** (self._digits or 5))
                return price
        except Exception as e:
            print(f"  [cTrader] Price fetch error: {e}")
        return None

    def get_account_summary(self):
        self._connect()

        from ctrader_open_api.messages.OpenApiMessages_pb2 import (
            ProtoOATraderReq,
        )

        req = ProtoOATraderReq()
        req.ctidTraderAccountId = self.account_id

        try:
            resp = self._send_sync(req, timeout=5)
            trader = resp.trader
            equity = trader.balance / 100.0  # cTrader stores in cents
            return {'equity': equity, 'positions': []}
        except Exception as e:
            print(f"  [cTrader] Account fetch error: {e}")
            return {'equity': 0.0, 'positions': []}

    def get_current_units(self, min_units=1.0):
        self._connect()

        from ctrader_open_api.messages.OpenApiMessages_pb2 import (
            ProtoOAReconcileReq,
        )

        req = ProtoOAReconcileReq()
        req.ctidTraderAccountId = self.account_id

        try:
            resp = self._send_sync(req, timeout=5)
            for pos in resp.position:
                if pos.tradeData.symbolId == self._symbol_id:
                    vol = pos.tradeData.volume / (self._lot_size or 100000)
                    return abs(vol) if vol != 0 else min_units
        except Exception as e:
            print(f"  [cTrader] Position fetch error: {e}")
        return min_units

    def fetch_candles(self, count=500, granularity='D', since_time=None):
        self._ensure_symbol_id()

        from ctrader_open_api.messages.OpenApiMessages_pb2 import (
            ProtoOAGetTrendbarsReq,
        )

        period = _CTRADER_TF_MAP.get(granularity, 7)  # default to Daily

        if since_time:
            from_ts = int(pd.Timestamp(since_time).timestamp() * 1000)
        else:
            # Approximate: go back `count` bars
            bar_seconds = {'M1': 60, 'M5': 300, 'M15': 900, 'M30': 1800,
                           'H1': 3600, 'H4': 14400, 'D': 86400,
                           'W': 604800, 'MN': 2592000}
            secs = bar_seconds.get(granularity, 86400) * count
            from_ts = int((time.time() - secs) * 1000)

        to_ts = int(time.time() * 1000)

        req = ProtoOAGetTrendbarsReq()
        req.ctidTraderAccountId = self.account_id
        req.symbolId = self._symbol_id
        req.period = period
        req.fromTimestamp = from_ts
        req.toTimestamp = to_ts

        try:
            resp = self._send_sync(req, timeout=15)
        except Exception as e:
            raise Exception(f'cTrader candle fetch error: {e}')

        if not resp.trendbar:
            return pd.DataFrame(columns=['date', 'open', 'high', 'low', 'close'])

        divisor = 10 ** (self._digits or 5)
        rows = []
        for bar in resp.trendbar:
            # cTrader trendbars: low is absolute, others are deltas from low
            low = bar.low / divisor
            rows.append({
                'date': datetime.fromtimestamp(bar.utcTimestampInMinutes * 60,
                                               tz=timezone.utc),
                'open': low + (bar.deltaOpen / divisor),
                'high': low + (bar.deltaHigh / divisor),
                'low': low,
                'close': low + (bar.deltaClose / divisor),
            })

        df = pd.DataFrame(rows)
        df['date'] = pd.to_datetime(df['date'])
        return df.sort_values('date').reset_index(drop=True)

    def get_trade_state(self, trade_id):
        self._connect()

        from ctrader_open_api.messages.OpenApiMessages_pb2 import (
            ProtoOAReconcileReq,
        )

        req = ProtoOAReconcileReq()
        req.ctidTraderAccountId = self.account_id

        try:
            resp = self._send_sync(req, timeout=5)
            for pos in resp.position:
                if str(pos.positionId) == str(trade_id):
                    return {
                        'state': 'OPEN',
                        'currentUnits': pos.tradeData.volume / (self._lot_size or 100000),
                        'price': pos.price / (10 ** (self._digits or 5)),
                    }
        except Exception:
            pass
        return None

    def quote_to_usd_rate(self):
        parts = self.instrument.split('_')
        if len(parts) < 2:
            return 1.0
        quote = parts[1].upper()
        if quote == 'USD':
            return 1.0
        # Use OANDA's static fallbacks for now — the FX rate doesn't change fast
        # enough to matter for position sizing, and fetching a cross-rate via
        # cTrader's tick API adds complexity for minimal accuracy gain.
        fallbacks = {
            'JPY': 1.0 / 150.0, 'CHF': 1.0 / 0.91, 'CAD': 1.0 / 1.37,
            'EUR': 1.08, 'GBP': 1.25, 'AUD': 0.66, 'NZD': 0.59,
        }
        return fallbacks.get(quote, 1.0)


# ---------------------------------------------------------------------------
# Factory
# ---------------------------------------------------------------------------

def get_broker_adapter(instrument: str, broker: Optional[str] = None) -> BrokerAdapter:
    """Return the appropriate broker adapter based on env config.

    broker: 'oanda' or 'ctrader'. If None, auto-detect from env vars.
    """
    if broker is None:
        broker = os.environ.get('BROKER', 'oanda').lower()

    if broker == 'ctrader':
        return CTraderAdapter(instrument)
    return OandaAdapter(instrument)
