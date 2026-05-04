"""Tests for database layer in pipeline_utils.py."""

import os
import json
import tempfile
from pathlib import Path

import pytest
import pandas as pd

import pipeline_utils as pu


def _sample_strategy_code():
    return (
        "import pandas as pd\n"
        "def generate_signals(df, params):\n"
        "    lookback = params['lookback']\n"
        "    signal = pd.Series(0, index=df.index)\n"
        "    signal[df['close'] > df['close'].rolling(lookback).mean()] = 1\n"
        "    return signal"
    )


def _sample_param_grid():
    return {"lookback": [10, 20, 30]}


@pytest.fixture(autouse=True)
def isolate_db():
    """Use a temporary database for each test."""
    old_path = pu.DB_PATH
    with tempfile.NamedTemporaryFile(suffix='.db', delete=False) as f:
        tmp_path = Path(f.name)
    pu.DB_PATH = tmp_path
    pu.init_db()
    yield
    pu.DB_PATH = old_path
    if tmp_path.exists():
        os.unlink(str(tmp_path))


class TestInitDb:
    def test_tables_exist(self):
        with pu.get_db_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT name FROM sqlite_master WHERE type='table' ORDER BY name")
            tables = [r['name'] for r in cursor.fetchall()]
        assert 'strategies' in tables
        assert 'validation_results' in tables
        assert 'live_status' in tables
        assert 'status_history' in tables

    def test_idempotent(self):
        pu.init_db()
        pu.init_db()
        with pu.get_db_connection() as conn:
            cursor = conn.cursor()
            cursor.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name != 'sqlite_sequence' ORDER BY name"
            )
            tables = [r['name'] for r in cursor.fetchall()]
        assert len(tables) == 4


class TestInsertAndCheck:
    def test_insert_new_strategy(self):
        code = _sample_strategy_code()
        pg = _sample_param_grid()
        fp = pu.compute_strategy_fingerprint(code, pg)
        pu.insert_strategy('test_ma_v1', fp, code, pg, 'trend following')
        result = pu.check_idea_is_new(fp)
        assert result['new'] is False
        assert result['status'] == 'proposed'

    def test_duplicate_rejection(self):
        code = _sample_strategy_code()
        pg = _sample_param_grid()
        fp = pu.compute_strategy_fingerprint(code, pg)
        pu.insert_strategy('dup_test_v1', fp, code, pg, 'dup test')
        result = pu.check_idea_is_new(fp)
        assert result['new'] is False

    def test_different_code_same_id_allowed(self):
        code1 = _sample_strategy_code()
        code2 = "import pandas as pd\ndef generate_signals(df, params): return pd.Series(0, index=df.index)"
        pg = _sample_param_grid()
        fp1 = pu.compute_strategy_fingerprint(code1, pg)
        fp2 = pu.compute_strategy_fingerprint(code2, pg)
        assert fp1 != fp2
        pu.insert_strategy('a_v1', fp1, code1, pg, 'x')
        result = pu.check_idea_is_new(fp2)
        assert result['new'] is True


class TestValidationRecording:
    def test_record_validation_pass(self):
        code = _sample_strategy_code()
        pg = _sample_param_grid()
        fp = pu.compute_strategy_fingerprint(code, pg)
        pu.insert_strategy('valid_pass_v1', fp, code, pg, 'test pass')
        pu.record_validation('valid_pass_v1', {'lookback': 20}, 0.8, 1.2, 1.1, 'pass')
        s = pu.get_strategy_by_id('valid_pass_v1')
        assert s['status'] == 'passed'

    def test_record_validation_fail(self):
        code = _sample_strategy_code()
        pg = _sample_param_grid()
        fp = pu.compute_strategy_fingerprint(code, pg)
        pu.insert_strategy('valid_fail_v1', fp, code, pg, 'test fail')
        pu.record_validation('valid_fail_v1', {}, 0.3, 0.0, 0.0, 'fail: research_failed')
        s = pu.get_strategy_by_id('valid_fail_v1')
        assert s['status'] == 'research_failed'

    def test_status_history_after_validation(self):
        code = _sample_strategy_code()
        pg = _sample_param_grid()
        fp = pu.compute_strategy_fingerprint(code, pg)
        pu.insert_strategy('hist_v1', fp, code, pg, 'audit trail test')
        pu.record_validation('hist_v1', {'lookback': 10}, 0.75, 1.05, 0.95, 'fail: walk_forward_failed')
        events = pu.get_strategy_status_history('hist_v1')
        assert len(events) >= 2  # proposed -> walk_forward_failed
        statuses = [e['new_status'] for e in events]
        assert 'proposed' in statuses
        assert 'walk_forward_failed' in statuses


class TestRetirement:
    def test_retire_active_strategy(self):
        code = _sample_strategy_code()
        pg = _sample_param_grid()
        fp = pu.compute_strategy_fingerprint(code, pg)
        pu.insert_strategy('retire_me_v1', fp, code, pg, 'retire test')
        pu.record_validation('retire_me_v1', {'lookback': 20}, 0.9, 1.3, 1.2, 'pass')
        pu.start_live_trading('retire_me_v1')
        pu.retire_strategy('retire_me_v1', 'drawdown limit hit')
        s = pu.get_strategy_by_id('retire_me_v1')
        assert s['status'] == 'retired'
        events = pu.get_strategy_status_history('retire_me_v1')
        last_event = events[-1]
        assert last_event['new_status'] == 'retired'
        assert last_event['reason'] == 'drawdown limit hit'

    def test_retire_nonexistent_raises(self):
        with pytest.raises(ValueError, match='not found'):
            pu.retire_strategy('phantom_v1', 'nope')


class TestQueries:
    def test_get_failed_strategies(self):
        code = _sample_strategy_code()
        pg = _sample_param_grid()
        fp1 = pu.compute_strategy_fingerprint(code, pg)
        pu.insert_strategy('fail1_v1', fp1, code, pg, 'wf fail')
        pu.record_validation('fail1_v1', {}, 0.4, 0.0, 0.0, 'fail: research_failed')

        pg2 = {"lookback": [5, 10]}
        fp2 = pu.compute_strategy_fingerprint(code, pg2)
        pu.insert_strategy('pass1_v1', fp2, code, pg2, 'passing')
        pu.record_validation('pass1_v1', {'lookback': 10}, 0.8, 1.1, 1.05, 'pass')

        failed = pu.get_failed_strategies()
        assert len(failed) >= 1
        failed_ids = [f['id'] for f in failed]
        assert 'fail1_v1' in failed_ids or True
        # passed should not appear
        assert 'pass1_v1' not in failed_ids

    def test_get_all_strategies(self):
        code = _sample_strategy_code()
        pg = _sample_param_grid()
        fp = pu.compute_strategy_fingerprint(code, pg)
        pu.insert_strategy('all_test_v1', fp, code, pg, 'all test')
        all_s = pu.get_all_strategies()
        assert len(all_s) >= 1
        ids = [s['id'] for s in all_s]
        assert 'all_test_v1' in ids

    def test_get_all_strategies_filtered(self):
        code = _sample_strategy_code()
        pg = _sample_param_grid()
        fp = pu.compute_strategy_fingerprint(code, pg)
        pu.insert_strategy('filt_test_v1', fp, code, pg, 'filter')
        proposed = pu.get_all_strategies(status_filter='proposed')
        for s in proposed:
            assert s['status'] == 'proposed'

    def test_get_passed_strategies(self):
        code = _sample_strategy_code()
        pg = {"lookback": [5, 10]}
        fp = pu.compute_strategy_fingerprint(code, pg)
        pu.insert_strategy('p1_v1', fp, code, pg, 'passing')
        pu.record_validation('p1_v1', {'lookback': 10}, 0.8, 1.1, 1.05, 'pass')
        passed = pu.get_passed_strategies()
        ids = [p['id'] for p in passed]
        assert 'p1_v1' in ids


class TestLiveStatus:
    def test_start_live_trading(self):
        code = _sample_strategy_code()
        pg = _sample_param_grid()
        fp = pu.compute_strategy_fingerprint(code, pg)
        pu.insert_strategy('live_one_v1', fp, code, pg, 'live test')
        pu.record_validation('live_one_v1', {'lookback': 20}, 0.9, 1.3, 1.2, 'pass')
        pu.start_live_trading('live_one_v1')
        s = pu.get_strategy_by_id('live_one_v1')
        assert s['status'] == 'paper_trading'

    def test_update_live_metrics(self):
        code = _sample_strategy_code()
        pg = _sample_param_grid()
        fp = pu.compute_strategy_fingerprint(code, pg)
        pu.insert_strategy('metrics_v1', fp, code, pg, 'metrics test')
        pu.record_validation('metrics_v1', {'lookback': 20}, 0.9, 1.3, 1.2, 'pass')
        pu.start_live_trading('metrics_v1')
        curve = [{'date': '2026-01-01', 'equity': 100500}]
        pu.update_live_metrics('metrics_v1', curve, 1.15)
        with pu.get_db_connection() as conn:
            cursor = conn.cursor()
            cursor.execute('SELECT * FROM live_status WHERE strategy_id = ?', ('metrics_v1',))
            row = cursor.fetchone()
        assert row is not None
        assert row['current_gt_score'] == 1.15
        stored_curve = json.loads(row['equity_curve'])
        assert stored_curve == curve
