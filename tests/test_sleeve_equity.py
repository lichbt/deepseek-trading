"""Per-sleeve bar recording — the thing live_status.equity_curve never did.

equity_curve stores self.account_equity, the WHOLE OANDA account balance, written
identically into every sleeve — so the hourly report showing one figure for
EUR_JPY, WHEAT_USD and BTC_USD alike is one balance copied N times, not N sleeve
P&Ls. And it is initialised to [] on startup and never read back, so every restart
replaces the stored history with a short in-memory buffer.

sleeve_equity fixes both: per-sleeve numbers, and append-only so a restart cannot
truncate. These tests run against a TEMPORARY db — the real table is sealed against
DELETE, so test rows written there would be permanent.
"""
import sqlite3

import pytest

import live_test as lt
import pipeline_utils as pu


@pytest.fixture
def db(tmp_path, monkeypatch):
    path = tmp_path / "t.db"
    conn = sqlite3.connect(path)
    conn.executescript(pu.LIFECYCLE_SCHEMA_SQL)
    conn.commit(); conn.close()
    monkeypatch.setattr(lt, "_NETTING_DB", str(path))
    return path


def _rows(db, sid="S1"):
    c = sqlite3.connect(db)
    r = c.execute(
        "SELECT position, bar_return, position_return, own_units, price, sleeve_pnl "
        "FROM sleeve_equity WHERE sleeve_id=? ORDER BY id", (sid,)).fetchall()
    c.close()
    return r


def test_records_one_bar(db):
    lt._record_sleeve_bar("S1", "2026-07-28T21:00:00", 1, 0.005, 0.005, 1.1450, 1000.0, 1.0)
    (pos, br, pr, units, px, pnl), = _rows(db)
    assert pos == 1 and br == pytest.approx(0.005) and pr == pytest.approx(0.005)
    # currency P&L = units * bar_return * price * quote->USD
    assert pnl == pytest.approx(1000.0 * 0.005 * 1.1450)


def test_short_sleeve_gains_on_a_falling_bar(db):
    """position_return must carry the sleeve's DIRECTION, not the instrument's."""
    lt._record_sleeve_bar("S1", "2026-07-28T21:00:00", -1, -0.004, 0.004, 27838.5, -2.0, 1.0)
    (pos, br, pr, units, px, pnl), = _rows(db)
    assert br < 0, "the instrument fell"
    assert pr > 0, "a short sleeve GAINS on a falling bar"
    assert pnl > 0, "and so does its currency P&L"


def test_replay_after_restart_is_a_noop(db):
    """A restart re-reading the same bar must not duplicate or overwrite it.

    This is the property equity_curve lacked: it was replaced wholesale on every
    restart, silently truncating the record book-wide.
    """
    for _ in range(3):
        lt._record_sleeve_bar("S1", "2026-07-28T21:00:00", 1, 0.005, 0.005, 1.1450, 1000.0, 1.0)
    assert len(_rows(db)) == 1


def test_history_cannot_be_rewritten(db):
    """Append-only is enforced by the DB, not by the writer's good manners."""
    lt._record_sleeve_bar("S1", "2026-07-28T21:00:00", 1, 0.005, 0.005, 1.1450, 1000.0, 1.0)
    conn = sqlite3.connect(db)
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("UPDATE sleeve_equity SET position_return = 99")
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("DELETE FROM sleeve_equity")
    conn.close()


def test_a_db_failure_never_stops_the_sleeve_trading(db, monkeypatch, capsys):
    """This runs inside the trading hot loop. It must warn, never raise."""
    monkeypatch.setattr(lt, "_NETTING_DB", "/nonexistent/dir/nope.db")
    lt._record_sleeve_bar("S1", "t", 0, 0.0, 0.0, 1.0, 0.0, 1.0)   # must not raise
    assert "sleeve_equity" in capsys.readouterr().out


def test_flat_sleeve_records_a_zero_bar(db):
    """A flat sleeve still gets a row — absence of a row must mean NOT OBSERVED,
    never 'was flat'. Otherwise gaps are unreadable after the fact."""
    lt._record_sleeve_bar("S1", "2026-07-28T21:00:00", 0, 0.005, 0.0, 1.1450, 0.0, 1.0)
    (pos, br, pr, units, px, pnl), = _rows(db)
    assert pos == 0 and pr == 0.0 and pnl == 0.0
    assert br == pytest.approx(0.005), "the instrument still moved"


def test_scale_free_return_is_independent_of_sizing(db):
    """The whole reason position_return is stored alongside sleeve_pnl.

    Two sleeves, same signal and same bar, different sizes: currency P&L differs,
    the scale-free return does not. Judging 'is it working as designed' on P&L
    would penalise a sleeve merely for taking a correlation haircut.
    """
    lt._record_sleeve_bar("BIG", "2026-07-28T21:00:00", 1, 0.01, 0.01, 100.0, 5000.0, 1.0)
    lt._record_sleeve_bar("SMALL", "2026-07-28T21:00:00", 1, 0.01, 0.01, 100.0, 50.0, 1.0)
    (_, _, big_pr, _, _, big_pnl), = _rows(db, "BIG")
    (_, _, small_pr, _, _, small_pnl), = _rows(db, "SMALL")
    assert big_pr == small_pr, "scale-free return must ignore size"
    assert big_pnl != small_pnl, "currency P&L must not"
