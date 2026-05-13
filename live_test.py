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
POLLING_INTERVAL = 60  # Check for new candles every 60 seconds
MIN_POSITION_SIZE = 500  # Minimum position size (units)
MAX_POSITION_SIZE = 50000  # Maximum position size (units)
RISK_PER_TRADE = 0.005   # Risk 0.5% of equity per trade
DEFAULT_STOP_MULT = 2.0  # ATR multiplier for stop loss
ROLLING_GT_WINDOW = 30  # Compute GT-Score over last 30 days of returns
UPDATE_INTERVAL = 86400  # Update metrics daily


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

        print(f"\n{'='*70}")
        print(f"Live Trader: {strategy_id}")
        print(f"Instrument: {instrument}  Timeframe: {self.timeframe}")
        print(f"Best Params: {self.best_params}")
        print(f"Rationale: {self.rationale}")
        print(f"{'='*70}\n")
    
    def _load_strategy_function(self):
        """Dynamically load strategy function from code."""
        namespace = {}
        exec(self.code, namespace)
        if 'generate_signals' not in namespace:
            raise ValueError('Strategy code must define generate_signals(df, params)')
        return namespace['generate_signals']

    def _compute_position_size(self, atr: Optional[float]) -> int:
        """
        Compute position size using percent-risk model.
        risk_amount = equity * RISK_PER_TRADE
        stop_distance = stop_mult * atr
        units = risk_amount / stop_distance
        """
        if atr is None or atr <= 0:
            return MIN_POSITION_SIZE
        stop_mult = self.best_params.get('stop_mult', DEFAULT_STOP_MULT)
        stop_distance = stop_mult * atr
        if stop_distance <= 0:
            return MIN_POSITION_SIZE
        risk_amount = self.account_equity * RISK_PER_TRADE
        units = risk_amount / stop_distance
        return int(np.clip(units, MIN_POSITION_SIZE, MAX_POSITION_SIZE))

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

    def _get_current_units(self) -> int:
        """Get absolute open units for current instrument."""
        account_info = self._get_account_summary()
        for pos in account_info.get('positions', []):
            if pos.get('instrument') == self.instrument:
                long_units = int(float(pos.get('long', {}).get('units', 0) or 0))
                short_units = int(float(pos.get('short', {}).get('units', 0) or 0))
                net_units = long_units + short_units
                return abs(net_units) if net_units != 0 else MIN_POSITION_SIZE
        return MIN_POSITION_SIZE

    def _place_order(self, signal: int, entry_price: float, atr: Optional[float]):
        """Place market order with percent-risk sizing and stop loss."""
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
            except Exception as e:
                print(f"  Error closing position: {e}")
                return

        # Open new position
        if signal != 0:
            units = self._compute_position_size(atr)
            stop_loss = self._compute_stop_loss(signal, entry_price, atr)
            try:
                self._execute_order(units=signal * units, comment=f'{self.strategy_id}', stop_loss=stop_loss)
                self.current_position = signal
                self.entry_price = entry_price
                sl_str = f" (SL: {stop_loss:.5f})" if stop_loss else " (no SL)"
                print(f"[{datetime.now().isoformat()}] Entered {signal:+d} position, size={units}{sl_str}")
            except Exception as e:
                self.current_position = 0
                self.entry_price = 0.0
                print(f"  Error opening position: {e}")

    def _execute_order(self, units: int, comment: str, stop_loss: float = None):
        """Execute market order via Oanda API with optional stop loss."""
        url = f'{OANDA_BASE_URL}/v3/accounts/{OANDA_ACCOUNT_ID}/orders'

        # Determine exact instrument precision from central map
        decimals = get_price_decimals(self.instrument)

        order = {
            'instrument': self.instrument,
            'units': str(units),   # OANDA requires string units
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
        
        last_bar_time = None   # Full timestamp of last processed bar (works for any timeframe)
        
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
                        if not self.halted and latest_signal != self.current_position:
                            entry_price = float(candles['close'].iloc[-1])
                            self._place_order(latest_signal, entry_price, atr)
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
