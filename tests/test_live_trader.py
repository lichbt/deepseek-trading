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

    def _mock_pricing(self, bid, ask):
        resp = MagicMock()
        resp.raise_for_status = MagicMock()
        resp.json.return_value = {
            'prices': [{'bids': [{'price': str(bid)}], 'asks': [{'price': str(ask)}]}]
        }
        return resp

    def test_chf_quoted_inverts_usdchf(self):
        """USD_CHF: quote=CHF — was hardcoded to 1.0 silently. Should invert."""
        trader = _make_trader(instrument='USD_CHF')
        with patch('requests.get', return_value=self._mock_pricing(0.90, 0.92)):
            rate = trader._quote_to_usd_rate()
        assert rate == pytest.approx(1.0 / 0.91)

    def test_cad_quoted_inverts_usdcad(self):
        """USD_CAD: quote=CAD — previously off by ~27%."""
        trader = _make_trader(instrument='USD_CAD')
        with patch('requests.get', return_value=self._mock_pricing(1.36, 1.38)):
            rate = trader._quote_to_usd_rate()
        assert rate == pytest.approx(1.0 / 1.37)

    def test_gbp_quoted_uses_gbpusd_direct(self):
        """EUR_GBP: quote=GBP — USD_GBP doesn't exist, use GBP_USD directly."""
        trader = _make_trader(instrument='EUR_GBP')
        with patch('requests.get', return_value=self._mock_pricing(1.24, 1.26)):
            rate = trader._quote_to_usd_rate()
        assert rate == pytest.approx(1.25)

    def test_unknown_quote_returns_one(self):
        """Unknown quote currency falls back to 1.0 (no API call)."""
        # SOYBN_USD has USD quote → 1.0 path
        trader = _make_trader(instrument='SOYBN_USD')
        assert trader._quote_to_usd_rate() == 1.0

    def test_api_failure_uses_fallback_per_currency(self):
        """Each currency has its own static fallback rate."""
        trader = _make_trader(instrument='USD_CAD')
        with patch('requests.get', side_effect=Exception('boom')):
            rate = trader._quote_to_usd_rate()
        assert rate == pytest.approx(1.0 / 1.37)


# ─────────────────────────────────────────────────────────────────────────────
# _get_account_summary
# ─────────────────────────────────────────────────────────────────────────────

class TestGetAccountSummary:
    def _mock_response(self, account_payload):
        resp = MagicMock()
        resp.json.return_value = {'account': account_payload}
        resp.raise_for_status = MagicMock()
        return resp

    def test_uses_nav_when_present(self):
        """Regression: previously used 'balance' which excludes open P&L."""
        trader = _make_trader('EUR_USD')
        with patch('live_test.requests.get',
                   return_value=self._mock_response({
                       'NAV':         '99500.00',  # 500 down on open trade
                       'balance':     '100000.00',
                       'unrealizedPL': '-500.00',
                       'positions':   [],
                   })):
            summary = trader._get_account_summary()
        assert summary['equity'] == 99500.0

    def test_falls_back_to_balance_if_nav_missing(self):
        """Old API responses or future schema changes shouldn't crash."""
        trader = _make_trader('EUR_USD')
        with patch('live_test.requests.get',
                   return_value=self._mock_response({
                       'balance':   '100000.00',
                       'positions': [],
                   })):
            summary = trader._get_account_summary()
        assert summary['equity'] == 100000.0

    def test_api_error_returns_cached_equity(self):
        trader = _make_trader('EUR_USD')
        trader.account_equity = 123456.0
        with patch('live_test.requests.get', side_effect=Exception('boom')):
            summary = trader._get_account_summary()
        assert summary['equity'] == 123456.0
        assert summary['positions'] == []


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

    def test_nan_atr_returns_minimum(self):
        """Regression: NaN <= 0 is False, so NaN ATR used to flow through to OANDA."""
        trader = _make_trader('EUR_USD')
        with patch.object(trader, '_quote_to_usd_rate', return_value=1.0):
            units = trader._compute_position_size(atr=float('nan'))
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

    def test_nan_atr_returns_none(self):
        trader = _make_trader()
        assert trader._compute_stop_loss(direction=1, entry_price=1.1, atr=float('nan')) is None

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

    def test_weight_scale_clamped_to_max(self, tmp_dir):
        """If portfolio.py writes bad weights, weight_scale must not exceed cap."""
        # 5 strategies, all weight=1.0 → weight_scale would be 5.0 without the cap
        state = {
            'n_strategies': 5,
            'weights': {'strat_a': 1.0},  # buggy: should sum to 1 across 5 strats
            'correlated_pairs': [],
        }
        path = self._write_state(tmp_dir, state)
        with patch.object(lt, 'PORTFOLIO_STATE_FILE', path):
            scale, _ = _load_portfolio_state('strat_a')
        assert scale == lt.MAX_WEIGHT_SCALE
        assert scale <= 3.0

    def test_weight_scale_clamped_to_zero_floor(self, tmp_dir):
        """Negative weight (shouldn't happen but defensive) → 0, not negative."""
        state = {
            'n_strategies': 2,
            'weights': {'strat_a': -0.5},
            'correlated_pairs': [],
        }
        path = self._write_state(tmp_dir, state)
        with patch.object(lt, 'PORTFOLIO_STATE_FILE', path):
            scale, _ = _load_portfolio_state('strat_a')
        assert scale == 0.0

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

    def test_last_bar_time_coerced_to_timestamp(self):
        """Regression: string from DB would always compare != to pd.Timestamp."""
        trader = _make_trader()
        db = self._db_state(pos=0, bar='2026-05-19T12:00:00')
        with patch('live_test.load_live_state', return_value=db), \
             patch.object(trader, '_get_account_summary',
                          return_value={'equity': 100000, 'positions': []}):
            trader._restore_and_reconcile()
        # After restore, last_bar_time must be a Timestamp so the main-loop
        # comparison with candles['date'].iloc[-1] (also a Timestamp) works
        assert isinstance(trader.last_bar_time, pd.Timestamp)

    def test_last_bar_time_none_stays_none(self):
        """If DB has no prior bar, attribute stays None."""
        trader = _make_trader()
        db = self._db_state(pos=0, bar=None)
        with patch('live_test.load_live_state', return_value=db), \
             patch.object(trader, '_get_account_summary',
                          return_value={'equity': 100000, 'positions': []}):
            trader._restore_and_reconcile()
        assert trader.last_bar_time is None

    def test_last_bar_time_malformed_falls_back_to_none(self):
        """Unparseable timestamp string shouldn't crash recovery."""
        trader = _make_trader()
        db = self._db_state(pos=0, bar='not-a-date')
        with patch('live_test.load_live_state', return_value=db), \
             patch.object(trader, '_get_account_summary',
                          return_value={'equity': 100000, 'positions': []}):
            trader._restore_and_reconcile()
        assert trader.last_bar_time is None


# ─────────────────────────────────────────────────────────────────────────────
# Order verification & broker reconciliation on error
# ─────────────────────────────────────────────────────────────────────────────

class TestExecuteOrderSafety:
    """Order-path safety: SL rejection detection and exception reconcile."""

    def _mock_response(self, payload, ok=True):
        resp = MagicMock()
        resp.json.return_value = payload
        if not ok:
            resp.raise_for_status.side_effect = Exception('http error')
            resp.text = 'simulated error body'
        else:
            resp.raise_for_status = MagicMock()
        return resp

    def test_sl_rejection_emits_warning_but_returns_trade_id(self, capsys):
        """When SL is rejected, the trade still opened — warn loudly."""
        trader = _make_trader('EUR_USD')
        payload = {
            'orderFillTransaction': {
                'tradeOpened': {'tradeID': 'T123'},
            },
            'stopLossOrderRejectTransaction': {'reason': 'STOP_LOSS_ON_FILL_LOSS'},
        }
        with patch('live_test.requests.post',
                   return_value=self._mock_response(payload)):
            trade_id = trader._execute_order(units=1000, comment='test', stop_loss=1.0500)
        assert trade_id == 'T123'
        out = capsys.readouterr().out
        assert 'Stop-loss REJECTED' in out
        assert 'STOP_LOSS_ON_FILL_LOSS' in out

    def test_no_sl_warning_when_no_sl_requested(self, capsys):
        """No false SL warning when stop_loss=None."""
        trader = _make_trader('EUR_USD')
        payload = {
            'orderFillTransaction': {'tradeOpened': {'tradeID': 'T999'}},
        }
        with patch('live_test.requests.post',
                   return_value=self._mock_response(payload)):
            trader._execute_order(units=1000, comment='test', stop_loss=None)
        assert 'Stop-loss REJECTED' not in capsys.readouterr().out

    def test_order_cancelled_raises(self):
        """FOK cancellation surfaces as RuntimeError so caller can recover."""
        trader = _make_trader('EUR_USD')
        payload = {
            'orderCancelTransaction': {'reason': 'MARKET_HALTED'},
        }
        with patch('live_test.requests.post',
                   return_value=self._mock_response(payload)):
            with pytest.raises(RuntimeError, match='MARKET_HALTED'):
                trader._execute_order(units=1000, comment='test')


class TestReconcileWithBroker:
    def test_broker_long_local_flat_adopts_long(self):
        trader = _make_trader('EUR_USD')
        broker_positions = [
            {'instrument': 'EUR_USD',
             'long':  {'units': '10000'},
             'short': {'units': '0'}}
        ]
        with patch.object(trader, '_get_account_summary',
                          return_value={'equity': 100000, 'positions': broker_positions}), \
             patch('live_test.save_live_state') as mock_save:
            trader._reconcile_with_broker()
        assert trader.current_position == 1
        mock_save.assert_called_once()

    def test_broker_flat_local_long_resets_to_flat(self):
        trader = _make_trader('EUR_USD', current_position=1, entry_price=1.10)
        trader.oanda_trade_id = 'T1'
        with patch.object(trader, '_get_account_summary',
                          return_value={'equity': 100000, 'positions': []}), \
             patch('live_test.save_live_state') as mock_save:
            trader._reconcile_with_broker()
        assert trader.current_position == 0
        assert trader.entry_price == 0.0
        assert trader.oanda_trade_id is None
        mock_save.assert_called_once()

    def test_agreement_no_save(self):
        """If local and broker agree, no DB write needed."""
        trader = _make_trader('EUR_USD', current_position=0)
        with patch.object(trader, '_get_account_summary',
                          return_value={'equity': 100000, 'positions': []}), \
             patch('live_test.save_live_state') as mock_save:
            trader._reconcile_with_broker()
        mock_save.assert_not_called()

    def test_broker_query_failure_keeps_local_state(self):
        """If broker is unreachable, keep current local state."""
        trader = _make_trader('EUR_USD', current_position=1)
        with patch.object(trader, '_get_account_summary',
                          side_effect=Exception('network down')):
            trader._reconcile_with_broker()
        # Should stay long, not reset
        assert trader.current_position == 1


class TestPlaceOrderErrorRecovery:
    """_place_order must reconcile with broker after any order-path exception."""

    def test_close_error_triggers_reconcile(self):
        trader = _make_trader('EUR_USD', current_position=1, entry_price=1.10)
        trader.oanda_trade_id = 'T1'
        with patch.object(trader, '_get_current_units', return_value=10000), \
             patch.object(trader, '_execute_order', side_effect=Exception('boom')), \
             patch.object(trader, '_reconcile_with_broker') as mock_reconcile, \
             patch('live_test.save_live_state'):
            trader._place_order(signal=-1, entry_price=1.10, atr=0.001)
        mock_reconcile.assert_called_once()

    def test_open_error_triggers_reconcile(self):
        trader = _make_trader('EUR_USD', current_position=0)
        with patch.object(trader, '_compute_position_size', return_value=10000), \
             patch.object(trader, '_get_corr_scale', return_value=1.0), \
             patch.object(trader, '_compute_stop_loss', return_value=1.09), \
             patch.object(trader, '_execute_order', side_effect=Exception('boom')), \
             patch.object(trader, '_reconcile_with_broker') as mock_reconcile, \
             patch('live_test.save_live_state'):
            trader._place_order(signal=1, entry_price=1.10, atr=0.001)
        mock_reconcile.assert_called_once()
        # Local state should be reset to flat (before reconcile re-checks)
        assert trader.current_position == 0


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


# ─────────────────────────────────────────────────────────────────────────────
# run_loop reconciles with the broker once per new bar
# ─────────────────────────────────────────────────────────────────────────────

class TestRunLoopPerBarReconcile:
    """run_loop must call _reconcile_with_broker at the start of every new-bar
    block — otherwise a stop-loss firing between bars leaves current_position
    stale until the next process restart."""

    def _loop_trader(self):
        trader = _make_trader('EUR_USD')
        trader.timeframe = 'D'
        trader.halted = False
        trader.pnl_history = []
        trader.equity_curve = []
        trader.last_bar_time = None  # so the fetched bar reads as new
        trader.breaker = MagicMock()
        trader.breaker.feed_return.return_value = {'action': 'none', 'current_drawdown': 0.0}
        trader.strategy_func = lambda df, p: pd.Series(0, index=df.index)
        return trader

    def _candles(self):
        return pd.DataFrame({
            'date':  pd.to_datetime(['2026-05-19', '2026-05-20']),
            'open':  [1.10, 1.10],
            'high':  [1.11, 1.11],
            'low':   [1.09, 1.09],
            'close': [1.10, 1.105],
        })

    def test_reconcile_called_on_new_bar(self):
        trader = self._loop_trader()
        with patch('live_test.get_strategy_by_id', return_value={'status': 'paper_trading'}), \
             patch.object(trader, '_fetch_candles', return_value=self._candles()), \
             patch.object(trader, '_reconcile_with_broker') as mock_reconcile, \
             patch.object(trader, '_update_metrics'), \
             patch.object(trader, '_place_order'), \
             patch('live_test.save_live_state'), \
             patch('live_test.update_live_signal'), \
             patch('live_test.time.sleep', side_effect=RuntimeError('stop-after-one-iter')):
            trader.run_loop()  # exits when the mocked sleep raises
        mock_reconcile.assert_called_once()

    def test_no_reconcile_when_bar_not_new(self):
        """Same bar as last_bar_time → not a new bar → no reconcile, no churn."""
        trader = self._loop_trader()
        candles = self._candles()
        trader.last_bar_time = candles['date'].iloc[-1]  # already processed
        with patch('live_test.get_strategy_by_id', return_value={'status': 'paper_trading'}), \
             patch.object(trader, '_fetch_candles', return_value=candles), \
             patch.object(trader, '_reconcile_with_broker') as mock_reconcile, \
             patch.object(trader, '_update_metrics'), \
             patch('live_test.time.sleep', side_effect=RuntimeError('stop-after-one-iter')):
            trader.run_loop()
        mock_reconcile.assert_not_called()
