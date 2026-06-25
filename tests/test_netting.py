"""Tests for per-instrument netting (live_test).

The invariant: the broker position is the running sum of each sleeve's deltas,
so a sleeve only ever orders its OWN change and its exit removes only its OWN
share — never another same-instrument sleeve's.
"""
import live_test as L


def test_netting_delta_basic():
    assert L.netting_delta(0, 100) == 100      # open long
    assert L.netting_delta(100, 0) == -100     # close own share
    assert L.netting_delta(100, 80) == -20     # shrink
    assert L.netting_delta(-50, 50) == 100     # flip short -> long


def test_two_sleeves_accumulate_then_each_exits_independently():
    broker = 0.0
    a = b = 0.0
    d = L.netting_delta(a, 100); a += d; broker += d   # A opens +100
    d = L.netting_delta(b, 80);  b += d; broker += d   # B opens +80
    assert broker == 180                                # netted, both present
    d = L.netting_delta(a, 0);   a += d; broker += d    # A exits...
    assert broker == 80 and b == 80                     # ...B untouched
    d = L.netting_delta(b, 0);   b += d; broker += d    # B exits
    assert broker == 0


def test_own_units_persist_across_restart(tmp_path, monkeypatch):
    monkeypatch.setattr(L, "_NETTING_DB", str(tmp_path / "p.db"))
    L._save_own_units("eurusd_auto_1", 123.0, 1.2345)
    units, stop = L._load_own_units("eurusd_auto_1")
    assert units == 123.0 and abs(stop - 1.2345) < 1e-9
    assert L._load_own_units("never_seen") == (0.0, None)   # default, no crash


def test_netting_off_by_default():
    assert L.NETTING_ENABLED is False


def test_netting_restore_ignores_broker_net(monkeypatch):
    """A flat netted sleeve must NOT adopt a same-instrument peer's broker net on
    restart — the broker only knows the net, not this sleeve's share."""
    t = L.LiveTrader.__new__(L.LiveTrader)   # skip heavy __init__
    t.strategy_id, t.instrument = "nas100usd_auto_x_i3", "NAS100_USD"
    monkeypatch.setattr(L, "NETTING_ENABLED", True)
    monkeypatch.setattr(L, "load_live_state", lambda sid: {
        'current_position': 0, 'entry_price': 0.0, 'last_bar_time': None,
        'prev_signal': 0, 'oanda_trade_id': None})
    monkeypatch.setattr(L, "_load_own_units", lambda sid: (0.0, None))

    def boom(*a, **k):
        raise AssertionError("netting restore queried the broker")
    t._get_account_summary = boom   # broker scan must never run

    t._restore_and_reconcile()
    assert t.current_position == 0   # did NOT adopt the peer's net=1
    assert t.own_units == 0.0
