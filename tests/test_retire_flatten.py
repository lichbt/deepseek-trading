"""retire_strategy must never strand broker exposure.

A retired sleeve's live_test process is stopped, so units it still owns under
NETTING become unmanaged and unstopped. These tests pin the three behaviours
that prevent that: flatten before retiring, refuse to retire when the close is
rejected, and record the residual when forced.
"""
import sqlite3
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))
import pipeline_utils as pu


@pytest.fixture
def db(tmp_path, monkeypatch):
    path = tmp_path / 'pipeline.db'
    con = sqlite3.connect(str(path))
    con.executescript("""
        CREATE TABLE strategies (id TEXT PRIMARY KEY, status TEXT);
        CREATE TABLE status_history (strategy_id TEXT, old_status TEXT,
            new_status TEXT, reason TEXT, changed_at TEXT);
        CREATE TABLE live_status (strategy_id TEXT PRIMARY KEY,
            current_position INTEGER DEFAULT 0, entry_price REAL DEFAULT 0.0);
        CREATE TABLE sleeve_units (sleeve_id TEXT PRIMARY KEY, units REAL, stop REAL);
        INSERT INTO strategies VALUES ('usdchf_auto_1_i1', 'paper_trading');
        INSERT INTO live_status VALUES ('usdchf_auto_1_i1', 1, 0.805);
        INSERT INTO sleeve_units VALUES ('usdchf_auto_1_i1', 23388.07, 0.795);
    """)
    con.commit()
    con.close()
    monkeypatch.setattr(pu, 'DB_PATH', str(path), raising=False)
    monkeypatch.setenv('OANDA_ACCOUNT_ID', 'test-acct')
    monkeypatch.setenv('OANDA_API_TOKEN', 'test-token')
    return path


def _status(db_path, sid='usdchf_auto_1_i1'):
    con = sqlite3.connect(str(db_path))
    try:
        return con.execute('SELECT status FROM strategies WHERE id=?', (sid,)).fetchone()[0]
    finally:
        con.close()


def _units(db_path, sid='usdchf_auto_1_i1'):
    con = sqlite3.connect(str(db_path))
    try:
        row = con.execute('SELECT units FROM sleeve_units WHERE sleeve_id=?', (sid,)).fetchone()
        return None if row is None else row[0]
    finally:
        con.close()


def _resp(payload):
    r = MagicMock()
    r.json.return_value = payload
    r.raise_for_status.return_value = None
    return r


FILLED = {'orderFillTransaction': {'units': '-23388', 'price': '0.8180', 'pl': '365.36'}}
HALTED = {'orderCancelTransaction': {'reason': 'MARKET_HALTED'}}
# Terminal, not transient: REDUCE_ONLY against a net the orphan's share has
# already been absorbed into. Retrying can never succeed.
NO_POS = {'orderCancelTransaction': {'reason': 'NO_POSITION_TO_REDUCE'}}


def test_flattens_then_retires(db):
    with patch('requests.post', return_value=_resp(FILLED)) as post:
        pu.retire_strategy('usdchf_auto_1_i1', 'decayed')
    order = post.call_args.kwargs['json']['order']
    assert order['units'] == '-23388'          # opposite sign, instrument precision
    assert order['positionFill'] == 'REDUCE_ONLY'
    assert _status(db) == 'retired'
    assert _units(db) is None                  # book row cleared


def test_rejected_close_aborts_retirement(db):
    with patch('requests.post', return_value=_resp(HALTED)):
        with pytest.raises(RuntimeError, match='MARKET_HALTED'):
            pu.retire_strategy('usdchf_auto_1_i1', 'decayed')
    # The sleeve must stay live and keep owning its units — a running sleeve is
    # strictly safer than a stranded one.
    assert _status(db) == 'paper_trading'
    assert _units(db) == pytest.approx(23388.07)


def test_force_retires_but_records_the_residual(db):
    with patch('requests.post', return_value=_resp(HALTED)):
        pu.retire_strategy('usdchf_auto_1_i1', 'decayed', force=True)
    assert _status(db) == 'retired'
    con = sqlite3.connect(str(db))
    reason = con.execute('SELECT reason FROM status_history WHERE strategy_id=?',
                         ('usdchf_auto_1_i1',)).fetchone()[0]
    con.close()
    assert 'stranded' in reason and 'MARKET_HALTED' in reason


def test_no_position_to_reduce_clears_the_row_without_force(db):
    """The broker says there is nothing on the reducible side — that is proof the
    share was netted away, not a failure. Retiring must succeed and clear the row.

    Negative control: against the pre-fix code this raised RuntimeError, so the
    sleeve stayed 'paper_trading' and the row survived — the exact state that made
    flatten_orphans report STILL EXPOSED forever.
    """
    with patch('requests.post', return_value=_resp(NO_POS)) as post:
        pu.retire_strategy('usdchf_auto_1_i1', 'decayed')
    # REDUCE_ONLY is what makes this safe to treat as benign — it cannot open.
    assert post.call_args.kwargs['json']['order']['positionFill'] == 'REDUCE_ONLY'
    assert _status(db) == 'retired'
    assert _units(db) is None
    # and it must NOT be recorded as a stranded residual, because nothing is stranded
    con = sqlite3.connect(str(db))
    reason = con.execute('SELECT reason FROM status_history WHERE strategy_id=?',
                         ('usdchf_auto_1_i1',)).fetchone()[0]
    con.close()
    assert 'stranded' not in reason


def test_no_position_to_reduce_reports_as_cleared_not_closed(db):
    """flatten_sleeve must flag the skip so callers never announce a phantom exit."""
    with patch('requests.post', return_value=_resp(NO_POS)):
        res = pu.flatten_sleeve('usdchf_auto_1_i1')
    assert res['skipped'] == 'NO_POSITION_TO_REDUCE'
    assert res['units'] == 0.0                       # nothing was closed
    assert res['requested'] == pytest.approx(23388.07)   # what the books claimed
    assert res['price'] is None and res['pl'] is None


def test_halted_still_raises_and_is_not_confused_with_no_position(db):
    """The transient class must keep its old behaviour — row intact, sleeve live."""
    with patch('requests.post', return_value=_resp(HALTED)):
        with pytest.raises(RuntimeError, match='MARKET_HALTED'):
            pu.flatten_sleeve('usdchf_auto_1_i1')
    assert _units(db) == pytest.approx(23388.07)
    assert _status(db) == 'paper_trading'


def test_flat_sleeve_needs_no_broker_call(db):
    con = sqlite3.connect(str(db))
    con.execute('UPDATE sleeve_units SET units=0 WHERE sleeve_id=?', ('usdchf_auto_1_i1',))
    con.commit()
    con.close()
    with patch('requests.post') as post:
        pu.retire_strategy('usdchf_auto_1_i1', 'decayed')
    post.assert_not_called()
    assert _status(db) == 'retired'
    assert _units(db) is None
