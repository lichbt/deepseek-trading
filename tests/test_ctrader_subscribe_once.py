"""Spot subscription happens ONCE per connection, not once per get_price call.

THE BUG THIS PINS (live, 2026-08-06): get_price sent ProtoOASubscribeSpotsReq on
every call. Subscriptions are per-connection and persist, so the SECOND call for a
symbol raised ALREADY_SUBSCRIBED. _fetch_nav_ctrader turns any exception into None,
guard_tick treats None as "no equity" and returns without sampling — so the ARMED
drawdown breaker evaluated exactly once per pod lifetime and was blind from then on.
Observed: every guard tick from 01:41 UTC onward failed, for 73 minutes, while the
boot log still said "[guard] ARMED".

WHY IT SURVIVED EVERY TEST: every previous exercise of this path was a short-lived
`python -c` — one process, one connection, one call. The bug needs a SECOND call on
the SAME connection, which only a long-lived process makes. These tests do that.
"""
import queue
import sys
import threading
import types

import pytest

sys.path.insert(0, __file__.rsplit('/tests/', 1)[0])


class _Tick:
    def __init__(self, bid=115540, ask=115544):
        self.bid = bid
        self.ask = ask


class _AutoFeed(dict):
    """Delivers a tick the moment get_price registers its waiter.

    Mirrors reality: ticks arrive on the message stream, NOT as a reply to the
    subscribe. A fake that feeds from send() would hide the bug, because a call
    that skips the subscribe would also silently stop receiving.
    """

    def __init__(self, tick):
        super().__init__()
        self._tick = tick

    def __setitem__(self, key, box):
        super().__setitem__(key, box)
        box.put(self._tick)


def _client(send_effect=None, tick=None):
    from ctrader_client import CTraderClient
    cli = CTraderClient.__new__(CTraderClient)      # no reactor, no socket
    cli.account_id = 1
    cli._digits = {}
    cli._subscribed = set()
    cli._price_waiters = _AutoFeed(tick or _Tick())
    cli.sent = []

    def _send(req, timeout=10):
        cli.sent.append(list(req.symbolId))
        if send_effect is not None:
            send_effect(len(cli.sent))
        return types.SimpleNamespace()

    cli.send = _send
    return cli


def test_second_call_does_not_resubscribe():
    """The regression itself: N calls, ONE subscribe."""
    cli = _client()
    for _ in range(5):
        assert cli.get_price(1) == (1.15540, 1.15544)
    assert len(cli.sent) == 1, f'subscribed {len(cli.sent)} times, expected once'
    assert cli._subscribed == {1}


def test_distinct_symbols_each_subscribe_once():
    cli = _client()
    cli.get_price(1); cli.get_price(2); cli.get_price(1); cli.get_price(2)
    assert cli.sent == [[1], [2]]


def test_already_subscribed_is_tolerated_not_fatal():
    """Someone else holds the subscription (another session, or our own across a
    reconnect). Ticks still arrive, so this must NOT abort the equity read — that
    is precisely the path that blinded the breaker."""
    from ctrader_client import CTraderError

    def boom(n):
        raise CTraderError('ALREADY_SUBSCRIBED: An attempt to subscribe twice')

    cli = _client(send_effect=boom)
    assert cli.get_price(1) == (1.15540, 1.15544)
    assert cli._subscribed == {1}


def test_a_different_error_still_propagates():
    """Tolerating ALREADY_SUBSCRIBED must not become tolerating everything."""
    from ctrader_client import CTraderError

    def boom(n):
        raise CTraderError('NOT_AUTHENTICATED: session is dead')

    cli = _client(send_effect=boom)
    with pytest.raises(CTraderError, match='NOT_AUTHENTICATED'):
        cli.get_price(1)


def test_disconnect_clears_the_cache_so_a_reconnect_resubscribes():
    """Subscriptions die with the connection. Keeping the cache across a reconnect
    would make get_price skip the re-subscribe and then block for ticks that were
    never subscribed to — a silent hang instead of a loud error."""
    cli = _client()
    cli.get_price(1)
    assert cli._subscribed == {1}
    cli._authed = threading.Event()
    cli._authed.set()
    cli._on_disconnected(None, 'connection lost')
    assert cli._subscribed == set(), 'stale subscription survived a disconnect'
    assert not cli._authed.is_set()
    cli.get_price(1)
    assert len(cli.sent) == 2, 'did not re-subscribe after reconnect'


# NOT tested here: prop_guard._fetch_nav_ctrader over repeated ticks. Mocking it
# would mock the function under test, and mocking the client beneath it only
# re-asserts what test_second_call_does_not_resubscribe already proves. That leg
# was verified LIVE against ctid 48171893 on 2026-08-06 — three consecutive
# assemblies on one connection returned 100007.51 / 100005.89 / 100005.36 where
# the pre-fix code returned None on the second and every one after.
