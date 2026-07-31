"""Notes on an evaluation row, and the Telegram skip button's audit trail.

Two gaps this pins:
  * `evaluations.verdict` is machine-generated and records what the gates
    MEASURED, never why a human accepted or rejected the candidate.
  * telegram_bot's skip button used a bare UPDATE, so a rejection landed with no
    status_history row and no reason at all — status changed, cause lost.

Runs against a TEMP db: the real evaluations table is sealed against DELETE.
"""
import sqlite3

import pytest

import evaluate_strategy as E


SCHEMA = """
CREATE TABLE evaluations (
    id INTEGER PRIMARY KEY, strategy_id TEXT NOT NULL, run_at TEXT NOT NULL,
    window_start TEXT NOT NULL, window_end TEXT NOT NULL,
    recent_gt REAL, gt_floor REAL, decay_status TEXT, near_miss INTEGER,
    entries_in_window INTEGER, entries_lifetime INTEGER, capped_by TEXT,
    r12 REAL, sharpe REAL, maxdd REAL, inmkt REAL, tot_return REAL,
    verdict TEXT, source TEXT NOT NULL DEFAULT 'live', notes TEXT
);
CREATE TRIGGER evaluations_no_update BEFORE UPDATE ON evaluations
BEGIN SELECT RAISE(ABORT, 'evaluations is append-only'); END;
"""

M = dict(r12=0.1, sharpe=0.8, maxdd=-0.05, inmkt=0.2, tot=1.5)
D = dict(recent_gt=0.5, threshold=0.35, status='OK', near_miss=False,
         in_window=30, entries=100, capped_by='entries')


@pytest.fixture
def db(tmp_path):
    conn = sqlite3.connect(tmp_path / 't.db')
    conn.executescript(SCHEMA)
    return conn


def _note(conn):
    return conn.execute('SELECT notes FROM evaluations').fetchone()[0]


def test_note_is_stored_when_supplied(db):
    E.record_evaluation(db, 'sid', M, D, 'LOOKAHEAD=PASS', '2015-01-01', '2026-07-30',
                        notes='REJECT: corr +0.99 with incumbent')
    assert _note(db) == 'REJECT: corr +0.99 with incumbent'


def test_note_is_optional_and_defaults_to_null(db):
    """Existing callers pass no note; the row must still write."""
    E.record_evaluation(db, 'sid', M, D, 'LOOKAHEAD=PASS', '2015-01-01', '2026-07-30')
    assert _note(db) is None


def test_note_does_not_displace_the_machine_verdict(db):
    """verdict is what the gates measured; notes is the human conclusion. Both survive."""
    E.record_evaluation(db, 'sid', M, D, 'LOOKAHEAD=FAIL DECAY=OK', '2015-01-01', '2026-07-30',
                        notes='HARD REJECT: flip 28%')
    row = db.execute('SELECT verdict, notes FROM evaluations').fetchone()
    assert row == ('LOOKAHEAD=FAIL DECAY=OK', 'HARD REJECT: flip 28%')


def test_annotating_again_appends_rather_than_edits(db):
    """The table is sealed, so a later opinion is a NEW observation, not an edit."""
    E.record_evaluation(db, 'sid', M, D, 'v', '2015-01-01', '2026-07-30', notes='first look')
    E.record_evaluation(db, 'sid', M, D, 'v', '2015-01-01', '2026-07-31', notes='second look')
    notes = [r[0] for r in db.execute('SELECT notes FROM evaluations ORDER BY id')]
    assert notes == ['first look', 'second look']
    with pytest.raises(sqlite3.IntegrityError):
        db.execute("UPDATE evaluations SET notes='edited' WHERE id=1")


def test_a_failed_note_write_does_not_raise_into_the_caller(db):
    """record_evaluation warns and returns; evaluation must never die on a DB problem."""
    db.execute('DROP TABLE evaluations')
    E.record_evaluation(db, 'sid', M, D, 'v', '2015-01-01', '2026-07-30', notes='x')


# --- telegram skip button --------------------------------------------------

def test_skip_button_records_a_reason(monkeypatch, tmp_path):
    """The bare-UPDATE version left NO status_history row. This fails against it."""
    import telegram_bot

    logged = {}
    monkeypatch.setattr(telegram_bot.pu, '_log_status_change',
                        lambda sid, old, new, reason=None: logged.update(
                            sid=sid, old=old, new=new, reason=reason))

    path = tmp_path / 'p.db'
    conn = sqlite3.connect(path)
    conn.execute('CREATE TABLE strategies (id TEXT PRIMARY KEY, status TEXT)')
    conn.execute("INSERT INTO strategies VALUES ('s1', 'passed')")
    conn.commit(); conn.close()

    import contextlib

    @contextlib.contextmanager
    def fake_conn():
        c = sqlite3.connect(path)
        try:
            yield c
            c.commit()
        finally:
            c.close()

    monkeypatch.setattr(telegram_bot.pu, 'get_db_connection', fake_conn)
    monkeypatch.setattr(telegram_bot.requests, 'post', lambda *a, **k: None)

    telegram_bot._handle_callback_query(
        {'id': '1', 'data': 'skip:s1', 'message': {'chat': {'id': 1}, 'message_id': 2}})

    assert logged.get('new') == 'skipped', 'skip must go through _log_status_change'
    assert logged.get('old') == 'passed'
    assert logged.get('reason'), 'a skip must never land without a reason'


def test_skip_of_a_non_candidate_logs_nothing(monkeypatch, tmp_path):
    """The UPDATE is guarded to passed/fragile; a no-op must not fabricate an event."""
    import telegram_bot
    import contextlib

    logged = {}
    monkeypatch.setattr(telegram_bot.pu, '_log_status_change',
                        lambda *a, **k: logged.update(called=True))

    path = tmp_path / 'p.db'
    conn = sqlite3.connect(path)
    conn.execute('CREATE TABLE strategies (id TEXT PRIMARY KEY, status TEXT)')
    conn.execute("INSERT INTO strategies VALUES ('s1', 'paper_trading')")
    conn.commit(); conn.close()

    @contextlib.contextmanager
    def fake_conn():
        c = sqlite3.connect(path)
        try:
            yield c
            c.commit()
        finally:
            c.close()

    monkeypatch.setattr(telegram_bot.pu, 'get_db_connection', fake_conn)
    monkeypatch.setattr(telegram_bot.requests, 'post', lambda *a, **k: None)

    telegram_bot._handle_callback_query(
        {'id': '1', 'data': 'skip:s1', 'message': {'chat': {'id': 1}, 'message_id': 2}})

    assert not logged, 'a live sleeve must not be skippable, nor logged as skipped'
    with sqlite3.connect(path) as c:
        assert c.execute('SELECT status FROM strategies').fetchone()[0] == 'paper_trading'
