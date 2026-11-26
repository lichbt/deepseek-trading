"""Decay flips get recorded to the sleeve DB and alerted once.

Decay detection was always automatic; nothing ever reported it. Since live_test
began re-reading portfolio_state.json every bar, a DECAYED verdict resizes a
sleeve to 0.25x with no restart and no human — so the only trace was one line in
one of 25 per-sleeve log files.

The tests that matter here are the QUIET ones. A risk channel that fires on
non-events trains you to ignore it, which is how the orphan sweep's permanent
false alarm became worse than no alarm at all.
"""
import os
import sqlite3
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 'scripts'))

import reason_codes
import decay_watch as DW


LIVE = {'a', 'b', 'c'}


def _v(**kw):
    """verdicts dict: sid -> (status, note)"""
    return {sid: (st, f'{sid} note') for sid, st in kw.items()}


# --- the flips that should fire -------------------------------------------

def test_ok_to_decayed_is_recorded():
    flips = DW.compute_flips(_v(a='DECAYED'), {'a': 'OK'}, LIVE)
    assert len(flips) == 1
    sid, code, prose = flips[0]
    assert (sid, code) == ('a', reason_codes.DECAY_DETECTED)
    assert '0.25x' in prose, 'the note must say what already happened to the size'
    assert 'HUMAN decision' in prose, 'and that retiring is not automatic'


def test_decayed_to_ok_is_recorded_as_recovery():
    flips = DW.compute_flips(_v(a='OK'), {'a': 'DECAYED'}, LIVE)
    assert [(s, c) for s, c, _ in flips] == [('a', reason_codes.DECAY_CLEARED)]


def test_first_ever_observation_of_decay_fires():
    """No prior event must not mean no alert — that is the one that matters most."""
    flips = DW.compute_flips(_v(a='DECAYED'), {}, LIVE)
    assert flips and flips[0][1] == reason_codes.DECAY_DETECTED
    assert 'first observation' in flips[0][2]


# --- the silence that keeps it believable ---------------------------------

def test_unchanged_verdict_is_silent():
    assert DW.compute_flips(_v(a='DECAYED'), {'a': 'DECAYED'}, LIVE) == []
    assert DW.compute_flips(_v(a='OK'), {'a': 'OK'}, LIVE) == []


def test_first_observation_of_ok_does_not_announce_a_recovery():
    """Otherwise the first run congratulates you on all 12 healthy sleeves."""
    assert DW.compute_flips(_v(a='OK', b='OK', c='OK'), {}, LIVE) == []


def test_insufficient_never_flips():
    """INSUFFICIENT is the measure declining to score — 13 of 25 sleeves today.
    Alerting on it would fire whenever a sleeve crosses the entry threshold."""
    assert DW.compute_flips(_v(a='INSUFFICIENT'), {}, LIVE) == []
    assert DW.compute_flips(_v(a='INSUFFICIENT'), {'a': 'OK'}, LIVE) == []
    assert DW.compute_flips(_v(a='INSUFFICIENT'), {'a': 'DECAYED'}, LIVE) == []


def test_a_retired_sleeve_does_not_alert_forever():
    """Retired sleeves keep a stale verdict in portfolio_state.json."""
    assert DW.compute_flips(_v(gone='DECAYED'), {}, LIVE) == []


# --- the DB note ----------------------------------------------------------

@pytest.fixture
def db(tmp_path):
    conn = sqlite3.connect(tmp_path / 'p.db')
    conn.executescript("""
        CREATE TABLE strategy_events (
            id INTEGER PRIMARY KEY, strategy_id TEXT NOT NULL, occurred_at TEXT NOT NULL,
            old_status TEXT, new_status TEXT, reason_code TEXT NOT NULL,
            reason_prose TEXT, source TEXT NOT NULL DEFAULT 'live', history_id INTEGER);
    """)
    return conn


def test_note_lands_on_the_sleeve_with_prose_and_code(db):
    DW.record(db, 'a', reason_codes.DECAY_DETECTED, 'recent GT 0.21 vs floor 0.35')
    row = db.execute("""SELECT strategy_id, reason_code, reason_prose, source,
                               old_status, new_status FROM strategy_events""").fetchone()
    assert row[:4] == ('a', reason_codes.DECAY_DETECTED, 'recent GT 0.21 vs floor 0.35', 'live')
    # A decay flip is NOT a status change — the sleeve stays paper_trading.
    assert row[4] is None and row[5] is None


def test_previous_state_is_read_back_from_the_events_table(db):
    """strategy_events IS the prior state — no side-car file that can drift."""
    DW.record(db, 'a', reason_codes.DECAY_DETECTED, 'x')
    DW.record(db, 'b', reason_codes.DECAY_CLEARED, 'y')
    assert DW.last_recorded(db) == {'a': 'DECAYED', 'b': 'OK'}


def test_the_newest_event_wins_after_a_round_trip(db):
    """A sleeve that decayed and recovered must read OK, not DECAYED."""
    db.execute("""INSERT INTO strategy_events (strategy_id, occurred_at, reason_code, source)
                  VALUES ('a', '2026-07-01T00:00:00+00:00', ?, 'live')""",
               (reason_codes.DECAY_DETECTED,))
    db.execute("""INSERT INTO strategy_events (strategy_id, occurred_at, reason_code, source)
                  VALUES ('a', '2026-07-20T00:00:00+00:00', ?, 'live')""",
               (reason_codes.DECAY_CLEARED,))
    db.commit()
    assert DW.last_recorded(db) == {'a': 'OK'}


def test_unrelated_events_are_not_mistaken_for_decay_state(db):
    """The table holds ~149k rows of status history; only decay codes count."""
    db.execute("""INSERT INTO strategy_events (strategy_id, occurred_at, reason_code, source)
                  VALUES ('a', '2026-07-30T00:00:00+00:00', 'DEPLOYED', 'live')""")
    db.commit()
    assert DW.last_recorded(db) == {}
