"""place_stop -> state -> cancel_stop must round-trip.

The 2026-07-28 production failure: place_stop returns {'ord_status','ref'} and
fix_runner stores that dict verbatim as st['stop_ref'], but cancel_stop only
understood a dict carrying 'order_id' (the legacy FIX standalone-stop shape). It
fell through to the amend branch and did int(<dict>), raising TypeError — which
the runner catches as "sleeve skipped this pass". Every cTrader position in the
book became unable to close on a signal flip while its stop was attached.

Nothing checked that the two functions agreed on a shape, so these tests assert
the round-trip directly rather than either half in isolation.
"""
import pytest

import ctrader_exec
from ctrader_exec import CTraderExecAdapter


class FakeClient:
    """Records the protobuf requests, and keeps a broker-side book.

    The book exists because place_stop now CONFIRMS: it reads the stop back off
    the position and reports a reject unless the broker really holds it. A fake
    that only records requests can no longer stand in for a broker — it cannot
    tell "amend applied" from "amend refused", which is the whole defect.

    `applies` models the refusal: when False the amend is accepted at the
    transport (send does not raise, exactly as the real client behaves on an
    order-error event) but the position's SL never changes.
    """

    account_id = 47916240

    def __init__(self, book=None, applies=True):
        self.sent = []
        self.applies = applies
        self.book = book if book is not None else {
            '4387582': {'sl': None, 'entry_price': 24950.0},
        }

    def send(self, req, timeout=None):
        self.sent.append(req)
        pos = self.book.get(str(getattr(req, 'positionId', '')))
        if pos is not None and isinstance(req, ctrader_exec.ProtoOAAmendPositionSLTPReq):
            if self.applies:
                pos['sl'] = req.stopLoss if req.HasField('stopLoss') else None
        return req

    def get_positions(self):
        return [{'position_id': pid, 'symbol_id': 10, 'side': 'BUY',
                 'volume': 2, 'entry_price': v['entry_price'], 'sl': v['sl'],
                 'tp': None}
                for pid, v in self.book.items()]


def _adapter(client):
    """A CTraderExecAdapter without __init__ — it would open a live socket."""
    ad = object.__new__(CTraderExecAdapter)
    ad.instrument = 'DE30_EUR'
    ad.symbol_id = 10
    ad.min_volume = 1.0
    ad.step_volume = 1.0
    ad.digits = 2
    ad.client = client
    return ad


@pytest.fixture(autouse=True)
def _no_confirm_sleep(monkeypatch):
    """The confirm poll is network latency in production and dead time here."""
    monkeypatch.setattr(ctrader_exec.time, 'sleep', lambda *_: None)


@pytest.fixture
def adapter():
    return _adapter(FakeClient())


def test_place_stop_returns_the_position_id_as_ref(adapter):
    out = adapter.place_stop('4387582', 0.02, 1, 24911.24)
    assert out == {'ord_status': '0', 'ref': '4387582'}


def test_cancel_stop_accepts_what_place_stop_returns(adapter):
    """THE regression. Feed cancel_stop the exact dict place_stop produced."""
    ref = adapter.place_stop('4387582', 0.02, 1, 24911.24)
    adapter.client.sent.clear()

    out = adapter.cancel_stop(ref, orig_side=1)

    assert out is not None, 'cancel_stop must not fail on its own place_stop output'
    assert out['ord_status'] == '0'
    # An amend clearing the SL, aimed at the position — not an order cancel.
    assert len(adapter.client.sent) == 1
    req = adapter.client.sent[0]
    assert req.positionId == 4387582
    assert not req.HasField('stopLoss'), 'clearing the stop means leaving stopLoss unset'


def test_cancel_stop_still_cancels_a_legacy_fix_order(adapter):
    """A dict carrying order_id is a standalone resting stop and must be CANCELLED.

    Amending the position instead would leave the resting order alive, and a stop
    that outlives its position fires as a naked entry.
    """
    out = adapter.cancel_stop({'order_id': '998877', 'ref': '4387582'}, orig_side=1)

    assert out == {'ord_status': '0', 'ref': '998877'}
    req = adapter.client.sent[0]
    assert isinstance(req, ctrader_exec.ProtoOACancelOrderReq)
    assert req.orderId == 998877


def test_cancel_stop_accepts_a_bare_position_id(adapter):
    out = adapter.cancel_stop('4387582', orig_side=1)
    assert out == {'ord_status': '0', 'ref': '4387582'}
    assert adapter.client.sent[0].positionId == 4387582


@pytest.mark.parametrize('ref', [
    {},                                  # dict with neither key
    {'ord_status': '0'},                 # a place_stop REJECT carries no ref
    {'ord_status': '8', 'reject': 'x'},
    'not-a-number',
])
def test_cancel_stop_returns_none_rather_than_raising(adapter, ref):
    """The runner refuses to close on None; it must never see an exception."""
    assert adapter.cancel_stop(ref, orig_side=1) is None
    assert adapter.client.sent == []


# ── place_stop must CONFIRM, not assume ──────────────────────────────────────
#
# 2026-08-25, live on the prop book: nas100usd_..._164540_i1 reopened after
# roll-flat with a carried stop of 28954.46 onto a long FILLED at 28953.35 — the
# wrong side for a BUY. The broker refused the amend, but the refusal arrives as
# an order-error event rather than PROTO_OA_ERROR_RES, so client.send returned
# normally and place_stop reported ord_status '0'. _stop_ok passed it, the runner
# logged "stop@broker OK" and wrote a stop_ref. The position ran unstopped while
# every artefact the pod produces said it was protected.


def test_place_stop_rejects_when_the_broker_does_not_hold_the_stop():
    """THE regression. Transport says fine, the book says no stop."""
    ad = _adapter(FakeClient(applies=False))

    out = ad.place_stop('4387582', 0.02, 1, 24911.24)

    assert out['ord_status'] == '8', 'an unapplied amend must never read as attached'
    assert 'broker holds' in out['reject']


def test_place_stop_rejects_when_the_broker_holds_a_different_stop():
    ad = _adapter(FakeClient(book={'4387582': {'sl': 24000.0, 'entry_price': 24950.0}},
                             applies=False))

    out = ad.place_stop('4387582', 0.02, 1, 24911.24)

    assert out['ord_status'] == '8'
    assert '24000' in out['reject']


def test_place_stop_confirms_within_one_tick_of_precision():
    """The broker stores at the symbol's own precision; an exact compare would
    fail on the rounding alone, so a tick of tolerance is deliberate."""
    ad = _adapter(FakeClient(book={'4387582': {'sl': None, 'entry_price': 24950.0}}))
    ad.digits = 2

    out = ad.place_stop('4387582', 0.02, 1, 24911.238)

    assert out == {'ord_status': '0', 'ref': '4387582'}
    assert ad.client.book['4387582']['sl'] == 24911.24


def test_place_stop_reports_a_read_failure_as_a_reject():
    """A stop you cannot read back is not a stop."""
    class Blind(FakeClient):
        def get_positions(self):
            raise RuntimeError('reconcile timed out')

    out = _adapter(Blind()).place_stop('4387582', 0.02, 1, 24911.24)

    assert out['ord_status'] == '8'
    assert 'unconfirmable' in out['reject']


def test_position_entry_returns_the_brokers_fill_price():
    """The pre-trade reference is not the fill, and on a fast move the two
    straddle the carried stop — which is exactly how i1 ended up bare."""
    ad = _adapter(FakeClient(book={'4685037': {'sl': None, 'entry_price': 28953.35}}))

    assert ad.position_entry('4685037') == 28953.35
    assert ad.position_entry('nope') is None
