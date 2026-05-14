"""
Live Tester: Paper trading on Oanda practice account for validated strategies.
Entry point: python live_test.py <strategy_id>

Workflow:
1. Fetch strategy from database (must be 'passed' status)
2. Connect to Oanda streaming API for real-time prices
3. Build rolling DataFrame of recent candles
4. Generate signals and place orders
5. Track equity curve and update live metrics
6. Runs indefinitely until manually stopped
"""

import sys
import json
import argparse
import os
from datetime import datetime, timedelta
from typing import Optional, Dict, List
import pandas as pd
import numpy as np
import requests
import time
import traceback

from pipeline_utils import (
    get_strategy_by_id,
    start_live_trading,
    update_live_metrics,
    update_live_signal,
    get_live_signals,
    save_live_state,
    load_live_state,
    compute_gt_score,
    compute_strategy_returns,
    get_price_decimals,
    init_db,
)
from data_fetcher import get_candles_date_range
from risk import (
    DrawdownCircuitBreaker,
    DrawdownLimits,
    compute_current_drawdown,
    compute_calmar_ratio,
    compute_ulcer_index,
)
from telegram_bot import notify_live_metrics, notify_drawdown_alert


# Oanda API configuration
OANDA_ACCOUNT_ID = os.getenv('OANDA_ACCOUNT_ID', '')
OANDA_API_TOKEN = os.getenv('OANDA_API_TOKEN', '')
OANDA_BASE_URL = 'https://api-fxpractice.oanda.com'
OANDA_STREAM_URL = 'https://stream-fxpractice.oanda.com'


# Configuration
ROLLING_WINDOW_SIZE = 500  # Keep 500 recent candles
POLLING_INTERVAL = 3600  # Check for new candles every 60 minutes
RISK_PER_TRADE = 0.005   # Risk 0.5% of equity per trade (baseline, scaled by portfolio weight)
PORTFOLIO_STATE_FILE = os.path.join(os.path.dirname(__file__), "portfolio_state.json")
DEFAULT_STOP_MULT = 2.0  # ATR multiplier for stop loss
ROLLING_GT_WINDOW = 30  # Compute GT-Score over last 30 days of returns
UPDATE_INTERVAL = 86400  # Update metrics daily

# Per-instrument sizing constraints (from OANDA instrument specs).
# unit_precision: decimal places for order units (0 = whole units, 3 = 0.001 BTC etc.)
# min_units / max_units: OANDA hard limits
_INSTRUMENT_SIZING = {
    'BTC_USD':  {'min_units': 0.001, 'max_units': 1000,      'unit_precision': 3},
    'ETH_USD':  {'min_units': 0.001, 'max_units': 10000,     'unit_precision': 3},
    'LTC_USD':  {'min_units': 0.1,   'max_units': 100000,    'unit_precision': 1},
    # All other instruments default to whole units with these bounds:
    '_default': {'min_units': 1,     'max_units': 100000000, 'unit_precision': 0},
}
# Practical per-instrument caps to prevent runaway sizing on practice account
_INSTRUMENT_MAX_NOTIONAL = {
    'BTC_USD':  1.0,     # max 1 BTC per trade
    'ETH_USD':  10.0,    # max 10 ETH per trade
    'LTC_USD':  100.0,   # max 100 LTC per trade
    'WTICO_USD': 5000,   # max 5000 barrels
    '_default': 50000,   # max 50k units for forex/metals
}


def _get_instrument_sizing(instrument: str) -> dict:
    """Return sizing config for an instrument, falling back to defaults."""
    return _INSTRUMENT_SIZING.get(instrument, _INSTRUMENT_SIZING['_default'])


def _load_portfolio_state(strategy_id: str):
    """
    Load portfolio_state.json written by `portfolio.py --write`.

    Returns (weight_scale, corr_peers) where:
      weight_scale  — float multiplier for RISK_PER_TRADE
                      = portfolio_weight * n_strategies
                      (so equal-weight = 1.0, higher-weight = >1.0)
      corr_peers    — list of peer strategy_ids that are correlated with this one
                      (only the weaker side gets the haircut)

    Falls back to (1.0, []) if the file is absent or can't be parsed.
    """
    try:
        with open(PORTFOLIO_STATE_FILE) as fh:
            state = json.load(fh)
        weights      = state.get("weights", {})
        n_strategies = state.get("n_strategies", 1) or 1
        own_weight   = weights.get(strategy_id, 1.0 / n_strategies)
        weight_scale = own_weight * n_strategies  # normalised so equal-weight = 1.0

        # Collect peer IDs where THIS strategy is flagged as the weaker side
        corr_peers = []
        for pair in state.get("correlated_pairs", []):
            if pair.get("weaker") == strategy_id:
                peer = pair["b"] if pair["a"] == strategy_id else pair["a"]
                corr_peers.append(peer)
            elif strategy_id in (pair.get("a"), pair.get("b")):
                # Not the weak side — still track the peer for signal monitoring
                peer = pair["b"] if pair["a"] == strategy_id else pair["a"]
                corr_peers.append(peer)

        return float(weight_scale), list(set(corr_peers))
    except Exception:
        return 1.0, []


class LiveTrader:
    """Paper trader for a single validated strategy."""
    
    def __init__(self, strategy_id: str, instrument: str = 'EUR_USD'):
        """Initialize trader for strategy."""
        self.strategy_id = strategy_id
        self.instrument = instrument
        self.headers = {
            'Authorization': f'Bearer {OANDA_API_TOKEN}',
            'Content-Type': 'application/json',
        }
        
        # Fetch strategy
        strat = get_strategy_by_id(strategy_id)
        if not strat:
            raise ValueError(f'Strategy {strategy_id} not found')
        if strat['status'] not in ('passed', 'paper_trading'):
            raise ValueError(f'Strategy {strategy_id} status is {strat["status"]}, not "passed" or "paper_trading"')
        
        self.code = strat['code']
        self.best_params = strat['best_params']
        self.rationale = strat['rationale']
        self.timeframe = strat.get('timeframe') or 'D'  # e.g. 'D', 'H4', 'H1'

        # Load strategy function
        self.strategy_func = self._load_strategy_function()

        # Drawdown circuit breaker (halt at 20% drawdown, resume at 10%)
        limits = DrawdownLimits(max_drawdown_pct=0.20, recovery_threshold_pct=0.10)
        self.breaker = DrawdownCircuitBreaker(limits)

        # Trading state
        self.current_position = 0  # -1, 0, +1
        self.prev_signal = 0       # Signal from the previous completed bar
        self.entry_price = 0.0  # Most recent entry price
        self.equity_curve = []  # List of {date, equity} dicts
        self.account_equity = 100000  # Initial balance
        self.last_metric_update = datetime.utcnow()
        self.pnl_history = []  # For rolling GT-Score
        self.halted = False  # True when drawdown circuit breaker has halted

        # Portfolio awareness (from portfolio_state.json written by portfolio.py --write)
        self.weight_scale, self.corr_peers = _load_portfolio_state(strategy_id)

        # Crash recovery: load persisted state then reconcile with live broker
        self.oanda_trade_id = None  # set by _restore_and_reconcile or _place_order
        self._restore_and_reconcile()

        print(f"\n{'='*70}")
        print(f"Live Trader: {strategy_id}")
        print(f"Instrument: {instrument}  Timeframe: {self.timeframe}")
        print(f"Best Params: {self.best_params}")
        print(f"Rationale: {self.rationale}")
        if self.weight_scale != 1.0 or self.corr_peers:
            print(f"Portfolio:  weight_scale={self.weight_scale:.2f}x  corr_peers={self.corr_peers}")
        print(f"{'='*70}\n")
    
    def _load_strategy_function(self):
        """Dynamically load strategy function from code."""
        namespace = {}
        exec(self.code, namespace)
        if 'generate_signals' not in namespace:
            raise ValueError('Strategy code must define generate_signals(df, params)')
        return namespace['generate_signals']

    def _restore_and_reconcile(self):
        """Load DB state and verify against live OANDA position. Broker is truth."""
        saved       = load_live_state(self.strategy_id)
        db_pos      = saved['current_position']
        db_price    = saved['entry_price'] or 0.0
        db_bar      = saved['last_bar_time']
        db_prev_sig = saved['prev_signal']
        db_trade_id = saved['oanda_trade_id']

        # Prefer direct trade lookup if we have an ID — single precise API call
        broker_pos   = 0
        broker_price = 0.0
        trade_id_ok  = False
        if db_trade_id:
            try:
                url = f'{OANDA_BASE_URL}/v3/accounts/{OANDA_ACCOUNT_ID}/trades/{db_trade_id}'
                r = requests.get(url, headers=self.headers, timeout=5)
                if r.status_code == 200:
                    trade = r.json().get('trade', {})
                    if trade.get('state') == 'OPEN':
                        cu = int(float(trade.get('currentUnits', 0)))
                        broker_pos   = 1 if cu > 0 else (-1 if cu < 0 else 0)
                        broker_price = float(trade.get('price', 0.0))
                    # CLOSED → broker_pos stays 0
                    trade_id_ok = True
                elif r.status_code == 404:
                    trade_id_ok = True  # definitive: trade is gone
            except Exception as e:
                print(f"[Recovery] Trade lookup failed: {e} — scanning positions", flush=True)

        # Fallback: scan all account positions
        if not trade_id_ok:
            try:
                summary = self._get_account_summary()
                for p in summary.get('positions', []):
                    if p['instrument'] == self.instrument:
                        cu = (int(float(p['long']['units'])) +
                              int(float(p['short']['units'])))
                        broker_pos = 1 if cu > 0 else (-1 if cu < 0 else 0)
            except Exception as e:
                print(f"[Recovery] Broker query failed: {e} — trusting DB state", flush=True)
                broker_pos = db_pos

        # Apply reconciliation
        if db_pos == broker_pos:
            self.current_position = db_pos
            self.entry_price      = broker_price if broker_price else db_price
            self.last_bar_time    = db_bar
            self.prev_signal      = db_prev_sig
            self.oanda_trade_id   = db_trade_id if db_pos != 0 else None
            label = f"pos={db_pos} @ {self.entry_price:.5f}" if db_pos != 0 else "flat"
            print(f"[Recovery] {self.strategy_id}: {label}", flush=True)

        elif db_pos == 0 and broker_pos != 0:
            # Crashed right after order sent, before DB write — adopt broker state
            print(f"[Recovery] WARNING: broker holds pos={broker_pos} but DB says flat "
                  f"— adopting broker state for {self.strategy_id}", flush=True)
            self.current_position = broker_pos
            self.entry_price      = broker_price
            self.last_bar_time    = db_bar
            self.prev_signal      = broker_pos
            self.oanda_trade_id   = db_trade_id
            save_live_state(self.strategy_id, broker_pos, broker_price,
                            db_bar, broker_pos, db_trade_id)

        else:
            # DB says position but broker is flat — SL/TP hit while process was down
            print(f"[Recovery] WARNING: DB says pos={db_pos} but broker is flat "
                  f"— resetting {self.strategy_id} to flat", flush=True)
            self.current_position = 0
            self.entry_price      = 0.0
            self.prev_signal      = 0
            self.last_bar_time    = db_bar
            self.oanda_trade_id   = None
            save_live_state(self.strategy_id, 0, 0.0, db_bar, 0, None)

    def _compute_position_size(self, atr: Optional[float], corr_scale: float = 1.0) -> float:
        """
        Compute position size using percent-risk model, scaled by portfolio weight
        and an optional correlation haircut. Returns float to support fractional
        units (e.g. BTC min lot = 0.001).

        risk_amount = equity * RISK_PER_TRADE * weight_scale * corr_scale
        stop_distance = stop_mult * atr
        units = risk_amount / stop_distance
        """
        sizing = _get_instrument_sizing(self.instrument)
        min_u = sizing['min_units']
        max_u = _INSTRUMENT_MAX_NOTIONAL.get(self.instrument,
                _INSTRUMENT_MAX_NOTIONAL['_default'])

        if atr is None or atr <= 0:
            return min_u
        stop_mult = self.best_params.get('stop_mult', DEFAULT_STOP_MULT)
        stop_distance = stop_mult * atr
        if stop_distance <= 0:
            return min_u
        effective_risk = RISK_PER_TRADE * self.weight_scale * corr_scale
        risk_amount = self.account_equity * effective_risk
        units = risk_amount / stop_distance
        return float(np.clip(units, min_u, max_u))

    def _compute_stop_loss(self, direction: int, entry_price: float, atr: Optional[float]) -> Optional[float]:
        """Compute ATR-based stop loss from entry."""
        if atr is None or atr <= 0 or entry_price <= 0:
            return None
        stop_mult = self.best_params.get('stop_mult', DEFAULT_STOP_MULT)
        if direction == 1:
            return entry_price - stop_mult * atr
        if direction == -1:
            return entry_price + stop_mult * atr
        return None

    def _get_account_summary(self) -> Dict:
        """Fetch current account details from Oanda."""
        try:
            url = f'{OANDA_BASE_URL}/v3/accounts/{OANDA_ACCOUNT_ID}'
            response = requests.get(url, headers=self.headers, timeout=5)
            response.raise_for_status()
            data = response.json()
            account = data['account']
            return {
                'equity': float(account['balance']),
                'positions': account.get('positions') or [],
            }
        except Exception as e:
            print(f"  Warning: Could not fetch account: {e}")
            return {'equity': self.account_equity, 'positions': []}

    def _get_current_units(self) -> float:
        """Get absolute open units for current instrument (float to support fractional crypto)."""
        min_u = _get_instrument_sizing(self.instrument)['min_units']
        account_info = self._get_account_summary()
        for pos in account_info.get('positions', []):
            if pos.get('instrument') == self.instrument:
                long_units  = float(pos.get('long',  {}).get('units', 0) or 0)
                short_units = float(pos.get('short', {}).get('units', 0) or 0)
                net_units = long_units + short_units
                return abs(net_units) if net_units != 0 else min_u
        return min_u

    def _get_corr_scale(self, signal: int) -> float:
        """
        Return 0.5 if any correlated peer strategy is currently positioned in the
        same direction as `signal`, otherwise 1.0.

        Reads current_signal from live_status table in the DB (written by each
        trader after every signal flip via update_live_signal).
        """
        if not self.corr_peers or signal == 0:
            return 1.0
        try:
            peer_signals = get_live_signals(self.corr_peers)
            same_direction = [p for p, s in peer_signals.items() if s == signal]
            if same_direction:
                print(f"  [Portfolio] Corr conflict: {same_direction} also {signal:+d} → halving size")
                return 0.5
        except Exception as e:
            print(f"  [Portfolio] Corr check failed (using full size): {e}")
        return 1.0

    def _place_order(self, signal: int, entry_price: float, atr: Optional[float]):
        """Place market order with percent-risk sizing, portfolio weight, and correlation haircut."""
        if signal == self.current_position:
            return

        # Close existing position
        if self.current_position != 0:
            try:
                # Fetch live open units from account
                open_units = self._get_current_units()
                closing_units = -self.current_position * open_units
                self._execute_order(closing_units, f'close_{self.strategy_id}', stop_loss=None)
                self.current_position = 0
                self.entry_price = 0.0
                self.oanda_trade_id = None
            except Exception as e:
                print(f"  Error closing position: {e}")
                return

        # Open new position
        if signal != 0:
            corr_scale = self._get_corr_scale(signal)
            units = self._compute_position_size(atr, corr_scale=corr_scale)
            stop_loss = self._compute_stop_loss(signal, entry_price, atr)
            try:
                trade_id = self._execute_order(units=signal * units, comment=f'{self.strategy_id}', stop_loss=stop_loss)
                self.current_position = signal
                self.entry_price = entry_price
                self.oanda_trade_id = trade_id
                sl_str = f" (SL: {stop_loss:.5f})" if stop_loss else " (no SL)"
                scale_str = f"  wt={self.weight_scale:.2f}x corr={corr_scale:.1f}x" if (self.weight_scale != 1.0 or corr_scale != 1.0) else ""
                print(f"[{datetime.now().isoformat()}] Entered {signal:+d} position, size={units} trade_id={trade_id}{sl_str}{scale_str}")
            except Exception as e:
                self.current_position = 0
                self.entry_price = 0.0
                self.oanda_trade_id = None
                print(f"  Error opening position: {e}")

        # Persist state immediately after any order so a crash doesn't lose it
        save_live_state(
            self.strategy_id,
            self.current_position,
            self.entry_price,
            getattr(self, 'last_bar_time', None),
            self.prev_signal,
            self.oanda_trade_id,
        )

    def _execute_order(self, units: float, comment: str, stop_loss: float = None) -> Optional[str]:
        """Execute market order via Oanda API. Returns OANDA trade ID if a new trade was opened."""
        url = f'{OANDA_BASE_URL}/v3/accounts/{OANDA_ACCOUNT_ID}/orders'

        # Determine exact instrument precision from central map
        decimals = get_price_decimals(self.instrument)

        # Format units with correct decimal precision for this instrument
        unit_precision = _get_instrument_sizing(self.instrument)['unit_precision']
        units_str = f'{units:.{unit_precision}f}'

        order = {
            'instrument': self.instrument,
            'units': units_str,   # OANDA requires string units
            'type': 'MARKET',
            'timeInForce': 'FOK',
            'positionFill': 'DEFAULT',
            'tradeClientExtensions': {
                'comment': comment,
            },
        }
        # Only include optional fields when they have values (OANDA rejects null)
        if stop_loss is not None:
            order['stopLossOnFill'] = {'price': f'{stop_loss:.{decimals}f}'}

        payload = {'order': order}

        response = requests.post(url, headers=self.headers, json=payload, timeout=5)
        try:
            response.raise_for_status()
        except Exception:
            print(f"  Order error detail: {response.text[:400]}")
            raise

        # Parse trade ID from fill response (present only when a new trade is opened)
        data     = response.json()
        fill_txn = data.get('orderFillTransaction', {})
        opened   = fill_txn.get('tradeOpened', {})
        return opened.get('tradeID')  # None for close orders
    
    def _fetch_candles(self, since_time: Optional[str] = None) -> pd.DataFrame:
        """Fetch recent candles from Oanda using the strategy's timeframe."""
        params = {
            'granularity': self.timeframe,
            'price': 'M',
            'count': ROLLING_WINDOW_SIZE,
        }
        
        if since_time:
            params['from'] = since_time
        
        url = f'{OANDA_BASE_URL}/v3/instruments/{self.instrument}/candles'
        
        try:
            response = requests.get(url, headers=self.headers, params=params, timeout=5)
            response.raise_for_status()
        except Exception as e:
            raise Exception(f'Candle fetch error: {e}')
        
        data = response.json()
        candles = data.get('candles', [])
        
        if not candles:
            return pd.DataFrame(columns=['date', 'open', 'high', 'low', 'close'])
        
        rows = []
        for candle in candles:
            if candle.get('complete', True):
                rows.append({
                    'date': candle['time'],
                    'open': float(candle['mid']['o']),
                    'high': float(candle['mid']['h']),
                    'low': float(candle['mid']['l']),
                    'close': float(candle['mid']['c']),
                })
        
        df = pd.DataFrame(rows)
        df['date'] = pd.to_datetime(df['date'])
        return df.sort_values('date').reset_index(drop=True)
    
    def _update_metrics(self, force: bool = False):
        """Update live metrics in database."""
        now = datetime.utcnow()
        
        if not force and (now - self.last_metric_update).total_seconds() < UPDATE_INTERVAL:
            return
        
        try:
            # Compute rolling GT-Score over last N days
            if len(self.pnl_history) > ROLLING_GT_WINDOW:
                recent_pnl = pd.Series(self.pnl_history[-ROLLING_GT_WINDOW:])
                current_score = compute_gt_score(recent_pnl)
            else:
                current_score = 0.0 if not self.pnl_history else compute_gt_score(pd.Series(self.pnl_history))
            
            # Update equity curve
            account_info = self._get_account_summary()
            self.account_equity = account_info['equity']
            
            self.equity_curve.append({
                'date': now.isoformat(),
                'equity': self.account_equity,
            })
            
            # Trim to avoid huge JSON
            if len(self.equity_curve) > 365:
                self.equity_curve = self.equity_curve[-365:]
            
            # Compute additional risk metrics
            if len(self.pnl_history) >= 10:
                returns_series = pd.Series(self.pnl_history)
                calmar = compute_calmar_ratio(returns_series)
                ulcer = compute_ulcer_index(returns_series)
                current_drawdown = compute_current_drawdown(returns_series)
            else:
                calmar = 0.0
                ulcer = 0.0
                current_drawdown = 0.0

            update_live_metrics(self.strategy_id, self.equity_curve, current_score)
            self.last_metric_update = now

            notify_live_metrics(self.strategy_id, self.account_equity,
                                current_score, self.current_position)

            print(f"[{now.isoformat()}] Metrics: equity={self.account_equity:.2f}, "
                  f"GT-Score={current_score:.4f}, Calmar={calmar:.2f}, "
                  f"Ulcer={ulcer:.2f}, Drawdown={current_drawdown:.2%}")

        except Exception as e:
            print(f"  Warning: Could not update metrics: {e}")
    
    def run_loop(self):
        """Main trading loop."""
        print(f"Starting live trading loop (polling every {POLLING_INTERVAL}s)...\n")
        
        # Initialize live status in DB on first launch
        if get_strategy_by_id(self.strategy_id).get('status') == 'passed':
            start_live_trading(self.strategy_id)
        
        # Resume from last processed bar (loaded from DB by _restore_and_reconcile)
        last_bar_time = getattr(self, 'last_bar_time', None)
        
        try:
            while True:
                # Fetch recent candles
                try:
                    candles = self._fetch_candles()
                    if len(candles) == 0:
                        print(f"[{datetime.now().isoformat()}] No candles yet")
                        time.sleep(POLLING_INTERVAL)
                        continue
                except Exception as e:
                    print(f"[{datetime.now().isoformat()}] Error fetching candles: {e}")
                    time.sleep(POLLING_INTERVAL)
                    continue
                
                # Full timestamp of the most recent completed bar (works for D, H4, H1, etc.)
                current_bar_time = candles['date'].iloc[-1]

                atr = None
                if len(candles) >= 2:
                    tr = pd.concat([
                        candles['high'] - candles['low'],
                        (candles['high'] - candles['close'].shift(1)).abs(),
                        (candles['low'] - candles['close'].shift(1)).abs(),
                    ], axis=1).max(axis=1)
                    atr_window = self.best_params.get('atr_window', 14)
                    atr_series = tr.rolling(atr_window).mean()
                    atr = atr_series.iloc[-1] if not atr_series.empty else None

                # Only act when a new completed bar has arrived
                if last_bar_time != current_bar_time:
                    # Track bar PnL and circuit breaker
                    if len(candles) > 1:
                        bar_return = (candles['close'].iloc[-1] - candles['close'].iloc[-2]) / candles['close'].iloc[-2]
                        position_return = self.current_position * bar_return
                        self.pnl_history.append(position_return)

                        breaker_result = self.breaker.feed_return(position_return)
                        action = breaker_result['action']
                        current_dd = breaker_result['current_drawdown']

                        if action == 'halt' and not self.halted:
                            self.halted = True
                            notify_drawdown_alert(self.strategy_id, current_dd, 'halt')
                            print(f"[{current_bar_time}] Drawdown halt triggered: {current_dd:.2%}")
                            if self.current_position != 0:
                                entry_price = float(candles['close'].iloc[-1])
                                self._place_order(0, entry_price, atr)
                        elif action == 'resume' and self.halted:
                            self.halted = False
                            notify_drawdown_alert(self.strategy_id, current_dd, 'resume')
                            print(f"[{current_bar_time}] Drawdown recovered: {current_dd:.2%}")

                        print(f"[{current_bar_time}] [{self.timeframe}] Bar return: {bar_return:+.4f}, Position: {self.current_position:+d}, P&L: {position_return:+.4f}")

                    last_bar_time = current_bar_time
                    self.last_bar_time = current_bar_time  # keep attribute in sync for _place_order
                    self._update_metrics(force=True)

                    # Generate signal from the newly completed bar
                    try:
                        signals = self.strategy_func(candles, self.best_params)
                        latest_signal = int(signals.iloc[-1]) if len(signals) > 0 else 0
                    except Exception as e:
                        print(f"  Error generating signal: {e}")
                        latest_signal = self.prev_signal  # hold last known signal on error

                    # Only place order if the signal actually flipped on this new bar
                    if latest_signal != self.prev_signal:
                        print(f"[{current_bar_time}] Signal flip: {self.prev_signal:+d} → {latest_signal:+d}")
                        self.prev_signal = latest_signal
                        # Publish signal to DB so correlated peers can see it
                        try:
                            update_live_signal(self.strategy_id, latest_signal)
                        except Exception:
                            pass
                        if not self.halted and latest_signal != self.current_position:
                            entry_price = float(candles['close'].iloc[-1])
                            self._place_order(latest_signal, entry_price, atr)
                            # _place_order already saves state after orders; save here covers no-order flip
                        else:
                            save_live_state(
                                self.strategy_id,
                                self.current_position,
                                self.entry_price,
                                str(current_bar_time),
                                self.prev_signal,
                                self.oanda_trade_id,
                            )
                    else:
                        # No flip but new bar — update last_bar_time in DB
                        save_live_state(
                            self.strategy_id,
                            self.current_position,
                            self.entry_price,
                            str(current_bar_time),
                            self.prev_signal,
                            self.oanda_trade_id,
                        )
                else:
                    self._update_metrics()  # periodic metrics between bars
                
                # Wait for next poll
                time.sleep(POLLING_INTERVAL)
        
        except KeyboardInterrupt:
            print(f"\n\n[{datetime.now().isoformat()}] Keyboard interrupt: stopping live trader")
            # Close any open position
            if self.current_position != 0:
                try:
                    latest_price = self.entry_price if self.entry_price > 0 else 0.0
                    self._place_order(0, latest_price, None)
                except Exception:
                    pass
        
        except Exception as e:
            print(f"\nFatal error in trading loop: {e}")
            print(traceback.format_exc())


def main():
    """CLI entry point."""
    parser = argparse.ArgumentParser(description='Run live paper trading for validated strategy')
    parser.add_argument('strategy_id', help='Strategy ID to trade')
    parser.add_argument('--instrument', default='EUR_USD', help='Instrument to trade (default: EUR_USD)')
    args = parser.parse_args()
    
    # Check credentials
    if not OANDA_ACCOUNT_ID or not OANDA_API_TOKEN:
        print("ERROR: OANDA_ACCOUNT_ID and OANDA_API_TOKEN env vars required")
        sys.exit(1)
    
    # Initialize database
    init_db()
    
    # Create and run trader
    try:
        trader = LiveTrader(args.strategy_id, args.instrument)
        trader.run_loop()
    
    except Exception as e:
        print(f"\nERROR: {e}")
        print(traceback.format_exc())
        sys.exit(1)


if __name__ == '__main__':
    main()
