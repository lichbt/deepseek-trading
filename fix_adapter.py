"""FixAdapter — cTrader FIX 4.4 implementation of the BrokerAdapter interface.

    pip install simplefix

simplefix builds/parses FIX messages; the SESSION layer (logon, heartbeat,
seqnum, reader thread) is hand-rolled below. Test against cTrader DEMO FIX before
the funded account. Values marked  <<FILL: ...>>  come from your The5ers FIX
spec sheet — do NOT guess them on a live account.

CEILINGS (ponytail — harden before funded):
  * reconnect + ResendRequest/gap-fill on a sequence gap is stubbed (see _reader).
    A daily-bar algo tolerates a reconnect between bars, but a funded account
    needs it solid. Add before going live.
  * cTrader routes SL as a SEPARATE stop order; if the market order fills and the
    stop send fails you are unhedged — the code raises so the caller can retry.
"""
import os, socket, threading, time, uuid, ssl
from typing import Optional, Dict
import pandas as pd
try:
    import simplefix
except ImportError:
    raise ImportError("pip install simplefix")

from ctrader_adapter import BrokerAdapter

# OANDA instrument -> cTrader FIX numeric Symbol(55). cTrader FIX requires a NUMERIC
# symbol id (name 'EURUSD' is rejected: "Symbol(55) must be numeric"). Harvested from
# SecurityListRequest on The5ers live FIX 2026-07-17 — these ids are broker/server-specific.
_FIX_SYMBOL_ID = {
    'EUR_USD': '1', 'GBP_USD': '2', 'EUR_JPY': '3', 'USD_JPY': '4', 'AUD_USD': '5',
    'USD_CHF': '6', 'GBP_JPY': '7', 'EUR_GBP': '9', 'XAU_USD': '41', 'XAG_USD': '42',
    'XPD_USD': '95', 'XPT_USD': '97', 'WTICO_USD': '99', 'BTC_USD': '101',
    'SPX500_USD': '104', 'NAS100_USD': '106', 'DE30_EUR': '109', 'XCU_USD': '118',
    'AU200_AUD': '125', 'HK33_HKD': '126', 'NATGAS_USD': '132',
    # NOT offered on The5ers cTrader: WHEAT_USD, SOYBN_USD (no ags) — those sleeves can't route here.
}
_CCY_PAIR_ID = {'JPY': '4', 'CHF': '6', 'EUR': '1', 'GBP': '2', 'AUD': '5'}   # for quote_to_usd

# ── config — The5ers cTrader FIX (TRADE session). Password ONLY via env. ─────
FIX_HOST      = os.getenv('FIX_HOST', 'live-uk-eqx-01.p.c-trader.com')
FIX_PORT      = int(os.getenv('FIX_PORT', '5212'))           # 5212 SSL / 5202 plain
FIX_SENDER    = os.getenv('FIX_SENDER_COMP_ID', 'live.the5ers.5047309')
FIX_TARGET    = os.getenv('FIX_TARGET_COMP_ID', 'cServer')
FIX_SENDERSUB = os.getenv('FIX_SENDER_SUB_ID', 'TRADE')      # TRADE session (QUOTE = second session, not needed)
FIX_TARGETSUB = os.getenv('FIX_TARGET_SUB_ID', 'TRADE')      # cTrader also wants TargetSubID(57)
FIX_USER      = os.getenv('FIX_USERNAME', '5047309')         # account number
FIX_PASSWORD  = os.getenv('FIX_PASSWORD', '')                # <<SET env FIX_PASSWORD — never hardcode
FIX_START_EQUITY = float(os.getenv('FIX_START_EQUITY', '100000'))  # manual account size (FIX has no NAV)
BEGIN_STRING  = 'FIX.4.4'
HEARTBEAT     = 30

def _clid() -> str:
    return uuid.uuid4().hex[:20]


# ── session: one persistent socket per account, shared across all sleeves ────
class _FixSession:
    _instance = None
    _lock = threading.Lock()

    @classmethod
    def get(cls, start_equity: float):
        with cls._lock:
            if cls._instance is None:
                cls._instance = cls(start_equity)
            return cls._instance

    def __init__(self, start_equity: float):
        self.parser = simplefix.FixParser()
        self.out_seq = 1
        self.in_seq = 1                         # next expected inbound MsgSeqNum(34)
        self.sock: Optional[socket.socket] = None
        self.connected = threading.Event()
        self._io = threading.Lock()
        self._recon = threading.Lock()
        # live caches, updated by the reader thread
        self.quotes: Dict[str, float] = {}      # symbol -> mid
        self.positions: Dict[str, float] = {}   # symbol -> signed units
        self.entry: Dict[str, float] = {}       # symbol -> avg entry px
        self.exec_reports: Dict[str, dict] = {}
        self.open_pos: Dict[str, float] = {}    # PosID -> signed qty (from RequestForPositions)
        self._pos_snap: Dict[str, float] = {}; self._pos_n = 0; self._pos_total = None
        self._pos_ev = threading.Event()
        # self-tracked equity (no NAV in FIX)
        self.start_equity = start_equity
        self.realized_pnl = 0.0
        self._acks: Dict[str, dict] = {}        # clordid -> ExecReport, for blocking sends
        self._ack_ev: Dict[str, threading.Event] = {}
        self._reconnect()                       # connect + logon + confirm + reconcile (with retry)
        threading.Thread(target=self._reader, daemon=True).start()
        threading.Thread(target=self._heartbeat, daemon=True).start()

    # --- equity (matches the sketch) ---
    def unrealized(self) -> float:
        u = 0.0
        for s, units in self.positions.items():
            px, ep = self.quotes.get(s), self.entry.get(s)
            if px and ep and units:
                u += units * (px - ep)          # + quote->USD in real code
        return u
    def equity(self) -> float:
        return self.start_equity + self.realized_pnl + self.unrealized()
    def reconcile(self, broker_balance: float):
        self.realized_pnl = 0.0; self.start_equity = broker_balance

    # --- wire ---
    def _connect(self):
        raw = socket.create_connection((FIX_HOST, FIX_PORT), timeout=10)
        self.sock = ssl.create_default_context().wrap_socket(raw, server_hostname=FIX_HOST)

    def _base(self, msg_type: str) -> simplefix.FixMessage:
        m = simplefix.FixMessage()
        m.append_pair(8, BEGIN_STRING)
        m.append_pair(35, msg_type)
        m.append_pair(49, FIX_SENDER)
        m.append_pair(56, FIX_TARGET)
        m.append_pair(50, FIX_SENDERSUB)
        m.append_pair(57, FIX_TARGETSUB)
        m.append_pair(34, self.out_seq)
        m.append_utc_timestamp(52)
        return m

    def _send(self, m: simplefix.FixMessage):
        with self._io:
            self.sock.sendall(m.encode())         # simplefix computes 9=len and 10=checksum
            self.out_seq += 1

    def _establish(self):
        """Connect + logon with ResetSeqNumFlag=Y (both seqnums restart at 1, so there
        is NO gap to resend), synchronously confirm the 35=A reply, then re-request
        positions to reconcile state after the gap. Raises on any failure."""
        self._connect()
        self.parser = simplefix.FixParser()        # fresh parser for the new socket
        self.out_seq = 1; self.in_seq = 1
        m = self._base('A')
        m.append_pair(98, 0)                       # EncryptMethod=None
        m.append_pair(108, HEARTBEAT)
        m.append_pair(141, 'Y')                    # ResetSeqNumFlag -> both sides restart at 1
        m.append_pair(553, FIX_USER)
        m.append_pair(554, FIX_PASSWORD)
        self._send(m)
        self.sock.settimeout(10)                   # confirm logon synchronously (reader not yet reading)
        while True:
            data = self.sock.recv(4096)
            if not data:
                raise ConnectionError('closed during logon')
            self.parser.append_buffer(data)
            r = self.parser.get_message()
            if r is None:
                continue
            mt = r.get(35).decode()
            if mt == 'A':
                self.in_seq = int(r.get(34)) + 1
                break
            raise ConnectionError(f'logon rejected: 35={mt} {(r.get(58) or b"").decode()}')
        self.sock.settimeout(None)
        self.connected.set()
        self.request_positions()                   # reconcile after (re)connect

    def _reconnect(self):
        """Serialize reconnects; retry _establish with backoff until it succeeds."""
        with self._recon:
            if self.connected.is_set():
                return                             # another thread already recovered
            delay = 2
            while True:
                try:
                    self._establish()
                    print("[FIX] session (re)established + positions re-requested")
                    return
                except Exception as e:
                    print(f"[FIX] connect/logon failed ({e}); retry in {delay}s")
                    time.sleep(delay); delay = min(delay * 2, 30)

    def _heartbeat(self):
        while True:
            time.sleep(HEARTBEAT)
            if not self.connected.is_set():
                continue
            try:
                self._send(self._base('0'))
            except Exception:
                self.connected.clear()             # reader will drive the reconnect

    def _reader(self):
        while True:
            try:
                data = self.sock.recv(4096)
                if not data:
                    raise ConnectionError('FIX socket closed')
                self.parser.append_buffer(data)
                while True:
                    m = self.parser.get_message()
                    if m is None:
                        break
                    seq = int(m.get(34) or 0)      # detect gaps: a daily algo reconnects+reconciles
                    if seq < self.in_seq:
                        continue                   # dup/replay — ignore
                    if seq > self.in_seq:
                        raise ConnectionError(f'seqnum gap (got {seq}, want {self.in_seq})')
                    self.in_seq += 1
                    self._dispatch(m)
            except Exception as e:
                print(f"[FIX] reader: {e} — reconnecting")
                self.connected.clear()
                self._reconnect()                  # blocks until back, then resume reading

    def _dispatch(self, m):
        t = m.get(35).decode()
        if t in ('5', '2'):                         # server Logout / ResendRequest ->
            raise ConnectionError(f'server sent 35={t} — reconnect (reset-seq logon, no replay)')
        if t == '1':                                # TestRequest -> Heartbeat with TestReqID
            hb = self._base('0'); hb.append_pair(112, m.get(112)); self._send(hb)
        elif t == 'W':                              # MarketData snapshot
            sym = m.get(55).decode()
            bid = ask = None
            # NoMDEntries group parse (269 type, 270 px) — simplefix repeats tags in order
            # <<FILL: cTrader MD group layout; below is the FIX 4.4 default
            self.quotes[sym] = self._parse_mid(m) or self.quotes.get(sym)
        elif t == '8':                              # ExecutionReport — the fill certainty
            self._on_exec(m)
        elif t == 'AP':                             # PositionReport: 721=PosID, 704/705=long/short qty
            pid = m.get(721)
            if pid is not None:                     # a real open position (flat report has no 721)
                self._pos_snap[pid.decode()] = float(m.get(704) or 0) - float(m.get(705) or 0)
            self._pos_n += 1
            tot = m.get(727)
            if tot is not None: self._pos_total = int(tot)
            if self._pos_total is not None and self._pos_n >= max(self._pos_total, 1):
                self.open_pos = dict(self._pos_snap)   # publish the completed snapshot
                self._pos_ev.set()

    def _parse_mid(self, m):
        # minimal: pull first bid(269=0)/offer(269=1) 270 pair. Real parse walks the group.
        try:
            px = float(m.get(270)); return px       # <<FILL: proper bid/ask group walk -> mid
        except Exception:
            return None

    def _on_exec(self, m):
        clid = (m.get(11) or b'').decode()
        sym  = m.get(55).decode()
        side = m.get(54).decode()                   # 1 buy / 2 sell
        last_qty = float(m.get(32) or 0)            # LastQty
        last_px  = float(m.get(6) or m.get(31) or 0)  # cTrader fills carry AvgPx(6), not LastPx(31)
        rpt = {'ord_status': (m.get(39) or b'').decode(),
               'order_id': (m.get(37) or b'').decode(),
               'clid': clid,                        # for OrderCancelRequest (cancel a pending stop)
               'pos_id': (m.get(721) or b'').decode()}  # PosID = tag 721 (confirmed on live fill)
        self.exec_reports[rpt['order_id'] or clid] = rpt
        if last_qty:                                # update position + avg entry
            signed = last_qty if side == '1' else -last_qty
            prev = self.positions.get(sym, 0.0)
            new = prev + signed
            if prev == 0 or (prev > 0) == (signed > 0):        # opening/adding
                pe = self.entry.get(sym, last_px)
                self.entry[sym] = (pe*abs(prev) + last_px*abs(signed)) / max(abs(new), 1e-9)
            else:                                              # closing -> realize P&L
                self.realized_pnl += (last_px - self.entry.get(sym, last_px)) * (-signed)
                if new == 0: self.entry.pop(sym, None)
            self.positions[sym] = new
        if clid in self._ack_ev:                    # unblock a waiting send
            self._acks[clid] = rpt; self._ack_ev[clid].set()

    # --- outbound order helpers (block until the ExecReport) ---
    def _order(self, symbol, side, qty, ord_type, stop_px=None, pos_id=None) -> Optional[dict]:
        clid = _clid(); ev = threading.Event(); self._ack_ev[clid] = ev
        m = self._base('D')
        m.append_pair(11, clid)
        m.append_pair(55, symbol)                   # numeric cTrader symbol id (see _FIX_SYMBOL_ID)
        m.append_pair(54, side)
        m.append_pair(38, abs(qty))
        m.append_pair(40, ord_type)                 # 1=market 3=stop
        if stop_px is not None: m.append_pair(99, stop_px)
        if pos_id is not None: m.append_pair(721, pos_id)  # HEDGING close: reference PosMaintRptID
        # TIF by type: market=IOC(3) fills-or-dies; STOP=GTC(1) must REST as a pending
        # order (IOC would cancel it instantly since a stop can't fill on arrival — this
        # is why stops weren't attaching).
        m.append_pair(59, '1' if str(ord_type) == '3' else '3')
        m.append_utc_timestamp(60)
        self._send(m)
        ok = ev.wait(timeout=10)
        self._ack_ev.pop(clid, None)
        return self._acks.pop(clid, None) if ok else None

    def request_positions(self):
        """Startup snapshot -> PositionReport(s) 35=AP refresh self.positions.
        VERIFIED on The5ers live FIX: send ONLY PosReqID(710) — cTrader rejects
        PosReqType(724) and SubscriptionRequestType(263) as invalid tags. Reply AP
        carries 727=TotalNumPosReports, 728=PosReqResult (2 = flat / no positions)."""
        m = self._base('AN')
        m.append_pair(710, _clid())
        self._send(m)

    def reconcile_positions(self, timeout=6) -> Dict[str, float]:
        """Fresh snapshot {PosID -> signed qty} of what's ACTUALLY open at the broker.
        Lets the runner detect positions closed out-of-band (broker SL fired, manual
        close). Verified AP format: 721=PosID, 704/705=long/short, 727=total count."""
        self._pos_snap = {}; self._pos_n = 0; self._pos_total = None; self._pos_ev.clear()
        m = self._base('AN'); m.append_pair(710, _clid()); self._send(m)
        self._pos_ev.wait(timeout)
        return dict(self.open_pos)

    def cancel(self, orig_clid, order_id, symbol, side):
        """OrderCancelRequest(35=F) — cancel a pending order (e.g. a protective stop)
        so a signal-close doesn't orphan it (an orphaned stop later fires -> new hedge)."""
        m = self._base('F')
        m.append_pair(11, _clid())        # new ClOrdID
        m.append_pair(41, orig_clid)      # OrigClOrdID (the stop's)
        if order_id: m.append_pair(37, order_id)
        m.append_pair(55, symbol)
        m.append_pair(54, side)
        m.append_utc_timestamp(60)
        self._send(m)


# ── adapter: same 6-method surface as OandaAdapter / CTraderAdapter ──────────
class FixAdapter(BrokerAdapter):
    def __init__(self, instrument: str):
        super().__init__(instrument)
        self.symbol = _FIX_SYMBOL_ID.get(instrument)   # numeric id string, e.g. '1'
        if self.symbol is None:
            raise ValueError(f"{instrument}: no cTrader FIX symbol on this broker (not offered)")
        self.fix = _FixSession.get(FIX_START_EQUITY)

    def execute_order(self, units, comment, stop_loss=None) -> Optional[str]:
        """OPEN a position. Returns the cTrader PosID (721) — the caller MUST store it
        per sleeve; on a hedging account it's the only handle to close this exact
        position later (see close_position). units>0 buy, <0 sell."""
        side = '1' if units > 0 else '2'
        rep = self.fix._order(self.symbol, side, units, '1')     # market
        if rep is None:
            return None
        if stop_loss is not None:
            sl = self.fix._order(self.symbol, '2' if side == '1' else '1', units, '3', stop_px=stop_loss)
            if sl is None:
                raise RuntimeError(f"{self.symbol}: filled but STOP rejected — position UNHEDGED, retry")
        return rep.get('pos_id') or rep.get('order_id')

    def close_position(self, pos_id: str, units: float, orig_side: int) -> Optional[dict]:
        """HEDGING close: opposite-side market order, SAME volume, referencing the
        position's PosMaintRptID(721). Without the 721 ref this would open a new hedge
        instead of closing (confirmed via cTrader FIX spec). orig_side: +1 was long, -1 short."""
        opp = '2' if orig_side > 0 else '1'
        return self.fix._order(self.symbol, opp, abs(units), '1', pos_id=pos_id)

    def place_stop(self, pos_id, units, orig_side, stop_px) -> Optional[dict]:
        """BROKER-SIDE protective stop: opposite-side STOP order (40=3) linked to the
        position via 721. Survives a script crash and shows on the position. Returns
        the stop order ref {clid, order_id} — store it to CANCEL on a signal-close, or
        the pending stop orphans and later opens a hedge. NOTE cTrader's 721-stop is
        reportedly finicky — VERIFY it attaches (position shows SL) on the first live use."""
        opp = 2 if orig_side > 0 else 1
        rep = self.fix._order(self.symbol, str(opp), abs(units), '3', stop_px=stop_px, pos_id=pos_id)
        # A working stop reports New/PendingNew; Rejected(8)/Canceled(4)/Expired(C) = it did
        # NOT attach. Return None so the caller knows the position is on software-stop only.
        if rep is None or rep.get('ord_status') in ('8', '4', 'C'):
            return None
        return rep

    def cancel_stop(self, ref, orig_side):
        """Cancel the protective stop placed by place_stop (its side was opposite the position)."""
        if ref:
            self.fix.cancel(ref.get('clid'), ref.get('order_id'), self.symbol, str(2 if orig_side > 0 else 1))

    def open_pos_ids(self) -> Dict[str, float]:
        return self.fix.reconcile_positions()

    def get_current_price(self) -> Optional[float]:
        return self.fix.quotes.get(self.symbol)

    def get_current_units(self, min_units=1.0) -> float:
        return abs(self.fix.positions.get(self.symbol, 0.0))

    def get_trade_state(self, trade_id: str) -> Optional[Dict]:
        return self.fix.exec_reports.get(trade_id)

    def get_account_summary(self) -> Dict:
        return {'equity': self.fix.equity(),
                'positions': [{'instrument': s, 'units': u}
                              for s, u in self.fix.positions.items() if u]}

    def fetch_candles(self, count=500, granularity='D', since_time=None) -> pd.DataFrame:
        raise NotImplementedError("no history over FIX — keep your existing OHLC source")

    def quote_to_usd_rate(self) -> float:
        q = self.instrument.split('_')[1]
        if q == 'USD': return 1.0
        mid = self.fix.quotes.get(_CCY_PAIR_ID.get(q))    # quotes keyed by numeric symbol id
        invert = q in ('JPY', 'CHF')
        fb = {'JPY': 1/150., 'CHF': 1/0.91, 'EUR': 1.08, 'GBP': 1.25, 'AUD': 0.66}[q]
        return (1.0/mid if invert else mid) if mid else fb


# ── no-network self-check: message build + parse round-trips (run: python fix_adapter.py) ──
def _demo():
    m = simplefix.FixMessage()
    for t, v in [(8, 'FIX.4.4'), (35, 'D'), (11, 'abc'), (55, 'EURUSD'),
                 (54, '1'), (38, 1000), (40, '1'), (99, 1.0850)]:
        m.append_pair(t, v)
    raw = m.encode()
    p = simplefix.FixParser(); p.append_buffer(raw); back = p.get_message()
    assert back.get(35) == b'D' and back.get(55) == b'EURUSD' and back.get(54) == b'1'
    assert float(back.get(99)) == 1.0850
    # equity math
    s = _FixSession.__new__(_FixSession)
    s.positions = {'EURUSD': 1000}; s.entry = {'EURUSD': 1.08}; s.quotes = {'EURUSD': 1.09}
    s.start_equity = 100000; s.realized_pnl = 0.0
    assert abs(s.equity() - (100000 + 1000*0.01)) < 1e-6
    print("ok: message round-trip + equity tracking")

if __name__ == '__main__':
    _demo()
