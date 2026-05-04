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
    init_db,
)
from data_fetcher import get_candles_date_range
from telegram_bot import notify_live_metrics, notify_drawdown_alert


# Oanda API configuration
OANDA_ACCOUNT_ID = os.getenv('OANDA_ACCOUNT_ID', '')
OANDA_API_TOKEN = os.getenv('OANDA_API_TOKEN', '')
OANDA_BASE_URL = 'https://api-fxpractice.oanda.com'
OANDA_STREAM_URL = 'https://stream-fxpractice.oanda.com'


# Configuration
ROLLING_WINDOW_SIZE = 500  # Keep 500 recent candles
POLLING_INTERVAL = 60  # Check for new candles every 60 seconds
POSITION_SIZE = 1000  # Units per trade (1k EUR for micro lot)
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
        if strat['status'] != 'passed':
            raise ValueError(f'Strategy {strategy_id} status is {strat["status"]}, not "passed"')
        
        self.code = strat['code']
        self.best_params = strat['best_params']
        self.rationale = strat['rationale']
        
        # Load strategy function
        self.strategy_func = self._load_strategy_function()
        
        # Trading state
        self.current_position = 0  # -1, 0, +1
        self.equity_curve = []  # List of {date, equity} dicts
        self.account_equity = 100000  # Initial balance
        self.last_metric_update = datetime.utcnow()
        self.pnl_history = []  # For rolling GT-Score
        
        print(f"\n{'='*70}")
        print(f"Live Trader: {strategy_id}")
        print(f"Instrument: {instrument}")
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
    
    def _get_account_summary(self) -> Dict:
        """Fetch current account details from Oanda."""
        try:
            url = f'{OANDA_BASE_URL}/v3/accounts/{OANDA_ACCOUNT_ID}'
            response = requests.get(url, headers=self.headers, timeout=5)
            response.raise_for_status()
            data = response.json()
            return {
                'equity': float(data['account']['balance']),
                'positions': data['account']['openPositions'] or [],
            }
        except Exception as e:
            print(f"  Warning: Could not fetch account: {e}")
            return {'equity': self.account_equity, 'positions': []}
    
    def _place_order(self, signal: int):
        """Place market order on Oanda."""
        if signal == self.current_position:
            return  # No change
        
        # Close existing position if any
        if self.current_position != 0:
            closing_units = -self.current_position * POSITION_SIZE
            try:
                self._execute_order(closing_units, f'close_{self.strategy_id}')
                self.current_position = 0
            except Exception as e:
                print(f"  Error closing position: {e}")
                return
        
        # Open new position
        if signal != 0:
            units = signal * POSITION_SIZE
            try:
                self._execute_order(units, f'{self.strategy_id}')
                self.current_position = signal
                print(f"[{datetime.now().isoformat()}] Entered {signal:+d} position")
            except Exception as e:
                print(f"  Error opening position: {e}")
    
    def _execute_order(self, units: int, comment: str):
        """Execute market order via Oanda API."""
        url = f'{OANDA_BASE_URL}/v3/accounts/{OANDA_ACCOUNT_ID}/orders'
        
        payload = {
            'order': {
                'instrument': self.instrument,
                'units': units,
                'type': 'MARKET',
                'priceBound': None,
                'takeProfitOnFill': None,
                'stopLossOnFill': None,
                'trailingStopLossOnFill': None,
                'tradeClientExtensions': {
                    'comment': comment,
                },
            }
        }
        
        response = requests.post(url, headers=self.headers, json=payload, timeout=5)
        response.raise_for_status()
    
    def _fetch_candles(self, since_time: Optional[str] = None) -> pd.DataFrame:
        """Fetch recent candles from Oanda."""
        params = {
            'granularity': 'D',
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
            
            update_live_metrics(self.strategy_id, self.equity_curve, current_score)
            self.last_metric_update = now
            
            notify_live_metrics(self.strategy_id, self.account_equity,
                                current_score, self.current_position)
            
            print(f"[{now.isoformat()}] Metrics updated: equity={self.account_equity:.2f}, GT-Score={current_score:.4f}")
        
        except Exception as e:
            print(f"  Warning: Could not update metrics: {e}")
    
    def run_loop(self):
        """Main trading loop."""
        print(f"Starting live trading loop (polling every {POLLING_INTERVAL}s)...\n")
        
        # Initialize live status in DB
        start_live_trading(self.strategy_id)
        
        last_close_date = None
        
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
                
                # Get latest close date
                current_close_date = candles['date'].iloc[-1].date()
                
                # Generate signals (use all available history for signal generation)
                try:
                    signals = self.strategy_func(candles, self.best_params)
                    latest_signal = signals.iloc[-1] if len(signals) > 0 else 0
                    latest_signal = int(latest_signal)
                except Exception as e:
                    print(f"  Error generating signal: {e}")
                    latest_signal = 0
                
                # Place order if signal changed
                if latest_signal != self.current_position:
                    self._place_order(latest_signal)
                
                # Track daily PnL (simplified)
                if last_close_date != current_close_date:
                    # Daily close event
                    if len(candles) > 1:
                        daily_return = (candles['close'].iloc[-1] - candles['close'].iloc[-2]) / candles['close'].iloc[-2]
                        position_return = self.current_position * daily_return
                        self.pnl_history.append(position_return)
                        
                        print(f"[{current_close_date.isoformat()}] Daily return: {daily_return:+.4f}, Position: {self.current_position:+d}, P&L: {position_return:+.4f}")
                    
                    last_close_date = current_close_date
                    self._update_metrics(force=True)
                else:
                    # Regular metric update
                    self._update_metrics()
                
                # Wait for next poll
                time.sleep(POLLING_INTERVAL)
        
        except KeyboardInterrupt:
            print(f"\n\n[{datetime.now().isoformat()}] Keyboard interrupt: stopping live trader")
            # Close any open position
            if self.current_position != 0:
                try:
                    self._place_order(0)
                except:
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
