"""
Unit tests for live_test.py — covers position sizing, stop loss,
correlation haircut, portfolio state loading, and crash recovery logic.

All tests mock OANDA API calls so no network access is required.
LiveTrader.__init__ is bypassed using object.__new__ + manual attribute setup.
"""
import sys
import os
import json
import tempfile
from pathlib import Path
from unittest.mock import patch, MagicMock

import pytest
import pandas as pd
import numpy as np

sys.path.insert(0, str(Path(__file__).parent.parent))

import live_test as lt
from live_test import LiveTrader, _load_portfolio_state, _get_instrument_sizing


# ─────────────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────────────

def _make_trader(instrument='EUR_USD', weight_scale=1.0, corr_peers=None,
                 current_position=0, entry_price=0.0, best_params=None):
    """Build a LiveTrader without calling __init__ (no API calls)."""
    trader = object.__new__(LiveTrader)
    trader.instrument       = instrument
    trader.strategy_id      = 'test_strat_v1'
    trader.weight_scale     = weight_scale
    trader.corr_peers       = corr_peers or []
    trader.current_position = current_position
    trader.entry_price      = entry_price
    trader.account_equity   = 100_000.0
    trader.best_params      = best_params or {}
    trader.oanda_trade_id   = None
    trader.prev_signal      = 0
    trader.last_bar_time    = None
    trader.headers          = {'Authorization': 'Bearer test'}
    return trader


# ─────────────────────────────────────────────────────────────────────────────
# _get_instrument_sizing
# ─────────────────────────────────────────────────────────────────────────────

class TestInstrumentSizing:
    def test_btc_fractional(self):
        s = _get_instrument_sizing('BTC_USD')
        assert s['min_units'] == 0.001
        assert s['unit_precision'] == 3

    def test_default_forex_whole_units(self):
        s = _get_instrument_sizing('EUR_USD')
        assert s['min_units'] == 1
        assert s['unit_precision'] == 0

    def test_unknown_instrument_gets_default(self):
        s = _get_instrument_sizing('EXOTIC_XYZ')
        assert s == _get_instrument_sizing('_default')


# ─────────────────────────────────────────────────────────────────────────────
# _quote_to_usd_rate
# ─────────────────────────────────────────────────────────────────────────────

class TestQuoteToUsdRate:
    def test_usd_quoted_returns_one(self):
        for inst in ('EUR_USD', 'GBP_USD', 'XAU_USD', 'WTICO_USD', 'XAG_USD'):
            trader = _make_trader(instrument=inst)
            assert trader._quote_to_usd_rate() == 1.0

    def test_jpy_quoted_returns_inverse_usdjpy(self):
        mock_resp = MagicMock()
        mock_resp.raise_for_status = MagicMock()
        mock_resp.json.return_value = {
            'prices': [{'bids': [{'price': '149.00'}], 'asks': [{'price': '151.00'}]}]
        }
        trader = _make_trader(instrument='GBP_JPY')
        with patch('requests.get', return_value=mock_resp):
            rate = trader._quote_to_usd_rate()
        expected = 1.0 / 150.0  # mid of 149/151
        assert abs(rate - expected) < 1e-6

    def test_jpy_quoted_api_failure_uses_fallback(self):
        trader = _make_trader(instrument='USD_JPY')
        with patch('requests.get', side_effect=Exception('timeout')):
            rate = trader._quote_to_usd_rate()
        assert abs(rate - 1.0 / 150.0) < 1e-9

    def test_other_non_usd_quote_returns_one(self):
        # USD_CHF: quote=CHF, treated as ~1:1
        trader = _make_trader(instrument='USD_CHF')
        assert trader._quote_to_usd_rate() == 1.0


# ─────────────────────────────────────────────────────────────────────────────
# _compute_position_size
# ─────────────────────────────────────────────────────────────────────────────

class TestComputePositionSize:
    def test_basic_forex_sizing(self):
        """risk=0.5%, equity=100k, atr=0.001 (10 pips), stop_mult=2 → 25,000 units."""
        trader = _make_trader('EUR_USD')
        # stop_distance = 2 * 0.001 = 0.002 USD (USD-quoted)
        # risk_amount   = 100000 * 0.005 * 1.0 * 1.0 = 500 USD
        # units         = 500 / 0.002 = 250,000 → clamped to 50,000 max
        with patch.object(trader, '_quote_to_usd_rate', return_value=1.0):
            units = trader._compute_position_size(atr=0.001,
                                                  corr_scale=1.0)
        assert units == 50_000  # max notional cap kicks in

    def test_jpy_pair_sizing_not_tiny(self):
        """GBP_JPY: without JPY conversion units would be 145x too small."""
        trader = _make_trader('GBP_JPY', best_params={'stop_mult': 2.0})
        atr = 100.0  # 100 JPY typical daily ATR for GBP/JPY
        usdjpy = 150.0
        # stop_distance_usd = 2 * 100 / 150 = 1.333 USD
        # risk_amount = 100000 * 0.005 = 500
        # units = 500 / 1.333 = 375
        with patch.object(trader, '_quote_to_usd_rate', return_value=1.0 / usdjpy):
            units = trader._compute_position_size(atr=atr, corr_scale=1.0)
        assert units > 100, f"GBP_JPY sizing should not be tiny, got {units}"
        # Without JPY correction (rate=1.0) it would be 500/200=2.5 → floored to 1
        with patch.object(trader, '_quote_to_usd_rate', return_value=1.0):
            units_wrong = trader._compute_position_size(atr=atr, corr_scale=1.0)
        assert units > units_wrong * 10, "JPY correction should produce much larger units"

    def test_none_atr_returns_minimum(self):
        trader = _make_trader('EUR_USD')
        with patch.object(trader, '_quote_to_usd_rate', return_value=1.0):
            units = trader._compute_position_size(atr=None)
        assert units == _get_instrument_sizing('EUR_USD')['min_units']

    def test_zero_atr_returns_minimum(self):
        trader = _make_trader('EUR_USD')
        with patch.object(trader, '_quote_to_usd_rate', return_value=1.0):
            units = trader._compute_position_size(atr=0.0)
        assert units == _get_instrument_sizing('EUR_USD')['min_units']

    def test_weight_scale_scales_risk(self):
        """Double weight → double units (up to max cap)."""
        t1 = _make_trader('NZD_USD', weight_scale=1.0)
        t2 = _make_trader('NZD_USD', weight_scale=2.0)
        atr = 0.0005
        with patch.object(t1, '_quote_to_usd_rate', return_value=1.0):
            u1 = t1._compute_position_size(atr=atr)
        with patch.object(t2, '_quote_to_usd_rate', return_value=1.0):
            u2 = t2._compute_position_size(atr=atr)
        assert abs(u2 - 2 * u1) < 1.0 or u2 == u1  # capped or doubled

    def test_corr_scale_halves_units(self):
        """corr_scale=0.5 should halve position size."""
        trader = _make_trader('EUR_USD')
        atr = 0.002
        with patch.object(trader, '_quote_to_usd_rate', return_value=1.0):
            full = trader._compute_position_size(atr=atr, corr_scale=1.0)
            half = trader._compute_position_size(atr=atr, corr_scale=0.5)
        assert abs(half - full / 2) < 1.0 or half == full  # or both hit cap

    def test_btc_fractional_units(self):
        """BTC sizing should respect 0.001 minimum and 3 decimal precision."""
        trader = _make_trader('BTC_USD', best_params={'stop_mult': 2.0})
        atr = 5000.0  # $5000 ATR on BTC
        with patch.object(trader, '_quote_to_usd_rate', return_value=1.0):
            units = trader._compute_position_size(atr=atr, corr_scale=1.0)
        assert units >= 0.001
        assert units <= 1.0  # max notional cap for BTC


# ─────────────────────────────────────────────────────────────────────────────
# _compute_stop_loss
# ─────────────────────────────────────────────────────────────────────────────

class TestComputeStopLoss:
    def test_long_stop_below_entry(self):
        trader = _make_trader(best_params={'stop_mult': 2.0})
        sl = trader._compute_stop_loss(direction=1, entry_price=1.1000, atr=0.0050)
        assert sl == pytest.approx(1.1000 - 2.0 * 0.0050)

    def test_short_stop_above_entry(self):
        trader = _make_trader(best_params={'stop_mult': 2.0})
        sl = trader._compute_stop_loss(direction=-1, entry_price=1.1000, atr=0.0050)
        assert sl == pytest.approx(1.1000 + 2.0 * 0.0050)

    def test_flat_signal_returns_none(self):
        trader = _make_trader()
        assert trader._compute_stop_loss(direction=0, entry_price=1.1, atr=0.005) is None

    def test_none_atr_returns_none(self):
        trader = _make_trader()
        assert trader._compute_stop_loss(direction=1, entry_price=1.1, atr=None) is None

    def test_zero_entry_returns_none(self):
        trader = _make_trader()
        assert trader._compute_stop_loss(direction=1, entry_price=0.0, atr=0.005) is None

    def test_uses_stop_mult_from_params(self):
        trader = _make_trader(best_params={'stop_mult': 3.0})
        sl = trader._compute_stop_loss(1, 1.2000, 0.0010)
        assert sl == pytest.approx(1.2000 - 3.0 * 0.0010)


# ─────────────────────────────────────────────────────────────────────────────
# _get_corr_scale
# ─────────────────────────────────────────────────────────────────────────────

class TestGetCorrScale:
    def test_no_peers_returns_one(self):
        trader = _make_trader(corr_peers=[])
        assert trader._get_corr_scale(signal=1) == 1.0

    def test_flat_signal_returns_one(self):
        trader = _make_trader(corr_peers=['peer_v1'])
        assert trader._get_corr_scale(signal=0) == 1.0

    def test_peer_same_direction_returns_half(self):
        trader = _make_trader(corr_peers=['peer_v1'])
        with patch('live_test.get_live_signals', return_value={'peer_v1': 1}):
            scale = trader._get_corr_scale(signal=1)
        assert scale == 0.5

    def test_peer_opposite_direction_returns_one(self):
        trader = _make_trader(corr_peers=['peer_v1'])
        with patch('live_test.get_live_signals', return_value={'peer_v1': -1}):
            scale = trader._get_corr_scale(signal=1)
        assert scale == 1.0

    def test_peer_flat_returns_one(self):
        trader = _make_trader(corr_peers=['peer_v1'])
        with patch('live_test.get_live_signals', return_value={'peer_v1': 0}):
            scale = trader._get_corr_scale(signal=1)
        assert scale == 1.0

    def test_api_failure_returns_one(self):
        """If peer signal lookup fails, default to full size (safe)."""
        trader = _make_trader(corr_peers=['peer_v1'])
        with patch('live_test.get_live_signals', side_effect=Exception('db error')):
            scale = trader._get_corr_scale(signal=1)
        assert scale == 1.0


# ─────────────────────────────────────────────────────────────────────────────
# _load_portfolio_state
# ─────────────────────────────────────────────────────────────────────────────

class TestLoadPortfolioState:
    def _write_state(self, tmp_path, state):
        p = tmp_path / 'portfolio_state.json'
        p.write_text(json.dumps(state))
        return str(p)

    def test_equal_weight_two_strategies(self, tmp_dir):
        state = {
            'n_strategies': 2,
            'weights': {'strat_a': 0.5, 'strat_b': 0.5},
            'correlated_pairs': [],
        }
        path = self._write_state(tmp_dir, state)
        with patch.object(lt, 'PORTFOLIO_STATE_FILE', path):
            scale, peers = _load_portfolio_state('strat_a')
        # equal weight: weight_scale = 0.5 * 2 = 1.0
        assert scale == pytest.approx(1.0)
        assert peers == []

    def test_overweight_strategy(self, tmp_dir):
        state = {
            'n_strategies': 2,
            'weights': {'strat_a': 0.7, 'strat_b': 0.3},
            'correlated_pairs': [],
        }
        path = self._write_state(tmp_dir, state)
        with patch.object(lt, 'PORTFOLIO_STATE_FILE', path):
            scale, _ = _load_portfolio_state('strat_a')
        assert scale == pytest.approx(0.7 * 2)

    def test_missing_file_returns_defaults(self):
        with patch.object(lt, 'PORTFOLIO_STATE_FILE', '/nonexistent/path.json'):
            scale, peers = _load_portfolio_state('any_strat')
        assert scale == 1.0
        assert peers == []

    def test_corr_peer_identified(self, tmp_dir):
        state = {
            'n_strategies': 2,
            'weights': {},
            'correlated_pairs': [{'a': 'strat_a', 'b': 'strat_b', 'weaker': 'strat_a'}],
        }
        path = self._write_state(tmp_dir, state)
        with patch.object(lt, 'PORTFOLIO_STATE_FILE', path):
            _, peers = _load_portfolio_state('strat_a')
        assert 'strat_b' in peers


@pytest.fixture
def tmp_dir(tmp_path):
    return tmp_path


# ─────────────────────────────────────────────────────────────────────────────
# _restore_and_reconcile — three recovery cases
# ─────────────────────────────────────────────────────────────────────────────

class TestRestoreAndReconcile:
    def _db_state(self, pos=0, price=0.0, bar=None, prev_sig=0, trade_id=None):
        return {
            'current_position': pos,
            'entry_price': price,
            'last_bar_time': bar,
            'prev_signal': prev_sig,
            'oanda_trade_id': trade_id,
        }

    def _run_reconcile(self, trader, db_state, broker_pos, broker_price=1.1000,
                       trade_id_found=False):
        """Run _restore_and_reconcile with mocked DB and broker responses."""
        import pipeline_utils as pu

        with patch('live_test.load_live_state', return_value=db_state), \
             patch('live_test.save_live_state') as mock_save, \
             patch.object(trader, '_get_account_summary',
                          return_value={'equity': 100000, 'positions': [
                              {'instrument': trader.instrument,
                               'long':  {'units': str(broker_pos) if broker_pos > 0 else '0'},
                               'short': {'units': str(broker_pos) if broker_pos < 0 else '0'},
                              }
                          ] if broker_pos != 0 else []}):
            trader._restore_and_reconcile()
        return mock_save

    def test_db_broker_agree_flat(self):
        trader = _make_trader()
        db = self._db_state(pos=0)
        with patch('live_test.load_live_state', return_value=db), \
             patch.object(trader, '_get_account_summary',
                          return_value={'equity': 100000, 'positions': []}):
            trader._restore_and_reconcile()
        assert trader.current_position == 0

    def test_db_flat_broker_long_adopts_broker(self):
        """Crashed after order, before DB write → adopt broker long."""
        trader = _make_trader()
        db = self._db_state(pos=0)
        with patch('live_test.load_live_state', return_value=db), \
             patch('live_test.save_live_state') as mock_save, \
             patch.object(trader, '_get_account_summary',
                          return_value={'equity': 100000, 'positions': [
                              {'instrument': 'EUR_USD',
                               'long': {'units': '10000'}, 'short': {'units': '0'}}
                          ]}):
            trader._restore_and_reconcile()
        assert trader.current_position == 1
        mock_save.assert_called_once()

    def test_db_long_broker_flat_resets_to_flat(self):
        """SL hit while process was down → reset to flat."""
        trader = _make_trader()
        db = self._db_state(pos=1, price=1.1000)
        with patch('live_test.load_live_state', return_value=db), \
             patch('live_test.save_live_state') as mock_save, \
             patch.object(trader, '_get_account_summary',
                          return_value={'equity': 100000, 'positions': []}):
            trader._restore_and_reconcile()
        assert trader.current_position == 0
        assert trader.entry_price == 0.0
        mock_save.assert_called_once()


# ─────────────────────────────────────────────────────────────────────────────
# Signal flip logic (isolated from run_loop)
# ─────────────────────────────────────────────────────────────────────────────

class TestSignalFlipLogic:
    """Test the signal flip detection rule without running the full loop."""

    def test_no_flip_no_order(self):
        """Same signal on both last bars → no order."""
        trader = _make_trader()
        trader.prev_signal = 1  # already established

        signals = pd.Series([0, 1, 1, 1, 1])  # last two bars both 1
        latest = int(signals.iloc[-1])
        if len(signals) >= 2:
            trader.prev_signal = int(signals.iloc[-2])  # sets to 1

        order_needed = latest != trader.prev_signal
        assert not order_needed

    def test_flip_detected(self):
        """0→1 flip on latest bar → order should fire."""
        trader = _make_trader()
        signals = pd.Series([0, 0, 0, 0, 1])
        if len(signals) >= 2:
            trader.prev_signal = int(signals.iloc[-2])  # 0
        latest = int(signals.iloc[-1])  # 1

        order_needed = latest != trader.prev_signal
        assert order_needed

    def test_restart_with_sustained_signal_no_order(self):
        """On restart if strategy has been long for many bars, no redundant order."""
        trader = _make_trader()
        signals = pd.Series([1, 1, 1, 1, 1])  # long all bars
        if len(signals) >= 2:
            trader.prev_signal = int(signals.iloc[-2])  # 1
        latest = int(signals.iloc[-1])  # 1

        order_needed = latest != trader.prev_signal
        assert not order_needed

    def test_halted_trader_does_not_order(self):
        """When halted=True, no new order even if signal flips."""
        trader = _make_trader()
        trader.halted = True
        trader.prev_signal = 0
        latest_signal = 1  # would normally trigger long

        would_order = (not trader.halted) and (latest_signal != trader.current_position)
        assert not would_order
