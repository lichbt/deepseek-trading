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
    """Records the protobuf requests instead of sending them."""

    account_id = 47916240

    def __init__(self):
        self.sent = []

    def send(self, req, timeout=None):
        self.sent.append(req)
        return req


@pytest.fixture
def adapter():
    """A CTraderExecAdapter without __init__ — it would open a live socket."""
    ad = object.__new__(CTraderExecAdapter)
    ad.instrument = 'DE30_EUR'
    ad.symbol_id = 10
    ad.min_volume = 1.0
    ad.step_volume = 1.0
    ad.digits = 2
    ad.client = FakeClient()
    return ad


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
