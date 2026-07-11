"""Tests for the broker adapter abstraction and cTrader symbol mapping."""
import pytest
from ctrader_adapter import (
    OandaAdapter,
    CTraderAdapter,
    BrokerAdapter,
    get_broker_adapter,
    _CTRADER_SYMBOL_MAP,
    _CTRADER_TF_MAP,
)


def test_broker_adapter_is_abstract():
    with pytest.raises(TypeError):
        BrokerAdapter('EUR_USD')


def test_oanda_adapter_is_broker_adapter():
    adapter = OandaAdapter('EUR_USD')
    assert isinstance(adapter, BrokerAdapter)
    assert adapter.instrument == 'EUR_USD'


def test_factory_defaults_to_oanda(monkeypatch):
    monkeypatch.delenv('BROKER', raising=False)
    adapter = get_broker_adapter('EUR_USD')
    assert isinstance(adapter, OandaAdapter)


def test_factory_ctrader_requires_env(monkeypatch):
    monkeypatch.setenv('BROKER', 'ctrader')
    monkeypatch.delenv('CTRADER_CLIENT_ID', raising=False)
    with pytest.raises(KeyError):
        get_broker_adapter('EUR_USD')


def test_symbol_map_covers_all_traded_instruments():
    required = [
        'EUR_USD', 'GBP_USD', 'USD_CHF', 'GBP_JPY', 'EUR_JPY', 'EUR_GBP',
        'NAS100_USD', 'SPX500_USD', 'DE30_EUR', 'AU200_AUD',
        'XAG_USD', 'XAU_USD', 'XCU_USD', 'XPT_USD', 'XPD_USD',
        'BTC_USD', 'WHEAT_USD', 'WTICO_USD',
    ]
    for inst in required:
        assert inst in _CTRADER_SYMBOL_MAP, f'{inst} missing from symbol map'


def test_timeframe_map():
    assert _CTRADER_TF_MAP['D'] == 7
    assert _CTRADER_TF_MAP['H1'] == 5
    assert _CTRADER_TF_MAP['M30'] == 4


def test_oanda_quote_to_usd_rate_usd_pair():
    adapter = OandaAdapter('EUR_USD')
    assert adapter.quote_to_usd_rate() == 1.0


def test_oanda_quote_to_usd_rate_unknown_quote():
    adapter = OandaAdapter('FOO_BAR')
    assert adapter.quote_to_usd_rate() == 1.0
