"""Validator-hash stamping: every verdict is traceable to the code that made it.

Without the stamp, `tested_at` is the only provenance on a validation row, so a
pass can outlive the gate constants that produced it with nothing to detect it —
exactly how the 2026-07-31 data_fetcher dedup fix invalidated ~31% of the failed
pool unnoticed.
"""
import os
import tempfile
from pathlib import Path

import pytest

import pipeline_utils as pu

_CODE = (
    "import pandas as pd\n"
    "def generate_signals(df, params):\n"
    "    s = pd.Series(0, index=df.index)\n"
    "    s[df['close'] > df['close'].rolling(10).mean()] = 1\n"
    "    return s"
)

@pytest.fixture(autouse=True)
def isolate_db():
    old = pu.DB_PATH
    with tempfile.NamedTemporaryFile(suffix='.db', delete=False) as f:
        tmp = Path(f.name)
    pu.DB_PATH = tmp
    pu.init_db()
    yield
    pu.DB_PATH = old
    tmp.unlink(missing_ok=True)

def _seed(sid, final_status='pass'):
    pu.insert_strategy(sid, pu.compute_strategy_fingerprint(_CODE, {'lookback': [10]}),
                       _CODE, {'lookback': [10]}, 'trend', 'D', 'EUR_USD')
    pu.record_validation(sid, {'lookback': 10}, 0.8, 1.2, 1.1, final_status)

def _stamp(sid):
    with pu.get_db_connection() as conn:
        return conn.execute('SELECT validator_hash FROM validation_results '
                            'WHERE strategy_id = ?', (sid,)).fetchone()['validator_hash']

def test_record_validation_stamps_current_hash():
    _seed('stamp_v1')
    assert _stamp('stamp_v1') == pu.validator_hash()

def test_fresh_row_is_not_stale():
    _seed('fresh_v1')
    assert pu.stale_validations() == []

def test_pre_stamp_row_is_stale():
    _seed('old_v1')
    with pu.get_db_connection() as conn:
        conn.execute('UPDATE validation_results SET validator_hash = NULL '
                     'WHERE strategy_id = ?', ('old_v1',))
    stale = pu.stale_validations()
    assert [r['id'] for r in stale] == ['old_v1']
    assert stale[0]['validator_hash'] is None

def test_dead_status_out_of_default_scope():
    # A stale research_failed row costs nothing; scope is what can still trade.
    _seed('dead_v1', final_status='fail: research_failed')
    with pu.get_db_connection() as conn:
        conn.execute('UPDATE validation_results SET validator_hash = NULL '
                     'WHERE strategy_id = ?', ('dead_v1',))
    assert pu.stale_validations() == []
    assert [r['id'] for r in pu.stale_validations(('research_failed',))] == ['dead_v1']

def test_hash_tracks_source_bytes(tmp_path, monkeypatch):
    (tmp_path / 'a.py').write_text('X = 1\n')
    monkeypatch.setattr(pu, '__file__', str(tmp_path / 'pipeline_utils.py'))
    monkeypatch.setattr(pu, 'VALIDATOR_HASH_FILES', ('a.py',))
    before = pu.validator_hash()
    (tmp_path / 'a.py').write_text('X = 2\n')
    assert pu.validator_hash() != before

def test_hash_is_filename_sensitive(tmp_path, monkeypatch):
    # Two modules with identical bytes must not collide: the deciding logic is
    # the file, not the text.
    (tmp_path / 'a.py').write_text('X = 1\n')
    (tmp_path / 'b.py').write_text('X = 1\n')
    monkeypatch.setattr(pu, '__file__', str(tmp_path / 'pipeline_utils.py'))
    monkeypatch.setattr(pu, 'VALIDATOR_HASH_FILES', ('a.py',))
    ha = pu.validator_hash()
    monkeypatch.setattr(pu, 'VALIDATOR_HASH_FILES', ('b.py',))
    assert pu.validator_hash() != ha

def test_scope_statuses_cover_deployable():
    assert set(pu.DEPLOYABLE) <= set(pu.STALE_SCOPE_STATUSES)
    assert 'live' in pu.STALE_SCOPE_STATUSES
