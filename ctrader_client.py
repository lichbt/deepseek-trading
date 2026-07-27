"""
cTrader Open API client core — connection, auth, heartbeat, reconnect, tokens.

READ-ONLY by design: this module never places, amends or closes an order. It is the
foundation the execution layer is built on, kept separate so the risky surface can be
reviewed on its own.

Why this exists instead of `ctrader_adapter.CTraderAdapter`: that class cannot run.
`_connect()` calls `self._client.connect()`, which does not exist — `ctrader_open_api.Client`
subclasses Twisted's `ClientService`, whose entry point is `startService()` and which does
nothing without a RUNNING REACTOR. Its `_send_sync()` then blocks on `queue.get()` while no
reactor turns, so it deadlocks even once the method name is fixed.

The core problem this module solves: `fix_runner.py` is synchronous (a plain `while` loop)
but Twisted is asynchronous. So the reactor runs in a background daemon thread and every
public method blocks the caller until the reactor thread answers.

Usage:
    client = get_client()
    client.start()
    for pos in client.get_positions():
        print(pos['position_id'], pos['sl'])
"""

import json
import os
import queue
import threading
import time
from typing import Dict, List, Optional, Tuple

import requests

from twisted.internet import reactor
from twisted.internet.task import LoopingCall

from ctrader_open_api import Client, TcpProtocol, EndPoints, Protobuf
from ctrader_open_api.messages.OpenApiCommonMessages_pb2 import ProtoHeartbeatEvent
from ctrader_open_api.messages.OpenApiMessages_pb2 import (
    ProtoOAApplicationAuthReq,
    ProtoOAAccountAuthReq,
    ProtoOATraderReq,
    ProtoOASymbolsListReq,
    ProtoOASymbolByIdReq,
    ProtoOAReconcileReq,
    ProtoOASubscribeSpotsReq,
)
from ctrader_open_api.messages.OpenApiModelMessages_pb2 import ProtoOAPayloadType


# ── config ───────────────────────────────────────────────────────────────────

_REPO = os.path.dirname(os.path.abspath(__file__))
_ENV_FILE = os.path.join(_REPO, '.env')
_TOKEN_FILE = os.path.join(_REPO, '.ctrader_tokens.json')

TOKEN_URL = 'https://openapi.ctrader.com/apps/token'
HEARTBEAT_SECS = 10          # server drops an idle connection at 30s
AUTH_TIMEOUT = 45.0
AUTH_REQ_TIMEOUT = 20      # per-request; SDK default of 5s is too tight for a cold boot
REFRESH_MARGIN = 300         # refresh when the access token is within 5 min of expiry

ACCESS_RIGHTS = {0: 'FULL_ACCESS', 1: 'CLOSE_ONLY', 2: 'NO_TRADING', 3: 'NO_LOGIN'}


def _load_env() -> Dict[str, str]:
    """Parse .env directly — the runner does not depend on python-dotenv."""
    env = {}
    if os.path.exists(_ENV_FILE):
        with open(_ENV_FILE) as fh:
            for line in fh:
                line = line.strip()
                if not line or line.startswith('#') or '=' not in line:
                    continue
                if line.startswith('export '):
                    line = line[len('export '):]
                key, val = line.split('=', 1)
                val = val.split('#')[0].strip().strip('"').strip("'")
                env[key.strip()] = val
    env.update({k: v for k, v in os.environ.items() if k.startswith('CTRADER_')})
    return env


# ── reactor thread — process-global, started at most once ────────────────────

_reactor_lock = threading.Lock()
_reactor_started = False


def _ensure_reactor() -> None:
    """Start the Twisted reactor in a daemon thread, once per process.

    `installSignalHandlers=False` is mandatory: signal handlers may only be installed
    from the main thread, and without this the reactor raises on startup. The reactor
    cannot be restarted once stopped, hence the module-level guard.
    """
    global _reactor_started
    with _reactor_lock:
        if _reactor_started:
            return
        threading.Thread(
            target=lambda: reactor.run(installSignalHandlers=False),
            name='ctrader-reactor', daemon=True).start()
        _reactor_started = True
        deadline = time.time() + 10
        while not reactor.running and time.time() < deadline:
            time.sleep(0.02)
        if not reactor.running:
            raise RuntimeError('Twisted reactor failed to start')


# ── tokens ───────────────────────────────────────────────────────────────────

def _load_tokens() -> Dict:
    """Tokens from CTRADER_TOKENS (JSON) if set, else the local file.

    The env path exists for deployment: .ctrader_tokens.json is gitignored — it holds a
    never-expiring refresh token and must never be baked into an image — so a container
    receives it as a secret instead. The refresh token does not expire, so a fresh
    container can always mint a new access token at boot.
    """
    blob = os.environ.get('CTRADER_TOKENS', '').strip()
    if blob:
        try:
            return json.loads(blob)
        except ValueError as exc:
            raise RuntimeError('CTRADER_TOKENS is set but is not valid JSON: %s' % exc)
    if not os.path.exists(_TOKEN_FILE):
        raise RuntimeError(
            'No cTrader tokens: set CTRADER_TOKENS or run ctrader_auth.py. '
            'NOTE: on this Mac, ControlCenter/AirPlay holds the *:5000 wildcard, so a '
            'v4-only callback server never receives the redirect — bind 127.0.0.1 AND ::1.')
    with open(_TOKEN_FILE) as fh:
        return json.load(fh)


def _save_tokens(tokens: Dict) -> None:
    """Best-effort persist. A read-only or ephemeral container FS is not an error —
    the refresh token is what matters and that comes from the environment."""
    try:
        with open(_TOKEN_FILE, 'w') as fh:
            json.dump(tokens, fh, indent=2)
    except OSError as exc:
        print('[cTrader] could not persist tokens (%s) — continuing on the '
              'refreshed token in memory' % exc, flush=True)


def _refresh_if_stale(env: Dict[str, str]) -> str:
    """Return a valid access token, refreshing it if near expiry.

    A failed refresh RAISES. Silent expiry is the worst failure mode of this migration:
    the runner would keep looping while quietly placing nothing.
    """
    tokens = _load_tokens()
    if tokens.get('expires_at', 0) > time.time() + REFRESH_MARGIN:
        return tokens['access_token']

    print('[cTrader] access token near expiry — refreshing', flush=True)
    resp = requests.post(TOKEN_URL, data={
        'grant_type': 'refresh_token',
        'refresh_token': tokens['refresh_token'],
        'client_id': env['CTRADER_CLIENT_ID'],
        'client_secret': env['CTRADER_CLIENT_SECRET'],
    }, timeout=20)
    if resp.status_code != 200:
        raise RuntimeError(
            'cTrader token refresh FAILED (http %s) — NOT TRADING until fixed'
            % resp.status_code)
    data = resp.json()
    if 'access_token' not in data:
        raise RuntimeError('cTrader token refresh returned no access_token — NOT TRADING')
    merged = {
        'access_token': data['access_token'],
        # the response may omit the refresh token; keep the one we have
        'refresh_token': data.get('refresh_token') or tokens['refresh_token'],
        'expires_at': time.time() + data.get('expires_in', 2592000),
        'created_at': time.time(),
    }
    _save_tokens(merged)
    return merged['access_token']


# ── client ───────────────────────────────────────────────────────────────────

class CTraderError(RuntimeError):
    """A ProtoOAErrorRes came back, or the connection is not usable."""


class CTraderClient:
    """Synchronous facade over the async cTrader Open API.

    One instance per account; use `get_client()` rather than constructing directly.
    """

    def __init__(self) -> None:
        env = _load_env()
        missing = [k for k in ('CTRADER_CLIENT_ID', 'CTRADER_CLIENT_SECRET',
                               'CTRADER_ACCOUNT_ID') if not env.get(k)]
        if missing:
            raise RuntimeError('missing in .env: %s' % ', '.join(missing))

        self._env = env
        self.client_id = env['CTRADER_CLIENT_ID']
        self.client_secret = env['CTRADER_CLIENT_SECRET']
        # the ctidTraderAccountId (47916240), NOT the 5047309 dashboard login FIX uses
        self.account_id = int(env['CTRADER_ACCOUNT_ID'])
        self.env_name = env.get('CTRADER_ENV', 'demo')

        self._client = None                     # type: Optional[Client]
        self._authed = threading.Event()
        self._auth_error = None                 # type: Optional[str]
        self._heartbeat = None                  # type: Optional[LoopingCall]
        self._digits = {}                       # type: Dict[int, int]
        self._price_waiters = {}                # type: Dict[int, queue.Queue]
        self._lock = threading.Lock()

    # --- lifecycle ---

    def start(self, timeout: float = AUTH_TIMEOUT) -> 'CTraderClient':
        """Connect and authenticate. Idempotent; blocks until ready or raises."""
        with self._lock:
            if self._authed.is_set():
                return self
            if self._client is None:
                _ensure_reactor()
                host = (EndPoints.PROTOBUF_LIVE_HOST if self.env_name == 'live'
                        else EndPoints.PROTOBUF_DEMO_HOST)
                self._client = Client(host, EndPoints.PROTOBUF_PORT, TcpProtocol)
                self._client.setConnectedCallback(self._on_connected)
                self._client.setDisconnectedCallback(self._on_disconnected)
                self._client.setMessageReceivedCallback(self._on_message)
                reactor.callFromThread(self._client.startService)

        if not self._authed.wait(timeout):
            raise CTraderError(self._auth_error or
                               'cTrader auth timed out after %ss' % timeout)
        return self

    def close(self) -> None:
        """Stop this connection. Leaves the global reactor running."""
        self._authed.clear()
        if self._heartbeat is not None and self._heartbeat.running:
            reactor.callFromThread(self._heartbeat.stop)
            self._heartbeat = None
        if self._client is not None:
            reactor.callFromThread(self._client.stopService)

    # --- connection callbacks (all run on the reactor thread) ---

    def _on_connected(self, _client) -> None:
        """Runs on every connection, including reconnects — so it re-authenticates."""
        try:
            token = _refresh_if_stale(self._env)
        except Exception as exc:                        # noqa: BLE001 — surfaced to caller
            self._auth_error = str(exc)
            print('[cTrader] %s' % exc, flush=True)
            return

        req = ProtoOAApplicationAuthReq()
        req.clientId = self.client_id
        req.clientSecret = self.client_secret
        # Explicit timeout: the SDK defaults to 5s, which is too tight for a cold
        # container boot or a slow link — a transient stall then reads as auth failure
        # and the runner never starts.
        d = self._client.send(req, responseTimeoutInSeconds=AUTH_REQ_TIMEOUT)
        d.addCallbacks(lambda _: self._auth_account(token), self._auth_failed)

    def _auth_account(self, token: str) -> None:
        req = ProtoOAAccountAuthReq()
        req.ctidTraderAccountId = self.account_id
        req.accessToken = token
        d = self._client.send(req, responseTimeoutInSeconds=AUTH_REQ_TIMEOUT)
        d.addCallbacks(self._auth_done, self._auth_failed)

    def _auth_done(self, resp) -> None:
        # A ProtoOAErrorRes arrives as a RESPONSE, so the deferred fires its callback,
        # not its errback — without this check a rejected account auth reads as success
        # and the client reports itself connected while nothing works. Seen live: the
        # 5047309 login (instead of ctid 47916240) printed "authenticated".
        if resp.payloadType == ProtoOAPayloadType.PROTO_OA_ERROR_RES:
            err = Protobuf.extract(resp)
            return self._auth_failed('%s: %s' % (getattr(err, 'errorCode', '?'),
                                                 getattr(err, 'description', '')))
        if resp.payloadType != ProtoOAPayloadType.PROTO_OA_ACCOUNT_AUTH_RES:
            return self._auth_failed('unexpected auth reply payloadType=%s'
                                     % resp.payloadType)
        self._auth_error = None
        self._authed.set()
        if self._heartbeat is None:
            self._heartbeat = LoopingCall(self._send_heartbeat)
            self._heartbeat.start(HEARTBEAT_SECS, now=False)
        print('[cTrader] authenticated account=%s env=%s' %
              (self.account_id, self.env_name), flush=True)

    def _auth_failed(self, failure) -> None:
        self._auth_error = 'cTrader auth failed: %s' % failure
        print('[cTrader] %s' % self._auth_error, flush=True)

    def _on_disconnected(self, _client, reason) -> None:
        # ClientService reconnects on its own with backoff; clearing the flag means a
        # call made before re-auth raises instead of quietly returning stale data.
        self._authed.clear()
        print('[cTrader] disconnected (%s) — will reconnect and re-auth' % reason, flush=True)

    def _send_heartbeat(self) -> None:
        try:
            if self._client is not None and self._client.isConnected:
                d = self._client.send(ProtoHeartbeatEvent())
                # A heartbeat gets NO reply, so its deferred ALWAYS times out at the
                # SDK's 5s default. The failure is asynchronous, so a try/except here
                # cannot catch it — without an errback Twisted logs an unhandled
                # TimeoutError every HEARTBEAT_SECS and buries real errors in the log.
                d.addErrback(lambda _failure: None)
        except Exception:                               # noqa: BLE001 — never kill the loop
            pass

    def _on_message(self, _client, message) -> None:
        if message.payloadType != ProtoOAPayloadType.PROTO_OA_SPOT_EVENT:
            return
        event = Protobuf.extract(message)
        waiter = self._price_waiters.get(event.symbolId)
        if waiter is not None and not waiter.full():
            waiter.put(event)

    # --- request/response ---

    def send(self, req, timeout: int = 10):
        """Send a request and block for its response. Returns the extracted payload."""
        if not self._authed.is_set():
            raise CTraderError('cTrader client is not authenticated')

        box = queue.Queue(1)                    # type: queue.Queue

        def _fire():
            d = self._client.send(req, responseTimeoutInSeconds=timeout)
            d.addCallbacks(lambda m: box.put(('ok', m)),
                           lambda f: box.put(('err', f)))

        reactor.callFromThread(_fire)
        try:
            kind, payload = box.get(timeout=timeout + 5)
        except queue.Empty:
            raise TimeoutError('cTrader request timed out after %ss' % timeout)

        if kind == 'err':
            raise CTraderError('cTrader request failed: %s' % payload)

        if payload.payloadType == ProtoOAPayloadType.PROTO_OA_ERROR_RES:
            err = Protobuf.extract(payload)
            raise CTraderError('%s: %s' % (getattr(err, 'errorCode', '?'),
                                           getattr(err, 'description', '')))
        return Protobuf.extract(payload)

    # --- read-only queries ---

    def get_trader(self) -> Dict:
        req = ProtoOATraderReq()
        req.ctidTraderAccountId = self.account_id
        trader = self.send(req).trader
        digits = getattr(trader, 'moneyDigits', 2)
        rights = getattr(trader, 'accessRights', None)
        return {
            'account_id': self.account_id,
            'balance': trader.balance / (10 ** digits),
            'money_digits': digits,
            'access_rights': rights,
            'access_rights_name': ACCESS_RIGHTS.get(rights, 'UNKNOWN'),
        }

    def get_symbols(self) -> Dict[int, str]:
        req = ProtoOASymbolsListReq()
        req.ctidTraderAccountId = self.account_id
        return {s.symbolId: s.symbolName for s in self.send(req).symbol}

    def get_symbol_details(self, symbol_ids: List[int]) -> Dict[int, Dict]:
        """Per-symbol volume specs. FIX cannot supply these at all — the VOL_SPEC gap."""
        req = ProtoOASymbolByIdReq()
        req.ctidTraderAccountId = self.account_id
        req.symbolId.extend(list(symbol_ids))
        out = {}
        for sym in self.send(req).symbol:
            self._digits[sym.symbolId] = sym.digits
            out[sym.symbolId] = {
                'symbol_id': sym.symbolId,
                'digits': sym.digits,
                'pip_position': getattr(sym, 'pipPosition', None),
                'min_volume': getattr(sym, 'minVolume', None),
                'step_volume': getattr(sym, 'stepVolume', None),
                'max_volume': getattr(sym, 'maxVolume', None),
                'lot_size': getattr(sym, 'lotSize', None),
            }
        return out

    def get_positions(self) -> List[Dict]:
        req = ProtoOAReconcileReq()
        req.ctidTraderAccountId = self.account_id
        res = self.send(req)
        positions = []
        for pos in res.position:
            trade = pos.tradeData
            positions.append({
                'position_id': pos.positionId,
                'symbol_id': trade.symbolId,
                'side': 'BUY' if trade.tradeSide == 1 else 'SELL',
                'volume': trade.volume,
                'entry_price': pos.price,
                # 0 on the wire means "no stop", never a real price of zero
                'sl': pos.stopLoss or None,
                'tp': pos.takeProfit or None,
            })
        return positions

    def get_price(self, symbol_id: int, timeout: int = 15) -> Tuple[float, float]:
        """Live (bid, ask). Raises on timeout — a closed market yields no ticks."""
        if symbol_id not in self._digits:
            self.get_symbol_details([symbol_id])
        digits = self._digits[symbol_id]

        box = queue.Queue(1)                    # type: queue.Queue
        self._price_waiters[symbol_id] = box
        try:
            req = ProtoOASubscribeSpotsReq()
            req.ctidTraderAccountId = self.account_id
            req.symbolId.extend([symbol_id])
            self.send(req, timeout=timeout)

            bid = ask = None
            deadline = time.time() + timeout
            while time.time() < deadline:
                try:
                    event = box.get(timeout=max(0.1, deadline - time.time()))
                except queue.Empty:
                    break
                # ticks carry bid or ask or both; hold the last seen of each
                if getattr(event, 'bid', 0):
                    bid = event.bid / (10 ** digits)
                if getattr(event, 'ask', 0):
                    ask = event.ask / (10 ** digits)
                if bid is not None and ask is not None:
                    return bid, ask
                box = queue.Queue(1)
                self._price_waiters[symbol_id] = box
            if bid is not None or ask is not None:
                return bid or ask, ask or bid
            raise TimeoutError(
                'no tick for symbolId %s in %ss — market may be closed' % (symbol_id, timeout))
        finally:
            self._price_waiters.pop(symbol_id, None)


# ── singleton ────────────────────────────────────────────────────────────────

_client_singleton = None                        # type: Optional[CTraderClient]
_singleton_lock = threading.Lock()


def get_client() -> CTraderClient:
    """The one client for this account. Mirrors fix_adapter._FixSession."""
    global _client_singleton
    with _singleton_lock:
        if _client_singleton is None:
            _client_singleton = CTraderClient()
    return _client_singleton


# ── selftest ─────────────────────────────────────────────────────────────────

def _selftest() -> int:
    client = get_client().start()

    trader = client.get_trader()
    print('account      : %s' % trader['account_id'])
    print('accessRights : %s (%s)' % (trader['access_rights'], trader['access_rights_name']))
    print('balance      : %.2f' % trader['balance'])

    symbols = client.get_symbols()
    print('symbols      : %d' % len(symbols))

    details = client.get_symbol_details([1, 6, 109])
    for sid in (1, 6, 109):
        det = details.get(sid)
        if not det:
            print('SELFTEST FAIL: symbolId %s did not resolve' % sid)
            return 1
        print('  id=%-4s %-8s minVolume=%-8s stepVolume=%s'
              % (sid, symbols.get(sid, '?'), det['min_volume'], det['step_volume']))

    positions = client.get_positions()
    print('positions    : %d' % len(positions))
    for pos in positions:
        print('  posId=%s symbolId=%s %s vol=%s entry=%s sl=%s'
              % (pos['position_id'], pos['symbol_id'], pos['side'],
                 pos['volume'], pos['entry_price'], pos['sl']))

    print('SELFTEST PASS')
    return 0


if __name__ == '__main__':
    import argparse

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--selftest', action='store_true',
                        help='connect, authenticate and read account state (places nothing)')
    args = parser.parse_args()

    if not args.selftest:
        parser.error('nothing to do — pass --selftest')

    try:
        raise SystemExit(_selftest())
    except SystemExit:
        raise
    except Exception as exc:                            # noqa: BLE001 — selftest reports, never traces
        print('SELFTEST FAIL: %s' % exc)
        raise SystemExit(1)
