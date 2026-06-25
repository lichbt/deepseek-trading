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
import re
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
# Poll cadence per timeframe — roughly half the bar duration, so a new bar is
# picked up promptly after it closes. A fixed hourly poll skipped every other
# M30 bar and lagged H1; daily/weekly are unchanged at hourly.
DEFAULT_POLL_INTERVAL = 3600
POLL_INTERVAL_BY_TF = {
    'M30':  900,    # 15 min  (bar = 30 min)
    'H1':  1800,    # 30 min  (bar = 1 h)
    'H4':  3600,    # 1 h     (bar = 4 h)
    'D':   3600,    # 1 h     (bar = 1 day)
    'W':   3600,    # 1 h     (bar = 1 week)
}
RISK_PER_TRADE = 0.005   # Risk 0.5% of equity per trade (baseline, scaled by portfolio weight)
PORTFOLIO_STATE_FILE = os.path.join(os.path.dirname(__file__), "portfolio_state.json")
DEFAULT_STOP_MULT = 2.0  # ATR multiplier for stop loss
# Max price drift (in ATRs) from the signal-bar close that a delayed entry will
# still accept. Daily orders fire at the 21:00 UTC bar close, which collides with
# OANDA's maintenance window (MARKET_HALTED); we retry on later polls but skip the
# fill if price has run more than this many ATRs — don't chase a moved market.
ENTRY_DRIFT_ATR = float(os.getenv('ENTRY_DRIFT_ATR', '1.0'))
# While an entry is pending (rejected at the bar close by OANDA's brief
# maintenance), poll this fast so it fills within minutes of reopen — near the
# signal-bar price the strategy was validated at — instead of a full poll_interval
# (1 h for daily) later, by which point drift often exceeds ENTRY_DRIFT_ATR and the
# trade is skipped. Only applies while an entry is pending.
PENDING_RETRY_INTERVAL = int(os.getenv('PENDING_RETRY_INTERVAL', '300'))  # 5 min
MAX_WEIGHT_SCALE  = 3.0  # Cap on weight_scale to prevent runaway risk if
                         # portfolio.py writes bad weights (e.g. concentration
                         # of >3x equal-weight on one strategy)
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
        # Safety clamp: if weights don't sum to 1.0 (portfolio.py bug or stale
        # state file) weight_scale can balloon. Cap at MAX_WEIGHT_SCALE so risk
        # never exceeds 3× the baseline regardless of upstream state.
        weight_scale = max(0.0, min(MAX_WEIGHT_SCALE, weight_scale))

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


# ---------------------------------------------------------------------------
# Netting: multiple sleeves on ONE instrument.
# OANDA nets per instrument, so same-instrument sleeves collide — only the first
# to fire opens a trade and the rest adopt its net position (losing their own
# conviction sizing). With NETTING on, each sleeve tracks its OWN signed units
# and orders only ITS OWN delta; the broker holds the running sum, so a sleeve's
# exit removes only its own share, never another's. Stops become software (the
# netted position can't carry per-sleeve broker stops). Default OFF — flip with
# NETTING=1 after a watched cutover.
# ---------------------------------------------------------------------------
NETTING_ENABLED = os.environ.get('NETTING', '0') != '0'
_NETTING_DB = os.path.join(os.path.dirname(__file__), 'pipeline.db')


def netting_delta(own_units, target_units):
    """Signed units THIS sleeve must send so it moves own_units -> target_units.
    The broker is the running sum across sleeves, so a sleeve only ever orders
    its own change (a pure function, for the test)."""
    return float(target_units) - float(own_units)


def _load_own_units(sleeve_id):
    """(own_units, stop_price), persisted across restarts — a netted sleeve can't
    recover its own share from the broker (the broker only knows the NET)."""
    import sqlite3
    try:
        con = sqlite3.connect(_NETTING_DB)
        con.execute("CREATE TABLE IF NOT EXISTS sleeve_units(sleeve_id TEXT PRIMARY KEY, units REAL, stop REAL)")
        r = con.execute("SELECT units, stop FROM sleeve_units WHERE sleeve_id=?", (sleeve_id,)).fetchone()
        con.close()
        return (float(r[0]), r[1]) if r else (0.0, None)
    except Exception:
        return 0.0, None


def _save_own_units(sleeve_id, units, stop):
    import sqlite3
    try:
        con = sqlite3.connect(_NETTING_DB)
        con.execute("CREATE TABLE IF NOT EXISTS sleeve_units(sleeve_id TEXT PRIMARY KEY, units REAL, stop REAL)")
        con.execute("INSERT OR REPLACE INTO sleeve_units VALUES (?,?,?)", (sleeve_id, float(units), stop))
        con.commit(); con.close()
    except Exception as e:
        print(f"  [netting] persist failed: {e}", flush=True)


def order_decision(latest_signal: int, prev_signal: int, current_position: int,
                   halted: bool, startup_pending: bool):
    """Decide what the trading loop does with a freshly computed signal.

    Returns 'flip', 'align', or None.

    'flip'  — the signal changed on this bar: the normal trade path.
    'align' — FIRST successful evaluation after process start only: the
              strategy's signal disagrees with the actual broker position.
              Happens when a sleeve is deployed (or respawned) while its
              strategy is mid-position — no flip occurs for up to
              max_holding bars, so the sleeve silently diverges from its
              validation (2026-06-10 wheat finding: reconstruction short
              +4.3% while the live trader sat flat for 12 days).
    None    — no action. Crucially this includes the MID-RUN persistent-
              signal case where the position is flat because the ATR stop
              fired: the validated return stream models a fired stop as
              flat-until-the-signal-changes, so re-entering mid-run would
              diverge from validation. Startup is the only safe alignment
              moment.
    """
    if latest_signal != prev_signal:
        return 'flip'
    if startup_pending and not halted and latest_signal != current_position:
        return 'align'
    return None


def entry_retry_decision(pending, current_bar_time, price_now, drift_atr):
    """Decide what to do with a PENDING entry (one the broker rejected, e.g.
    MARKET_HALTED at the bar close) on a poll. Pure — no I/O.

    Returns:
      'expire' — the bar has rolled; the daily signal is stale, drop it.
      'skip'   — price has drifted > drift_atr ATRs from the signal close;
                 don't chase a market that already moved.
      'retry'  — within the same bar and price still near the signal; place now
                 (fills if the market has reopened, re-rejects if still halted).
      'wait'   — nothing pending, or price not available yet; try next poll.
    """
    if pending is None:
        return 'wait'
    if current_bar_time != pending.get('bar_time'):
        return 'expire'
    if price_now is None:
        return 'wait'
    atr = pending.get('atr')
    if atr and abs(price_now - pending['signal_price']) > drift_atr * atr:
        return 'skip'
    return 'retry'


def _infer_archetype(code: str, declared: str = 'standard') -> str:
    """Derive the strategy archetype from the columns the code actually
    references — mirrors auto_research._infer_archetype. The strategies table
    doesn't persist archetype, so the live runner needs to recover it from the
    code; otherwise inject_supplementary_data is skipped and a macro strategy
    KeyErrors on df['dxy'] (etc.) the moment it computes a signal.
    """
    from macro_fetcher import ALL_MACRO_COLS
    refs = set(re.findall(r'df\[["\'](\w+)["\']\]', code or ''))
    if refs & ALL_MACRO_COLS:
        return 'macro'
    if 'session' in refs:
        return 'session'
    if refs & {'event_impact', 'event_surprise'}:
        return 'news'
    if 'close_leg2' in refs:
        return 'pair'
    if 'spread' in refs:
        return 'spread'
    return declared or 'standard'


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
        # Recover archetype from the code so live signal-gen injects the same
        # supplementary columns (macro/session/news/pair) the validator did.
        # Without this a macro strategy KeyErrors on every bar and produces
        # no trades. instrument2 is not derivable from code — if a pair strategy
        # ever passes, add the column to the strategies table and load it here.
        self.archetype   = _infer_archetype(self.code)
        self.instrument2 = strat.get('instrument2')
        # Poll cadence matched to the timeframe so intraday bars aren't missed.
        self.poll_interval = POLL_INTERVAL_BY_TF.get(self.timeframe, DEFAULT_POLL_INTERVAL)

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
        if NETTING_ENABLED:
            # The broker only knows the per-instrument NET, not this sleeve's
            # share, so a netted sleeve can't reconcile from the broker (same
            # reason the loop skips _reconcile_with_broker). Trust persisted
            # state + own_units; a flat sleeve must NOT adopt a same-instrument
            # peer's netted position.
            self.current_position = saved['current_position']
            self.entry_price      = saved['entry_price'] or 0.0
            self.prev_signal      = saved['prev_signal']
            self.oanda_trade_id   = None
            _raw = saved['last_bar_time']
            try:
                self.last_bar_time = pd.to_datetime(_raw) if _raw else None
            except Exception:
                self.last_bar_time = None
            self.own_units, self.stop_price = _load_own_units(self.strategy_id)
            print(f"[Recovery] {self.strategy_id}: netting pos={self.current_position} "
                  f"own_units={self.own_units}", flush=True)
            return
        db_pos      = saved['current_position']
        db_price    = saved['entry_price'] or 0.0
        # last_bar_time is stored as an ISO string; coerce to Timestamp so the
        # main-loop comparison `last_bar_time != current_bar_time` (which is a
        # pandas Timestamp) doesn't always read as "new bar" on the first tick
        # after restart and re-evaluate the same bar.
        _raw_bar = saved['last_bar_time']
        if _raw_bar:
            try:
                db_bar = pd.to_datetime(_raw_bar)
            except Exception:
                db_bar = None
        else:
            db_bar = None
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

    # OANDA pairs used to convert non-USD quote currencies to USD.
    # invert=True  → rate = 1 / mid(pair)     (e.g. JPY uses 1/USD_JPY)
    # invert=False → rate =     mid(pair)     (e.g. GBP uses GBP_USD)
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

    def _quote_to_usd_rate(self) -> float:
        """
        Return the value of 1 unit of the instrument's quote currency in USD.

        Without this conversion, position sizing for non-USD-quoted pairs is
        off by the FX rate (e.g. GBP_JPY sized in JPY would be 145× too
        small, EUR_GBP sized in GBP would be ~25% too large).

        For USD-quoted pairs: 1.0
        For pairs in _QUOTE_TO_USD_PAIR: fetch live mid-price, invert if needed.
        For anything else: 1.0 (conservative — better undersized than blowing up).
        """
        parts = self.instrument.split('_')
        if len(parts) < 2:
            return 1.0
        quote = parts[1].upper()
        if quote == 'USD':
            return 1.0

        info = self._QUOTE_TO_USD_PAIR.get(quote)
        if info is None:
            return 1.0
        pair, invert, fallback_rate = info

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
            print(f"  [Sizing] Could not fetch {pair} rate: {e} — using fallback")
        return fallback_rate

    def _get_current_price(self) -> Optional[float]:
        """Live mid price for this instrument, for the entry-drift check.
        Returns None on failure (caller waits and retries on the next poll)."""
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
            print(f"  [drift-check] price fetch failed: {e}")
        return None

    def _compute_position_size(self, atr: Optional[float], corr_scale: float = 1.0) -> float:
        """
        Compute position size using percent-risk model, scaled by portfolio weight
        and an optional correlation haircut. Returns float to support fractional
        units (e.g. BTC min lot = 0.001).

        risk_amount = equity * RISK_PER_TRADE * weight_scale * corr_scale
        stop_distance_usd = stop_mult * atr * quote_to_usd_rate
        units = risk_amount / stop_distance_usd

        quote_to_usd_rate converts the stop distance from the pair's quote currency
        (e.g. JPY for GBP_JPY) into USD so the risk calculation is always in dollars.
        Without this, GBP_JPY position sizes are ~145x too small.
        """
        sizing = _get_instrument_sizing(self.instrument)
        min_u = sizing['min_units']
        max_u = _INSTRUMENT_MAX_NOTIONAL.get(self.instrument,
                _INSTRUMENT_MAX_NOTIONAL['_default'])

        # NaN passes through `<= 0` (NaN comparisons return False), so check
        # explicitly. Happens when atr_window > rolling candle history.
        if atr is None or pd.isna(atr) or atr <= 0:
            return min_u
        stop_mult = self.best_params.get('stop_mult', DEFAULT_STOP_MULT)
        stop_distance = stop_mult * atr
        if stop_distance <= 0 or pd.isna(stop_distance):
            return min_u

        # Convert stop_distance from quote currency to USD
        stop_distance_usd = stop_distance * self._quote_to_usd_rate()

        effective_risk = RISK_PER_TRADE * self.weight_scale * corr_scale
        risk_amount = self.account_equity * effective_risk
        units = risk_amount / stop_distance_usd
        return float(np.clip(units, min_u, max_u))

    def _compute_stop_loss(self, direction: int, entry_price: float, atr: Optional[float]) -> Optional[float]:
        """Compute ATR-based stop loss from entry."""
        if atr is None or pd.isna(atr) or atr <= 0 or entry_price <= 0:
            return None
        stop_mult = self.best_params.get('stop_mult', DEFAULT_STOP_MULT)
        if direction == 1:
            return entry_price - stop_mult * atr
        if direction == -1:
            return entry_price + stop_mult * atr
        return None

    def _get_account_summary(self) -> Dict:
        """Fetch current account details from Oanda.

        Uses NAV (= balance + unrealizedPL), not balance, so position sizing
        reflects true equity while a trade is open. balance only changes on
        trade close, so using it leaves account_equity stale by the entire
        open P&L between entry and exit.
        """
        try:
            url = f'{OANDA_BASE_URL}/v3/accounts/{OANDA_ACCOUNT_ID}'
            response = requests.get(url, headers=self.headers, timeout=5)
            response.raise_for_status()
            data = response.json()
            account = data['account']
            # Prefer NAV when present; fall back to balance for any account
            # response that omits it (shouldn't happen with v3 but defensive).
            equity = float(account.get('NAV', account.get('balance', 0.0)))
            return {
                'equity': equity,
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
        if NETTING_ENABLED:
            return self._place_order_netting(signal, entry_price, atr)
        if signal == self.current_position:
            return True

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
                # The close may have succeeded at the broker even if response
                # parsing failed locally — reconcile so we don't double-trade
                # on the next iteration.
                print(f"  Error closing position: {e}")
                self._reconcile_with_broker()
                return False

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
                # POST might have succeeded before the exception — query broker
                # to find out whether a position was actually opened, and adopt
                # whatever state is real instead of optimistically setting flat.
                print(f"  Error opening position: {e}")
                self.current_position = 0
                self.entry_price = 0.0
                self.oanda_trade_id = None
                self._reconcile_with_broker()

        # Persist state immediately after any order so a crash doesn't lose it
        save_live_state(
            self.strategy_id,
            self.current_position,
            self.entry_price,
            getattr(self, 'last_bar_time', None),
            self.prev_signal,
            self.oanda_trade_id,
        )
        # True iff we ended in the position we wanted. False when an open was
        # rejected (e.g. MARKET_HALTED) — the caller keeps the entry pending.
        return self.current_position == signal

    def _place_order_netting(self, signal: int, entry_price: float, atr) -> bool:
        """NETTING mode: send only THIS sleeve's delta; the broker accumulates the
        per-instrument net. No broker stop (software stop is checked per bar in
        the loop). own_units/stop_price persist so they survive restart."""
        target_units = 0.0
        stop = None
        if signal != 0:
            corr_scale = self._get_corr_scale(signal)
            size = self._compute_position_size(atr, corr_scale=corr_scale)
            target_units = signal * size
            stop = self._compute_stop_loss(signal, entry_price, atr)
        own = getattr(self, 'own_units', 0.0)
        delta = netting_delta(own, target_units)
        if abs(delta) < 1e-9:
            self.current_position = signal
            return True
        try:
            self._execute_order(units=delta, comment=f'net_{self.strategy_id}', stop_loss=None)
        except Exception as e:
            print(f"  Error (netting) sending Δ{delta:+.4f}: {e}", flush=True)
            return False   # own_units unchanged -> pending-entry retry re-sends
        self.own_units = target_units
        self.current_position = signal
        self.entry_price = entry_price if signal != 0 else 0.0
        self.stop_price = stop
        _save_own_units(self.strategy_id, self.own_units, self.stop_price)
        print(f"[{datetime.now().isoformat()}] Netting Δ{delta:+.4f} -> own_units={self.own_units:+.4f} sig={signal:+d}", flush=True)
        return True

    def _execute_order(self, units: float, comment: str, stop_loss: float = None) -> Optional[str]:
        """Execute market order via Oanda API.

        Returns OANDA trade ID if a new trade was opened (None for close
        orders that just zero out an existing position).

        Raises RuntimeError if OANDA cancels the order outright.

        Also emits a loud warning if a stop-loss was requested but rejected —
        in that case the trade still filled, leaving an unprotected position.
        """
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

        # Parse trade ID from fill response (present only when a new trade was opened)
        data     = response.json()
        fill_txn = data.get('orderFillTransaction', {})
        opened   = fill_txn.get('tradeOpened', {})
        trade_id = opened.get('tradeID')  # None for close orders or cancelled FOK orders

        # Detect FOK cancellation (market closed / no liquidity) — order not filled
        if trade_id is None and data.get('orderCancelTransaction'):
            reason = data['orderCancelTransaction'].get('reason', 'UNKNOWN')
            raise RuntimeError(f'Order cancelled by OANDA (reason={reason}) — market may be closed')

        # Detect SL rejection: order filled but the dependent stopLoss did NOT.
        # This leaves an open position with no protective stop — operator must know.
        if stop_loss is not None and data.get('stopLossOrderRejectTransaction'):
            sl_reject = data['stopLossOrderRejectTransaction']
            reason = sl_reject.get('reason', 'UNKNOWN')
            print(f"  [WARN] Stop-loss REJECTED by OANDA (reason={reason}) — "
                  f"position open WITHOUT stop loss! trade_id={trade_id}", flush=True)
            try:
                from telegram_bot import notify
                notify(
                    f'⚠️ <b>SL REJECTED</b> for {self.strategy_id}\n'
                    f'Instrument: {self.instrument}  trade_id={trade_id}\n'
                    f'Reason: {reason}\n'
                    f'<b>Position is open without a stop loss.</b>'
                )
            except Exception:
                pass

        return trade_id

    def _reconcile_with_broker(self):
        """Re-query the broker and adopt its position as truth.

        Called after any order-path exception so a half-completed order
        (POST succeeded but response parsing failed, or close errored after
        the trade actually closed) can't leave local state out of sync.
        """
        try:
            summary = self._get_account_summary()
        except Exception as e:
            print(f"  [Reconcile] Broker query failed: {e} — keeping local state", flush=True)
            return

        broker_pos = 0
        for p in summary.get('positions', []):
            if p.get('instrument') == self.instrument:
                long_u  = float((p.get('long')  or {}).get('units', 0) or 0)
                short_u = float((p.get('short') or {}).get('units', 0) or 0)
                cu = long_u + short_u
                broker_pos = 1 if cu > 0 else (-1 if cu < 0 else 0)
                break

        if broker_pos != self.current_position:
            print(f"  [Reconcile] local={self.current_position} broker={broker_pos} — "
                  f"adopting broker state", flush=True)
            self.current_position = broker_pos
            if broker_pos == 0:
                self.entry_price = 0.0
                self.oanda_trade_id = None
            try:
                save_live_state(
                    self.strategy_id,
                    self.current_position,
                    self.entry_price,
                    getattr(self, 'last_bar_time', None),
                    self.prev_signal,
                    self.oanda_trade_id,
                )
            except Exception as e:
                print(f"  [Reconcile] save_live_state failed: {e}", flush=True)
    
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
        print(f"Starting live trading loop ({self.timeframe} bars, "
              f"polling every {self.poll_interval}s)...\n")
        
        # Initialize live status in DB on first launch
        if get_strategy_by_id(self.strategy_id).get('status') == 'passed':
            start_live_trading(self.strategy_id)
        
        # Resume from last processed bar (loaded from DB by _restore_and_reconcile)
        last_bar_time = getattr(self, 'last_bar_time', None)

        # One-time startup alignment window: consumed on the first SUCCESSFUL
        # signal evaluation after launch (see order_decision for why mid-run
        # alignment would be wrong after a stop-out).
        startup_alignment_pending = True
        # An entry rejected by the broker (e.g. MARKET_HALTED at the daily-close
        # maintenance window) is held here and retried on later polls until it
        # fills, the bar rolls (stale), or price drifts > ENTRY_DRIFT_ATR.
        pending_entry = None
        if NETTING_ENABLED:
            self.own_units, self.stop_price = _load_own_units(self.strategy_id)

        try:
            while True:
                # Fetch recent candles
                try:
                    candles = self._fetch_candles()
                    if len(candles) == 0:
                        print(f"[{datetime.now().isoformat()}] No candles yet")
                        time.sleep(self.poll_interval)
                        continue
                except Exception as e:
                    print(f"[{datetime.now().isoformat()}] Error fetching candles: {e}")
                    time.sleep(self.poll_interval)
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

                # Retry an entry the broker rejected at the bar close (market was
                # halted). Runs every poll so it fills as soon as the market
                # reopens; expires when the bar rolls; skips if price ran away.
                if pending_entry is not None:
                    price_now = self._get_current_price()
                    action = entry_retry_decision(
                        pending_entry, current_bar_time, price_now, ENTRY_DRIFT_ATR)
                    if action == 'expire':
                        print(f"[{current_bar_time}] Pending {pending_entry['signal']:+d} entry expired (bar rolled)")
                        pending_entry = None
                    elif action == 'skip':
                        drift = abs(price_now - pending_entry['signal_price']) / pending_entry['atr']
                        print(f"[{current_bar_time}] Pending {pending_entry['signal']:+d} entry skipped — "
                              f"price drifted {drift:.2f}×ATR > {ENTRY_DRIFT_ATR}×ATR (not chasing)")
                        pending_entry = None
                    elif action == 'retry':
                        if self._place_order(pending_entry['signal'], price_now, pending_entry['atr']):
                            print(f"[{current_bar_time}] Pending {pending_entry['signal']:+d} entry FILLED on retry @ {price_now}")
                            pending_entry = None
                        # else still halted — keep pending, retry next poll

                # Only act when a new completed bar has arrived
                if last_bar_time != current_bar_time:
                    # Sync with the broker before anything else. A stop-loss can
                    # fire between bars; run_loop otherwise never re-queries the
                    # broker, so self.current_position would stay stale until the
                    # next restart. Both the PnL/circuit-breaker calc and the
                    # signal-flip logic below depend on an accurate position.
                    # In NETTING mode the broker only knows the per-instrument net
                    # (not this sleeve's share), so we trust our persisted own_units
                    # instead of adopting the broker net.
                    if not NETTING_ENABLED:
                        self._reconcile_with_broker()

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
                    signal_ok = False
                    try:
                        # Inject archetype-specific columns (macro rates/yields,
                        # session labels, news, pair spread) — must match what
                        # the validator injected, or the strategy KeyErrors.
                        signal_df = candles
                        if self.archetype and self.archetype != 'standard':
                            try:
                                from supplementary_data import inject_supplementary_data
                                signal_df = inject_supplementary_data(
                                    candles, self.archetype, self.instrument,
                                    self.instrument2, None, None, self.timeframe,
                                )
                            except Exception as inj_e:
                                print(f"  Supplementary-data injection failed "
                                      f"({self.archetype}): {inj_e}")
                                latest_signal = self.prev_signal
                                raise
                        signals = self.strategy_func(signal_df, self.best_params)
                        latest_signal = int(signals.iloc[-1]) if len(signals) > 0 else 0
                        signal_ok = True
                        # Use signal from the PREVIOUS candle as the true prior signal.
                        # This avoids false flips on restart: if the strategy was already
                        # signaling 1 two bars ago and still signals 1 now, no order fires.
                        # prev_signal from DB is only a fallback when candle history is too short.
                        if len(signals) >= 2:
                            self.prev_signal = int(signals.iloc[-2])
                    except Exception as e:
                        print(f"  Error generating signal: {e}")
                        latest_signal = self.prev_signal  # hold last known signal on error

                    # Netting software stop: a netted position can't carry a
                    # per-sleeve broker stop, so enforce it here. If THIS bar
                    # breached our stop, force flat -> the flip path sends our
                    # closing delta (only our share, not the whole net).
                    if NETTING_ENABLED and getattr(self, 'own_units', 0.0) != 0 and getattr(self, 'stop_price', None):
                        bar_low = float(candles['low'].iloc[-1]); bar_high = float(candles['high'].iloc[-1])
                        if (self.own_units > 0 and bar_low <= self.stop_price) or \
                           (self.own_units < 0 and bar_high >= self.stop_price):
                            print(f"[{current_bar_time}] Netting software stop @ {self.stop_price} hit -> flat", flush=True)
                            latest_signal = 0

                    # Trade on signal flips — plus a ONE-TIME startup alignment
                    # when the strategy is already mid-position at launch (no
                    # flip will occur for up to max_holding bars, so the sleeve
                    # silently diverges from its validation; see order_decision).
                    decision = order_decision(
                        latest_signal, self.prev_signal, self.current_position,
                        self.halted, startup_alignment_pending and signal_ok,
                    )
                    if signal_ok:
                        startup_alignment_pending = False
                    if decision is not None:
                        if decision == 'align':
                            print(f"[{current_bar_time}] Startup alignment: strategy signal "
                                  f"{latest_signal:+d} vs broker position {self.current_position:+d} "
                                  f"— opening to match validation")
                        else:
                            print(f"[{current_bar_time}] Signal flip: {self.prev_signal:+d} → {latest_signal:+d}")
                        self.prev_signal = latest_signal
                        # Publish signal to DB so correlated peers can see it
                        try:
                            update_live_signal(self.strategy_id, latest_signal)
                        except Exception:
                            pass
                        if not self.halted and latest_signal != self.current_position:
                            entry_price = float(candles['close'].iloc[-1])
                            filled = self._place_order(latest_signal, entry_price, atr)
                            if not filled and latest_signal != 0:
                                # Broker rejected the open (likely MARKET_HALTED at
                                # the bar close) — keep it pending and retry on later
                                # polls until it fills / drifts / the bar rolls.
                                pending_entry = {'signal': latest_signal,
                                                 'signal_price': entry_price,
                                                 'bar_time': current_bar_time,
                                                 'atr': atr}
                                print(f"[{current_bar_time}] Entry {latest_signal:+d} not filled "
                                      f"(market likely closed) — pending retry until filled / >{ENTRY_DRIFT_ATR}×ATR drift")
                            else:
                                pending_entry = None
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
                
                # Wait for next poll — but retry a pending (maintenance-halted)
                # entry fast so it fills near the signal price, not an hour late.
                time.sleep(PENDING_RETRY_INTERVAL if pending_entry is not None
                           else self.poll_interval)
        
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
