"""Does a wedged cTrader auth handshake ever recover without a pod restart?

THE OUTAGE THIS PINS (live, 2026-08-09): the prop guard went blind for 15.5 HOURS.
The Open API protobuf handshake wedged — NOT token expiry, the refresh token does
not expire — and `start()` could never recover, because `if self._client is None`
was the only path that built a client. A wedged client is not None, so every later
call waited AUTH_TIMEOUT and raised the same error, forever. Twisted's ClientService
reconnects the TRANSPORT, but nothing escalated to "throw this client away".

The pod stayed 1/1 Running throughout, which is why nothing noticed: liveness is not
guard health. Broker-side stops held, so what was lost was the aggregate 3%/10%
breaker and signal-flip exits.

These tests drive the real start()/close() with a fake reactor and a connection that
never authenticates, which is exactly the shape of the failure.
"""
import sys
import threading

import pytest

sys.path.insert(0, __file__.rsplit('/tests/', 1)[0])


class _FakeClient:
    def __init__(self):
        self.stopped = 0

    def stopService(self):
        self.stopped += 1


def _wedged(monkeypatch):
    """A CTraderClient whose handshake never completes."""
    import ctrader_client as cc
    monkeypatch.setattr(cc.reactor, 'callFromThread',
                        lambda fn, *a, **k: fn(*a, **k), raising=False)
    cli = cc.CTraderClient.__new__(cc.CTraderClient)
    cli._authed = threading.Event()          # never set -> auth always times out
    cli._client = _FakeClient()
    cli._heartbeat = None
    cli._subscribed = {1, 2}
    cli._auth_error = None
    cli._auth_fails = 0
    cli._lock = threading.Lock()
    return cc, cli


def test_a_single_timeout_does_NOT_discard_the_client(monkeypatch):
    """A transient blip must not tear down a connection about to succeed."""
    cc, cli = _wedged(monkeypatch)
    with pytest.raises(cc.CTraderError):
        cli.start(timeout=0.01)
    assert cli._client is not None
    assert cli._auth_fails == 1


def test_it_discards_after_the_threshold_so_the_next_call_rebuilds(monkeypatch):
    """THE FIX. Without this the wedge is permanent and only a restart clears it."""
    cc, cli = _wedged(monkeypatch)
    for _ in range(cc.AUTH_REBUILD_AFTER):
        with pytest.raises(cc.CTraderError):
            cli.start(timeout=0.01)
    assert cli._client is None, "wedged client kept — start() can never rebuild it"
    assert cli._auth_fails == 0, "counter not reset; next wedge takes longer to clear"
    assert cli._subscribed == set(), "stale subscriptions would break the rebuild"


def test_the_discard_stops_the_old_service(monkeypatch):
    cc, cli = _wedged(monkeypatch)
    old = cli._client
    for _ in range(cc.AUTH_REBUILD_AFTER):
        with pytest.raises(cc.CTraderError):
            cli.start(timeout=0.01)
    assert old.stopped == 1


def test_success_resets_the_counter(monkeypatch):
    """Two timeouts then a success must not leave the client one failure from a
    teardown it no longer needs."""
    cc, cli = _wedged(monkeypatch)
    for _ in range(cc.AUTH_REBUILD_AFTER - 1):
        with pytest.raises(cc.CTraderError):
            cli.start(timeout=0.01)
    assert cli._auth_fails == cc.AUTH_REBUILD_AFTER - 1
    cli._authed.set()                        # handshake completes
    assert cli.start(timeout=0.01) is cli
    assert cli._auth_fails == 0
    assert cli._client is not None


def test_an_authed_client_is_never_torn_down(monkeypatch):
    cc, cli = _wedged(monkeypatch)
    cli._authed.set()
    for _ in range(cc.AUTH_REBUILD_AFTER * 3):
        cli.start(timeout=0.01)
    assert cli._client is not None


def test_close_forgets_the_client(monkeypatch):
    """close() used to stop the service but leave _client set, so start() would
    never rebuild — the same trap by another route."""
    cc, cli = _wedged(monkeypatch)
    cli.close()
    assert cli._client is None
    assert cli._authed.is_set() is False
    assert cli._subscribed == set()
