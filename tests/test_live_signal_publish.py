"""current_signal must never silently read FLAT while a sleeve holds a position.

_get_corr_scale halves a sleeve's size when a correlated peer is positioned the
SAME way. If a peer's current_signal is stale at the 0 default, the conflict is
MISSED and the sleeve sizes at FULL instead of half — the failure is
risk-INCREASING, which is why this is worth a test rather than a comment.

Two independent causes were found on 2026-07-29, and both are covered here:

  1. update_live_signal was a bare UPDATE, a silent no-op when the row is
     missing. The row is only created by start_live_trading, so a sleeve
     activated by any other path published its signal into the void.
  2. live_test published only on a flip/align, so a sleeve holding a position
     without flipping kept whatever the row was created with — and rows created
     by update_live_metrics' UPSERT start at the 0 default. Measured: eurusd_i9
     and xcuusd_i27 both read current_signal=0 while holding real -1 positions.
"""
import sqlite3

import pytest

import pipeline_utils as pu


@pytest.fixture
def db(tmp_path, monkeypatch):
    path = tmp_path / "t.db"
    monkeypatch.setattr(pu, "DB_PATH", path)
    pu.init_db()
    return path


def _signal(db, sid="S1"):
    c = sqlite3.connect(db)
    r = c.execute("SELECT current_signal FROM live_status WHERE strategy_id=?", (sid,)).fetchone()
    c.close()
    return None if r is None else r[0]


def test_publishing_creates_the_row_when_it_is_missing(db):
    """The bug: a bare UPDATE against a missing row succeeds and writes nothing."""
    assert _signal(db) is None, "precondition: no live_status row"
    pu.update_live_signal("S1", -1)
    assert _signal(db) == -1, "a missing row must be created, not silently skipped"


def test_publishing_updates_an_existing_row(db):
    pu.update_live_signal("S1", -1)
    pu.update_live_signal("S1", 1)
    assert _signal(db) == 1


def test_a_short_sleeve_never_reads_flat(db):
    """The exact production symptom: holding -1 while the column says 0.

    A peer reading 0 concludes 'no conflict' and skips the correlation haircut.
    """
    pu.update_live_signal("S1", -1)
    assert _signal(db) != 0
    assert pu.get_live_signals(["S1"]) == {"S1": -1}


def test_missing_peer_reads_flat_not_an_error(db):
    """An unknown peer must degrade to 0, not raise — _get_corr_scale runs in the
    sizing path and must never break an order."""
    assert pu.get_live_signals(["nope"]) == {"nope": 0}


def test_metrics_upsert_does_not_clobber_a_published_signal(db):
    """update_live_metrics UPSERTs the row. It must not reset current_signal.

    This is the interaction that created the stale rows: the row was inserted by
    the metrics path (defaulting current_signal to 0) for a sleeve that had
    already been positioned.
    """
    pu.update_live_signal("S1", -1)
    pu.update_live_metrics("S1", [{"date": "2026-07-29", "equity": 100.0}], 0.5)
    assert _signal(db) == -1, "a metrics write must not reset the published signal"
