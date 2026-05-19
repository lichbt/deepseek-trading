"""
Tests for meta_review.py — guards against the SQL schema mismatch that
silently broke meta-review for an extended period.
"""
import sys
import sqlite3
import tempfile
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

import meta_review as mr
import pipeline_utils as pu


@pytest.fixture
def temp_db(monkeypatch):
    """Create a temp DB with the live schema and a handful of fake rows."""
    with tempfile.NamedTemporaryFile(suffix='.db', delete=False) as f:
        tmp = Path(f.name)

    # Initialise both modules' pointers to the temp DB
    monkeypatch.setattr(pu, 'DB_PATH', tmp)
    monkeypatch.setattr(mr, 'DB_PATH', tmp)
    pu.init_db()

    # Seed strategies + validation_results so get_recent_results returns something
    with pu.get_db_connection() as conn:
        c = conn.cursor()
        c.execute(
            "INSERT INTO strategies (id, fingerprint, code, param_grid, rationale, timeframe, status, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            ('gbpusd_auto_20260519_120000_i1', 'fp_a', 'code1', '{}', 'gbp test', 'D', 'research_failed', '2026-05-19T12:00:00'),
        )
        c.execute(
            "INSERT INTO strategies (id, fingerprint, code, param_grid, rationale, timeframe, status, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            ('xau_usd_volatility_v1', 'fp_b', 'code2', '{}', 'gold test', 'D', 'passed', '2026-05-19T12:01:00'),
        )
        c.execute(
            "INSERT INTO validation_results "
            "(strategy_id, best_params, is_gt_score, walk_forward_gt_score, holdout_gt_score, final_status, tested_at, torture_flags) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            ('gbpusd_auto_20260519_120000_i1', '{}', 0.05, 0.0, 0.0, 'FAIL: IS 0.05 < 0.3', '2026-05-19T12:00:00', '[]'),
        )
        c.execute(
            "INSERT INTO validation_results "
            "(strategy_id, best_params, is_gt_score, walk_forward_gt_score, holdout_gt_score, final_status, tested_at, torture_flags) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            ('xau_usd_volatility_v1', '{"n": 20}', 1.5, 0.8, 0.7, 'PASS (D)', '2026-05-19T12:01:00', '[]'),
        )

    yield tmp
    tmp.unlink(missing_ok=True)


class TestGetRecentResults:
    def test_query_does_not_crash(self, temp_db):
        """Regression: meta_review SQL referenced s.instrument which doesn't exist."""
        results = mr.get_recent_results(limit=10)
        assert len(results) == 2

    def test_instrument_inferred_from_compact_id(self, temp_db):
        results = mr.get_recent_results(limit=10)
        row = next(r for r in results if r['strategy_id'].startswith('gbpusd_auto'))
        assert row['instrument'] == 'GBP_USD'

    def test_instrument_inferred_from_expanded_id(self, temp_db):
        results = mr.get_recent_results(limit=10)
        row = next(r for r in results if r['strategy_id'].startswith('xau_usd'))
        assert row['instrument'] == 'XAU_USD'


class TestInferInstrument:
    def test_compact_form(self):
        assert mr._infer_instrument_from_id('gbpusd_auto_20260519_120000_i1') == 'GBP_USD'
        assert mr._infer_instrument_from_id('eurusd_auto_x_i2') == 'EUR_USD'
        assert mr._infer_instrument_from_id('btcusd_v3') == 'BTC_USD'

    def test_expanded_form(self):
        assert mr._infer_instrument_from_id('xau_usd_v1') == 'XAU_USD'
        assert mr._infer_instrument_from_id('eur_jpy_meanrev_v2') == 'EUR_JPY'

    def test_unknown_returns_unknown(self):
        assert mr._infer_instrument_from_id('weird_name_xyz') == 'unknown'

    def test_empty_returns_unknown(self):
        assert mr._infer_instrument_from_id('') == 'unknown'


class TestAnalyzePatterns:
    def test_runs_on_real_query_results(self, temp_db):
        """End-to-end: get_recent_results + analyze_patterns must not crash."""
        results = mr.get_recent_results(limit=10)
        analysis = mr.analyze_patterns(results)
        assert analysis['total'] == 2
        assert analysis['passed_count'] == 1
        assert analysis['failed_count'] == 1
        assert 'GBP_USD' in analysis['inst_stats']
        assert 'XAU_USD' in analysis['inst_stats']
