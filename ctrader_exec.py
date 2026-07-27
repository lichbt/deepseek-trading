"""
cTrader execution adapter — drop-in for FixAdapter's order surface.

Deliberately mirrors the method names fix_runner already calls (execute_order,
close_position, place_stop, cancel_stop, get_current_price, open_pos_ids) so the
runner keeps its sleeve loading, sizing, min-lot cap, orphan sweep and state model
unchanged. Swapping venue should be an import change, not a rewrite.

Three things are structurally better here than over FIX:

  * Stops are ATTACHED TO THE POSITION (ProtoOAAmendPositionSLTPReq), not standalone
    opposite-side orders. There is nothing to leave working behind a closed position,
    and no OrderCancelRequest dialect to get wrong.
  * Closing is BY positionId (ProtoOAClosePositionReq). A close aimed at a position
    that no longer exists is rejected as such — it cannot execute as a fresh OPEN, the
    way a FIX close with a stale PosMaintRptID(721) does.
  * Volume specs come from the venue (minVolume/stepVolume/lotSize), so sizing is not
    guessed from a hand-maintained table.

VOLUME UNITS: cTrader wire volume is CENTI-UNITS — units x 100. Verified live on
2026-07-27: FIX state 1000.0 units reads back as broker volume 100000.0.
"""

import time
from typing import Dict, Optional

from ctrader_open_api.messages.OpenApiMessages_pb2 import (
    ProtoOANewOrderReq,
    ProtoOAClosePositionReq,
    ProtoOAAmendPositionSLTPReq,
    ProtoOACancelOrderReq,
)
from ctrader_open_api.messages.OpenApiModelMessages_pb2 import (
    ProtoOAOrderType,
    ProtoOATradeSide,
)

from ctrader_client import get_client, CTraderError

# units -> wire volume
UNITS_TO_VOLUME = 100.0


class CTraderExecAdapter:
    """Order surface for ONE instrument, matching FixAdapter's shape."""

    def __init__(self, instrument: str, symbol_id: int,
                 min_volume: float, step_volume: float, digits: int):
        self.instrument = instrument
        self.symbol_id = int(symbol_id)
        self.min_volume = float(min_volume)
        self.step_volume = float(step_volume)
        self.digits = int(digits)
        self.client = get_client().start()

    # ── volume helpers ───────────────────────────────────────────────────

    def _to_volume(self, units: float) -> int:
        """units -> wire volume, snapped to stepVolume and floored at minVolume.

        Mirrors fix_runner.round_vol: the venue minimum wins. The runner separately
        refuses to open when that minimum implies more risk than MAXRISK, so flooring
        here cannot silently oversize a sleeve past the cap.
        """
        vol = abs(units) * UNITS_TO_VOLUME
        stepped = round(vol / self.step_volume) * self.step_volume
        return int(max(stepped, self.min_volume))

    def _round_px(self, px: float) -> float:
        return round(float(px), self.digits)

    # ── orders ───────────────────────────────────────────────────────────

    def execute_order(self, units: float, comment: str,
                      stop_loss: Optional[float] = None) -> Optional[str]:
        """MARKET order with the stop ATTACHED. Returns positionId as str, or None."""
        if units == 0:
            return None
        req = ProtoOANewOrderReq()
        req.ctidTraderAccountId = self.client.account_id
        req.symbolId = self.symbol_id
        req.orderType = ProtoOAOrderType.MARKET
        req.tradeSide = ProtoOATradeSide.BUY if units > 0 else ProtoOATradeSide.SELL
        req.volume = self._to_volume(units)
        if stop_loss is not None:
            # Attaching at entry is the whole point: the stop exists at the broker from
            # the moment the position does, so a dead runner cannot strand it unstopped.
            req.stopLoss = self._round_px(stop_loss)
        req.label = str(comment)[:50]

        before = set(self.open_pos_ids())
        self.client.send(req, timeout=15)

        # The fill may arrive as a separate execution event, so confirm from the book
        # rather than trusting the first response. Reconcile is authoritative.
        for _ in range(10):
            time.sleep(0.5)
            new = set(self.open_pos_ids()) - before
            if new:
                return sorted(new)[-1]
        return None

    def close_position(self, pos_id: str, units: float,
                       orig_side: int = 0) -> Optional[dict]:
        """Close BY positionId. orig_side is accepted for FixAdapter parity, unused."""
        req = ProtoOAClosePositionReq()
        req.ctidTraderAccountId = self.client.account_id
        req.positionId = int(pos_id)
        req.volume = self._to_volume(units)
        try:
            self.client.send(req, timeout=15)
        except CTraderError as exc:
            # Shape the failure like FixAdapter's reject ack so the runner's existing
            # guards (which check ord_status) keep working unchanged.
            return {'ord_status': '8', 'reject': str(exc)}
        for _ in range(10):
            time.sleep(0.5)
            if str(pos_id) not in self.open_pos_ids():
                return {'ord_status': '2', 'pos_id': str(pos_id)}
        return {'ord_status': '8', 'reject': 'position still open after close'}

    # ── stops (attached, not standalone orders) ──────────────────────────

    def place_stop(self, pos_id: str, units: float, orig_side: int,
                   stop_px: float) -> Optional[dict]:
        """Set the position's stop. No separate order is created."""
        req = ProtoOAAmendPositionSLTPReq()
        req.ctidTraderAccountId = self.client.account_id
        req.positionId = int(pos_id)
        req.stopLoss = self._round_px(stop_px)
        try:
            self.client.send(req, timeout=15)
        except CTraderError as exc:
            return {'ord_status': '8', 'reject': str(exc)}
        # ref is the positionId itself — there is no distinct stop-order id to track,
        # which is precisely the FIX failure mode this removes.
        return {'ord_status': '0', 'ref': str(pos_id)}

    def cancel_stop(self, ref, orig_side: int = 0) -> Optional[dict]:
        """Clear a stop. Returns None on failure (the runner refuses to close on None).

        Handles BOTH shapes, because the state file predates the venue switch:

        * LEGACY FIX ref — a dict carrying `order_id` for a STANDALONE resting stop
          order (type 3). Those still exist at the broker for positions opened over
          FIX, and they must be CANCELLED as orders. Amending the position's SL would
          leave the resting order alive, and a stop that outlives its position fires
          as a naked entry — the exact failure this migration exists to remove.
        * cTrader ref — the positionId itself, where the stop is an attached SL and
          clearing it means amending with stopLoss unset (verified on 4384365).
        """
        order_id = None
        if isinstance(ref, dict):
            order_id = ref.get('order_id')
        elif isinstance(ref, str) and not ref.isdigit():
            return None

        if order_id:
            req = ProtoOACancelOrderReq()
            req.ctidTraderAccountId = self.client.account_id
            req.orderId = int(order_id)
            try:
                self.client.send(req, timeout=15)
            except CTraderError as exc:
                # Already gone counts as cancelled — do not block the close on it.
                if 'NOT_FOUND' in str(exc).upper():
                    return {'ord_status': '0', 'ref': str(order_id)}
                return None
            return {'ord_status': '0', 'ref': str(order_id)}

        req = ProtoOAAmendPositionSLTPReq()
        req.ctidTraderAccountId = self.client.account_id
        req.positionId = int(ref)
        try:
            self.client.send(req, timeout=15)
        except CTraderError:
            return None
        return {'ord_status': '0', 'ref': str(ref)}

    # ── reads ────────────────────────────────────────────────────────────

    def open_pos_ids(self) -> Dict[str, float]:
        """{positionId: signed units} for this account (all symbols, like FixAdapter)."""
        out = {}
        for pos in self.client.get_positions():
            units = pos['volume'] / UNITS_TO_VOLUME
            out[str(pos['position_id'])] = units if pos['side'] == 'BUY' else -units
        return out

    def position_sl(self, pos_id: str) -> Optional[float]:
        for pos in self.client.get_positions():
            if str(pos['position_id']) == str(pos_id):
                return pos['sl']
        return None

    def get_current_price(self) -> Optional[float]:
        try:
            bid, ask = self.client.get_price(self.symbol_id)
        except Exception:
            return None
        return (bid + ask) / 2.0


def adapter_for(instrument: str, symbols_json: Dict) -> CTraderExecAdapter:
    """Build an adapter from ctrader_symbols.json's spec for `instrument`."""
    spec = (symbols_json.get('instruments', {}).get(instrument)
            or symbols_json.get('unmapped_but_available', {}).get(instrument))
    if not spec:
        raise ValueError('%s has no cTrader symbol — cannot route' % instrument)
    return CTraderExecAdapter(instrument, spec['symbol_id'], spec['min_volume'],
                              spec['step_volume'], spec['digits'])
