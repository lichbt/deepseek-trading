"""Order construction must match the broker's real instrument spec.

Motivating incident (2026-07-31): live_test's hand-maintained _INSTRUMENT_SIZING
claimed ETH_USD tradeUnitsPrecision=3 where OANDA's is 2, so every ETH order was
rejected UNITS_PRECISION_EXCEEDED — 77 times over 7 hours — while the prop book,
which reads the venue's own specs, held the same short. The sleeve had never been
able to trade on paper.

Two layers here, and the second is the one that would have caught it:
  * unit tests pin the terminal-vs-transient classification;
  * a CONTRACT test revalidates the table against the live OANDA API, so drift is
    found by a test rather than by a rejected live order.
"""
import os

import pytest

import live_test


# --------------------------------------------------------------------------
# Contract: the table vs. the broker
# --------------------------------------------------------------------------

CRYPTO = ['BTC_USD', 'ETH_USD', 'LTC_USD']


@pytest.fixture(scope='module')
def oanda_specs():
    """Live instrument specs, or skip. Never fabricate — a fabricated spec here
    would re-create exactly the bug this file exists to prevent."""
    import requests
    tok = os.getenv('OANDA_API_TOKEN')
    acc = os.getenv('OANDA_ACCOUNT_ID')
    if not tok or not acc:
        pytest.skip('OANDA creds not set — contract test needs the live API')
    try:
        r = requests.get(
            f'https://api-fxpractice.oanda.com/v3/accounts/{acc}/instruments',
            headers={'Authorization': f'Bearer {tok}'},
            params={'instruments': ','.join(CRYPTO)}, timeout=15)
        r.raise_for_status()
    except Exception as e:                                   # pragma: no cover
        pytest.skip(f'OANDA unreachable: {e}')
    return {i['name']: i for i in r.json()['instruments']}


@pytest.mark.parametrize('name', CRYPTO)
def test_sizing_table_matches_the_broker(oanda_specs, name):
    spec = oanda_specs[name]
    ours = live_test._INSTRUMENT_SIZING[name]
    assert ours['unit_precision'] == int(spec['tradeUnitsPrecision']), (
        f'{name}: table says precision {ours["unit_precision"]}, OANDA says '
        f'{spec["tradeUnitsPrecision"]} — orders will be rejected')
    assert float(ours['min_units']) == float(spec['minimumTradeSize']), (
        f'{name}: min_units drifted from the broker minimum')
    assert float(ours['max_units']) == float(spec['maximumOrderUnits']), (
        f'{name}: max_units drifted from the broker maximum')


def test_rounding_a_size_produces_units_oanda_will_accept(oanda_specs):
    """The actual failure was formatting, not the table in the abstract."""
    for name in CRYPTO:
        prec = live_test._INSTRUMENT_SIZING[name]['unit_precision']
        rendered = f'{-0.87069999:.{prec}f}'
        decimals = len(rendered.split('.')[1]) if '.' in rendered else 0
        assert decimals <= int(oanda_specs[name]['tradeUnitsPrecision']), \
            f'{name}: rendered {rendered} exceeds broker precision'


# --------------------------------------------------------------------------
# Terminal vs transient classification
# --------------------------------------------------------------------------

class _Resp:
    def __init__(self, payload):
        self._payload = payload

    def json(self):
        if self._payload is None:
            raise ValueError('not json')
        return self._payload


def test_units_precision_exceeded_is_read_as_terminal():
    reason = live_test._oanda_reject_reason(_Resp(
        {'orderRejectTransaction': {'rejectReason': 'UNITS_PRECISION_EXCEEDED'}}))
    assert reason == 'UNITS_PRECISION_EXCEEDED'
    assert reason in live_test.TERMINAL_REJECT_REASONS


def test_market_halted_stays_transient():
    """The pending-retry path must survive — it is why halted daily closes fill."""
    reason = live_test._oanda_reject_reason(_Resp(
        {'orderCancelTransaction': {'reason': 'MARKET_HALTED'}}))
    assert reason == 'MARKET_HALTED'
    assert reason not in live_test.TERMINAL_REJECT_REASONS


def test_unknown_reason_stays_transient():
    """Unrecognised rejections keep retrying, so an unknown-but-transient
    reason still self-heals. Skipping a good trade is the worse error."""
    reason = live_test._oanda_reject_reason(_Resp(
        {'orderCancelTransaction': {'reason': 'SOMETHING_NEW'}}))
    assert reason not in live_test.TERMINAL_REJECT_REASONS


def test_reject_reason_never_raises_on_a_junk_body():
    """Runs on the order path; it must not turn a broker error into a crash."""
    assert live_test._oanda_reject_reason(_Resp(None)) is None
    assert live_test._oanda_reject_reason(_Resp({})) is None


def test_terminal_reject_is_not_a_plain_exception():
    """_place_order_netting discriminates on the type, so the hierarchy matters."""
    assert issubclass(live_test.TerminalOrderReject, RuntimeError)


# --------------------------------------------------------------------------
# Wiring: a terminal reject must reach the caller as "do not retry"
# --------------------------------------------------------------------------

def _netting_stub(raise_exc):
    """Minimal stand-in driving the REAL _place_order_netting."""
    import types

    class Stub:
        strategy_id = 'test_sleeve'
        instrument = 'ETH_USD'
        own_units = 0.0
        current_position = 0
        entry_price = 0.0
        stop_price = None
        halted = False

        def _get_corr_scale(self, signal):        return 1.0
        def _compute_position_size(self, atr, corr_scale=1.0): return 0.8707
        def _compute_stop_loss(self, sig, px, atr):   return None
        def _mirror_live_status(self):            return None
        def _execute_order(self, units, comment, stop_loss=None):
            if raise_exc:
                raise raise_exc
            return 'trade-1'

    s = Stub()
    s._place_order_netting = types.MethodType(
        live_test.LiveTrader._place_order_netting, s)
    return s


def test_terminal_reject_sets_the_do_not_retry_flag(monkeypatch):
    monkeypatch.setattr(live_test, '_save_own_units', lambda *a, **k: None)
    monkeypatch.setattr(live_test, '_read_trading_halt', lambda: (False, False))
    s = _netting_stub(live_test.TerminalOrderReject('UNITS_PRECISION_EXCEEDED'))
    s._last_order_terminal = False
    assert s._place_order_netting(-1, 3000.0, 50.0) is False
    assert s._last_order_terminal is True, \
        'terminal reject did not set the flag the retry sites read'


def test_transient_reject_leaves_the_flag_clear(monkeypatch):
    """MARKET_HALTED must still queue for retry, or halted closes never fill."""
    monkeypatch.setattr(live_test, '_save_own_units', lambda *a, **k: None)
    monkeypatch.setattr(live_test, '_read_trading_halt', lambda: (False, False))
    s = _netting_stub(RuntimeError('Order cancelled by OANDA (reason=MARKET_HALTED)'))
    s._last_order_terminal = False
    assert s._place_order_netting(-1, 3000.0, 50.0) is False
    assert s._last_order_terminal is False, \
        'a transient failure was misclassified as terminal — trade silently skipped'


def test_retry_sites_consult_the_flag():
    """Source pin: the flag is inert unless run_loop branches on it.

    Both the queue site and the retry site must check, or an order queued
    before the classification existed retries forever.
    """
    from pathlib import Path
    src = Path(live_test.__file__).read_text()
    run_loop = src.split('def run_loop(self):', 1)[1]
    assert run_loop.count('_last_order_terminal') >= 2, \
        'run_loop no longer checks _last_order_terminal at both sites'
    assert 'ABANDONED' in run_loop
    assert 'not queued for retry' in run_loop
