"""Spot ticks scale by a FIXED 1e5, never by the symbol's own `digits`.

NEGATIVE CONTROL: test_two_digit_instrument_is_not_scaled_by_digits FAILS against
the pre-fix code (which divided by 10**digits) and passes after. The digits=5 case
passes either way -- which is exactly why the bug survived: EUR_GBP, the only
instrument whose accrual had ever been checked by hand, is 5-digit and so was
correct by coincidence.

Reference values are the broker's own reconcile entry prices, read live
2026-07-31: NAS100 27,447.01 (digits=2) and EUR_GBP 0.85480 (digits=5).
"""
import queue
import sys
import types
from unittest import mock

import pytest

sys.path.insert(0, __file__.rsplit('/tests/', 1)[0])


class _Tick:
    """Minimal stand-in for ProtoOASpotEvent."""

    def __init__(self, bid, ask):
        self.bid = bid
        self.ask = ask


def _client_returning(tick):
    """A CTraderClient wired so get_price() consumes exactly one tick."""
    from ctrader_client import CTraderClient

    cli = CTraderClient.__new__(CTraderClient)      # no reactor, no socket
    cli.account_id = 1
    cli._digits = {}
    cli._price_waiters = {}
    cli._subscribed = set()                         # __init__ is bypassed; mirror it

    def _send(req, timeout=10):
        # the subscribe ack; the tick is delivered straight into the waiter
        for sid in req.symbolId:
            cli._price_waiters[sid].put(tick)
        return types.SimpleNamespace()

    cli.send = _send
    return cli


def _get_price(cli, symbol_id):
    from ctrader_open_api.messages.OpenApiMessages_pb2 import ProtoOASubscribeSpotsReq
    assert ProtoOASubscribeSpotsReq is not None      # the real req type is used
    return cli.get_price(symbol_id)


def test_two_digit_instrument_is_not_scaled_by_digits():
    """NAS100: digits=2, true price 27447.01, so the tick carries 2744701000.

    Under the old 10**digits divisor this returned 27,447,010 -- 1000x high.
    """
    cli = _client_returning(_Tick(bid=2744701000, ask=2744703000))
    cli._digits = {106: 2}                           # would mis-scale if consulted
    bid, ask = _get_price(cli, 106)
    assert bid == pytest.approx(27447.01, abs=0.01)
    assert ask == pytest.approx(27447.03, abs=0.01)


def test_five_digit_instrument_unchanged_by_the_fix():
    """EUR_GBP: digits=5, so 10**digits and 1e5 agree. Pins that the fix is a no-op here."""
    cli = _client_returning(_Tick(bid=85480, ask=85500))
    cli._digits = {9: 5}
    bid, ask = _get_price(cli, 9)
    assert bid == pytest.approx(0.85480, abs=1e-6)
    assert ask == pytest.approx(0.85500, abs=1e-6)


def test_scale_is_a_constant_not_a_per_symbol_lookup():
    """The same raw tick must yield the same price whatever `digits` claims.

    Guards the regression directly: any reintroduction of a per-symbol divisor makes
    these two disagree.
    """
    a = _client_returning(_Tick(bid=2744701000, ask=2744701000))
    a._digits = {1: 2}
    b = _client_returning(_Tick(bid=2744701000, ask=2744701000))
    b._digits = {2: 5}
    assert _get_price(a, 1) == _get_price(b, 2)


def test_get_price_does_not_fetch_symbol_details():
    """The digits prefetch was only ever for scaling; it must not cost a round trip."""
    from ctrader_client import CTraderClient

    cli = _client_returning(_Tick(bid=85480, ask=85500))
    with mock.patch.object(CTraderClient, 'get_symbol_details') as spy:
        _get_price(cli, 9)
    spy.assert_not_called()
