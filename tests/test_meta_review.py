"""
Tests for meta_review.py — guards against the SQL schema mismatch that
silently broke meta-review for an extended period.
"""
import sys
import math
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

    def test_avg_is_excludes_non_finite(self):
        # A -inf is_gt_score (degenerate GT computation stored in the DB) must not
        # drag avg_is to -inf and corrupt the role-proposal trigger.
        results = [
            {'final_status': 'FAIL: x', 'is_gt_score': float('-inf'),
             'walk_forward_gt_score': 0.0, 'strategy_id': 'a_auto_1', 'rationale': ''},
            {'final_status': 'FAIL: x', 'is_gt_score': 0.2,
             'walk_forward_gt_score': 0.05, 'strategy_id': 'b_auto_1', 'rationale': ''},
            {'final_status': 'PASS (H4)', 'is_gt_score': 0.8,
             'walk_forward_gt_score': 0.7, 'strategy_id': 'c_auto_1', 'rationale': ''},
        ]
        a = mr.analyze_patterns(results)
        assert math.isfinite(a['avg_is'])
        assert a['avg_is'] == 0.5  # mean(0.2, 0.8); the -inf row is excluded


class TestRoleProposal:
    """Propose-only Role-revision flow: trigger gate, parsing, cooldown, apply."""

    def _isolate(self, monkeypatch, tmp_path):
        """Point proposals dir + thesis.md at a temp sandbox with ROLE markers."""
        propdir = tmp_path / '.role-proposals'
        thesis = tmp_path / 'thesis.md'
        thesis.write_text(
            "# Thesis Generation Rules\n\n## Role\n"
            "<!-- ROLE_START -->\n"
            "You are a quant researcher. " + "Original role body. " * 5 + "\n"
            "<!-- ROLE_END -->\n\n## Strategy Families\n"
        )
        monkeypatch.setattr(mr, 'ROLE_PROPOSALS_DIR', propdir)
        monkeypatch.setattr(mr, 'THESIS_MD', thesis)
        monkeypatch.setattr(mr, '_notify_role_proposal', lambda p: None)
        return propdir, thesis

    def test_dominant_pattern_detected(self):
        a = {'gate_counts': {'wf': 18, 'is': 1, 'sparse': 1, 'code': 9, 'data': 4}}
        p = mr._dominant_failure_pattern(a)
        assert p and p['stage'] == 'wf' and p['count'] == 18

    def test_plumbing_stages_excluded(self):
        # code/data/duplicate must not trigger a Role proposal
        a = {'gate_counts': {'code': 20, 'data': 10, 'duplicate': 5, 'wf': 1}}
        assert mr._dominant_failure_pattern(a) is None

    def test_diffuse_pattern_no_trigger(self):
        a = {'gate_counts': {'is': 5, 'wf': 5, 'sparse': 4, 'holdout': 3}}
        assert mr._dominant_failure_pattern(a) is None

    def test_too_few_failures_no_trigger(self):
        a = {'gate_counts': {'wf': 3, 'is': 1}}
        assert mr._dominant_failure_pattern(a) is None

    def test_no_change_writes_nothing(self, monkeypatch, tmp_path):
        propdir, _ = self._isolate(monkeypatch, tmp_path)
        monkeypatch.setattr(mr, 'call_llm', lambda s, u, **kw: 'NO_CHANGE')
        a = {'gate_counts': {'wf': 18, 'is': 1, 'other': 1}, 'avg_is': 0.05, 'avg_wf': 0.0,
             'recent_rationales': ['x']}
        assert mr.propose_role_revision(a) is None
        assert not propdir.exists() or not list(propdir.glob('*.json'))

    def test_propose_saves_and_can_apply(self, monkeypatch, tmp_path):
        propdir, thesis = self._isolate(monkeypatch, tmp_path)
        new_role = 'You are a disciplined quant. ' + 'Revised body sentence. ' * 4
        monkeypatch.setattr(mr, 'call_llm', lambda s, u, **kw: 'PROPOSE\n' + new_role)
        a = {'gate_counts': {'wf': 18, 'is': 1, 'other': 1}, 'avg_is': 0.05, 'avg_wf': 0.0,
             'recent_rationales': ['unconditional mean reversion']}
        prop = mr.propose_role_revision(a)
        assert prop is not None
        assert list(propdir.glob('role_proposal_*.json'))
        # thesis.md must be UNCHANGED by propose
        assert 'Original role body' in thesis.read_text()
        # now apply and confirm swap
        assert mr.apply_role_proposal() is True
        assert 'disciplined quant' in mr.extract_current_role()
        assert 'Original role body' not in thesis.read_text()

    def test_cooldown_blocks_second_proposal(self, monkeypatch, tmp_path):
        propdir, _ = self._isolate(monkeypatch, tmp_path)
        monkeypatch.setattr(mr, 'call_llm', lambda s, u, **kw: 'PROPOSE\n' + 'Body. ' * 20)
        a = {'gate_counts': {'wf': 18, 'is': 1, 'other': 1}, 'avg_is': 0.0, 'avg_wf': 0.0,
             'recent_rationales': []}
        assert mr.propose_role_revision(a) is not None
        # immediate second call is within cooldown -> blocked
        assert mr.propose_role_revision(a) is None

    def test_cooldown_counts_processed_proposals(self, monkeypatch, tmp_path):
        # Regression: acting on a proposal renames it to *.md.applied / *.md.rejected.
        # The cooldown must still count those, or processing a proposal would reset
        # the cooldown and let another fire immediately (several per day).
        propdir, _ = self._isolate(monkeypatch, tmp_path)
        propdir.mkdir(parents=True, exist_ok=True)
        (propdir / 'role_proposal_20260604_160214.md.rejected').write_text('x')
        assert mr._role_proposal_on_cooldown() is True
        # A fresh PROPOSE must be blocked by the cooldown from the rejected one.
        monkeypatch.setattr(mr, 'call_llm', lambda s, u, **kw: 'PROPOSE\n' + 'Body. ' * 20)
        a = {'gate_counts': {'wf': 18, 'is': 1, 'other': 1}, 'avg_is': 0.0,
             'avg_wf': 0.0, 'recent_rationales': []}
        assert mr.propose_role_revision(a) is None

    def test_malformed_llm_output_discarded(self, monkeypatch, tmp_path):
        propdir, _ = self._isolate(monkeypatch, tmp_path)
        monkeypatch.setattr(mr, 'call_llm', lambda s, u, **kw: 'here is some prose without a verdict')
        a = {'gate_counts': {'wf': 18, 'is': 1, 'other': 1}, 'avg_is': 0.0, 'avg_wf': 0.0,
             'recent_rationales': []}
        assert mr.propose_role_revision(a) is None

    def test_prompt_loads_from_md_file(self):
        # role_reviewer.md must exist and contain the format placeholders
        assert mr.ROLE_REVIEWER_MD.exists()
        tmpl = mr._load_role_proposal_prompt()
        for ph in ('{stage}', '{count}', '{total}', '{current_role}', '{rationales}'):
            assert ph in tmpl
        # must format cleanly with the kwargs propose_role_revision supplies
        tmpl.format(stage='wf', count=1, total=2, pct=50, avg_is=0.0,
                    avg_wf=0.0, rationales='-', current_role='x')

    def test_prompt_falls_back_when_md_missing(self, monkeypatch, tmp_path):
        monkeypatch.setattr(mr, 'ROLE_REVIEWER_MD', tmp_path / 'nonexistent.md')
        tmpl = mr._load_role_proposal_prompt()
        assert 'PROPOSE' in tmpl and '{current_role}' in tmpl

    def test_force_bypasses_cooldown(self, monkeypatch, tmp_path):
        propdir, _ = self._isolate(monkeypatch, tmp_path)
        monkeypatch.setattr(mr, 'call_llm', lambda s, u, **kw: 'PROPOSE\n' + 'Body. ' * 20)
        a = {'gate_counts': {'wf': 18, 'is': 1, 'other': 1}, 'avg_is': 0.0, 'avg_wf': 0.0,
             'recent_rationales': []}
        assert mr.propose_role_revision(a) is not None
        # within cooldown: normal call blocked, force=True still proposes
        assert mr.propose_role_revision(a) is None
        assert mr.propose_role_revision(a, force=True) is not None
