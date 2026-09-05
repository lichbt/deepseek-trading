"""Does the client recover when the broker stops accepting its token?

THE OUTAGE THIS PINS (live, 2026-09-04): the cTrader access token expired in place
and the pod was dead to the broker for ~9 HOURS. `_refresh_if_stale` was reachable
only from `_on_connected`, so a long-lived fix_runner re-checked expiry ONLY when
the transport reconnected — and it never did (zero disconnect events in 619 log
lines). `_authed` stayed set, so `send()` never raised its own 'not authenticated';
every request just relayed a server rejection.

What that cost: the weekend-flat window fired and closed 0 of 2, the roll-flat
window fired and closed 0 of 7 (all 'stop cancel unconfirmed', the correct refusal),
the 00:15 UTC pass failed in 1s, and the prop guard was blind throughout. The pod
stayed 1/1 Running the whole time and NOTHING alerted — it was found only because a
human noticed weekend-flat had not fired.

Two details these tests exist to hold down:
  * the server DEGRADES its error — OA_AUTH_TOKEN_EXPIRED first, then the generic
    'Trading account is not authorized' forever after;
  * expiry is not the only death. Whichever host refreshes first ROTATES the refresh
    token, revoking the other's access token while its expires_at still looks fine —
    so a rejection-driven re-auth MUST bypass the staleness check.
"""
import sys
import threading
import time

import pytest

sys.path.insert(0, __file__.rsplit('/tests/', 1)[0])


class _Err:
    def __init__(self, code, desc):
        self.errorCode, self.description = code, desc


class _Payload:
    def __init__(self, payload_type, body=None):
        self.payloadType, self.body = payload_type, body


class _Deferred:
    def __init__(self, payload):
        self._payload = payload

    def addCallbacks(self, cb, _eb):
        cb(self._payload)


class _Conn:
    """A connection that hands back one queued payload per send()."""

    def __init__(self, payloads):
        self.payloads, self.sent = list(payloads), []

    def send(self, req, responseTimeoutInSeconds=None):
        self.sent.append(req)
        return _Deferred(self.payloads.pop(0))


def _client(monkeypatch, payloads):
    import ctrader_client as cc
    monkeypatch.setattr(cc.reactor, 'callFromThread',
                        lambda fn, *a, **k: fn(*a, **k), raising=False)
    monkeypatch.setattr(cc, 'Protobuf',
                        type('P', (), {'extract': staticmethod(lambda p: p.body)}))
    cli = cc.CTraderClient.__new__(cc.CTraderClient)
    cli._authed = threading.Event()
    cli._authed.set()
    cli._client = _Conn(payloads)
    cli._auth_error = None
    cli._lock = threading.Lock()
    return cc, cli


def _err_payload(cc, code, desc):
    return _Payload(cc.ProtoOAPayloadType.PROTO_OA_ERROR_RES, _Err(code, desc))


# --- the rejection is recognised and the request is retried ----------------

@pytest.mark.parametrize('code,desc', [
    ('OA_AUTH_TOKEN_EXPIRED', 'Access token has been expired'),
    ('INVALID_REQUEST', 'Trading account is not authorized'),
])
def test_an_authorization_rejection_re_auths_and_retries(monkeypatch, code, desc):
    """THE FIX. Both shapes the server actually produced must trigger recovery."""
    import ctrader_client as cc
    cc_, cli = _client(monkeypatch, [])
    cli._client = _Conn([_err_payload(cc_, code, desc), _Payload(7, 'RESULT')])
    calls = []
    cli._reauth = lambda: calls.append(1)

    assert cli.send(object()) == 'RESULT'
    assert calls == [1], 'must re-authenticate, not just relay the rejection'
    assert len(cli._client.sent) == 2, 'must retry the original request'


def test_an_unrelated_error_does_NOT_trigger_a_re_auth(monkeypatch):
    """'INVALID_REQUEST' alone is not an auth failure — most of them are not."""
    cc_, cli = _client(monkeypatch, [])
    cli._client = _Conn([_err_payload(cc_, 'INVALID_REQUEST', 'Symbol not found')])
    calls = []
    cli._reauth = lambda: calls.append(1)

    with pytest.raises(cc_.CTraderError):
        cli.send(object())
    assert calls == [], 'a re-auth on every error would hammer the token endpoint'


def test_it_retries_ONCE_and_then_raises(monkeypatch):
    """A permanently revoked token must not loop."""
    cc_, cli = _client(monkeypatch, [])
    cli._client = _Conn([_err_payload(cc_, 'OA_AUTH_TOKEN_EXPIRED', 'expired'),
                         _err_payload(cc_, 'INVALID_REQUEST',
                                      'Trading account is not authorized')])
    cli._reauth = lambda: None

    with pytest.raises(cc_.CTraderError):
        cli.send(object())
    assert len(cli._client.sent) == 2, 'exactly one retry, no more'


# --- a rejection-driven refresh must ignore expires_at --------------------

def test_a_forced_refresh_ignores_a_healthy_expires_at(monkeypatch):
    """Rotation revokes a token whose expires_at still looks fine."""
    import ctrader_client as cc
    monkeypatch.setattr(cc, '_load_tokens', lambda: {
        'access_token': 'revoked-but-not-expired',
        'refresh_token': 'r',
        'expires_at': time.time() + 99999,
    })
    monkeypatch.setattr(cc, '_save_tokens', lambda _t: None)

    # Without force the staleness test short-circuits and the dead token comes back.
    def _must_not_post(*_a, **_k):
        raise AssertionError('unforced refresh must not hit the token endpoint')
    monkeypatch.setattr(cc.requests, 'post', _must_not_post)
    assert cc._refresh_if_stale({}, force=False) == 'revoked-but-not-expired'

    class _Resp:
        status_code = 200

        @staticmethod
        def json():
            return {'access_token': 'fresh', 'expires_in': 100}

    monkeypatch.setattr(cc.requests, 'post', lambda *_a, **_k: _Resp())
    assert cc._refresh_if_stale(
        {'CTRADER_CLIENT_ID': 'i', 'CTRADER_CLIENT_SECRET': 's'}, force=True) == 'fresh'


# --- the stranded case must surface, not hang -----------------------------

def test_a_dead_refresh_token_raises_instead_of_hanging(monkeypatch):
    """When the refresh token itself is rotated dead, nothing here can fix it."""
    cc_, cli = _client(monkeypatch, [])
    cli._authed.clear()

    def _fails(_client, force_refresh=False):
        cli._auth_error = 'cTrader token refresh returned no access_token — NOT TRADING'
    cli._on_connected = _fails

    with pytest.raises(cc_.CTraderError) as excinfo:
        cli._reauth(timeout=0.05)
    assert 'NOT TRADING' in str(excinfo.value)
