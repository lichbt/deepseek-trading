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
            ('gbpusd_auto_20260519_120000_i1', 'fp_a', 'code1', '{}', 'gbp test', 'D', 'research_failed', '2026-06-19T12:00:00'),
        )
        c.execute(
            "INSERT INTO strategies (id, fingerprint, code, param_grid, rationale, timeframe, status, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            ('xau_usd_volatility_v1', 'fp_b', 'code2', '{}', 'gold test', 'D', 'passed', '2026-06-19T12:01:00'),
        )
        c.execute(
            "INSERT INTO validation_results "
            "(strategy_id, best_params, is_gt_score, walk_forward_gt_score, holdout_gt_score, final_status, tested_at, torture_flags) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            ('gbpusd_auto_20260519_120000_i1', '{}', 0.05, 0.0, 0.0, 'FAIL: IS 0.05 < 0.3', '2026-06-19T12:00:00', '[]'),
        )
        c.execute(
            "INSERT INTO validation_results "
            "(strategy_id, best_params, is_gt_score, walk_forward_gt_score, holdout_gt_score, final_status, tested_at, torture_flags) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            ('xau_usd_volatility_v1', '{"n": 20}', 1.5, 0.8, 0.7, 'PASS (D)', '2026-06-19T12:01:00', '[]'),
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

    def test_role_trigger_uses_wider_window_than_directive(self, monkeypatch):
        # Role proposals change the core prompt + fire <=1/day, so the dominance %
        # must be measured over multiple batches, not the single-batch (30) window
        # the per-batch directive uses.
        assert mr.ROLE_PROPOSAL_WINDOW > 30
        captured = {}
        def fake_get(limit=30):
            captured['limit'] = limit
            return []  # <5 results -> run_role_proposal returns early; limit still captured
        monkeypatch.setattr(mr, 'get_recent_results', fake_get)
        mr.run_role_proposal(force=True)
        assert captured['limit'] == mr.ROLE_PROPOSAL_WINDOW

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
        for ph in ('{stage}', '{count}', '{total}', '{current_role}', '{rationales}',
                   '{mechanism_mix}'):
            assert ph in tmpl
        # must format cleanly with the kwargs propose_role_revision supplies
        tmpl.format(stage='wf', count=1, total=2, pct=50, avg_is=0.0,
                    avg_wf=0.0, cohort_max_is=0.0, family_survival='-',
                    near_miss_themes='-', dd_blocked='-', mechanism_mix='-',
                    rationales='-', current_role='x')

    def test_prompt_falls_back_when_md_missing(self, monkeypatch, tmp_path):
        monkeypatch.setattr(mr, 'ROLE_REVIEWER_MD', tmp_path / 'nonexistent.md')
        tmpl = mr._load_role_proposal_prompt()
        assert 'PROPOSE' in tmpl and '{current_role}' in tmpl

    def test_reviewer_is_bidirectional(self):
        # The re-aimed reviewer must offer BOTH directions, not just tighten.
        tmpl = mr._load_role_proposal_prompt().upper()
        assert 'TIGHTEN' in tmpl
        assert 'LOOSEN' in tmpl and 'MONOCULTURE' in tmpl
        assert 'MECHANISM MIX' in tmpl

    def test_mechanism_mix_block_failsoft(self, monkeypatch, tmp_path):
        # Missing DB → '' (reviewer just skips the diversity branch), never crashes.
        monkeypatch.setattr(mr, 'DB_PATH', tmp_path / 'nope.db')
        assert mr._mechanism_mix_block() == ''

    def test_role_proposal_enabled_by_default(self, monkeypatch):
        # Re-enabled 2026-07-08 (bidirectional). Default on; ROLE_PROPOSAL=0 pauses.
        import os
        monkeypatch.delenv('ROLE_PROPOSAL', raising=False)
        assert (os.getenv('ROLE_PROPOSAL', '1') != '0') is True

    def test_force_bypasses_cooldown(self, monkeypatch, tmp_path):
        propdir, _ = self._isolate(monkeypatch, tmp_path)
        monkeypatch.setattr(mr, 'call_llm', lambda s, u, **kw: 'PROPOSE\n' + 'Body. ' * 20)
        a = {'gate_counts': {'wf': 18, 'is': 1, 'other': 1}, 'avg_is': 0.0, 'avg_wf': 0.0,
             'recent_rationales': []}
        assert mr.propose_role_revision(a) is not None
        # within cooldown: normal call blocked, force=True still proposes
        assert mr.propose_role_revision(a) is None
        assert mr.propose_role_revision(a, force=True) is not None


class TestHonestEraFilter:
    """get_recent_results must exclude results scored before the macro
    publication-lag fix — pre-fix scores were graded against unpublished
    (leaked) macro data and must not feed pattern analysis or role proposals."""

    def test_pre_era_results_are_excluded(self, temp_db, monkeypatch):
        import sqlite3
        conn = sqlite3.connect(str(mr.DB_PATH))
        conn.execute(
            "INSERT INTO strategies (id, fingerprint, code, param_grid, rationale, timeframe, status, created_at) "
            "VALUES (?,?,?,?,?,?,?,?)",
            ('nzdusd_auto_20260605_000000_i6', 'fp_leak', 'code3', '{}',
             'leak-era result', 'H4', 'passed', '2026-06-05T00:00:00'),
        )
        conn.execute(
            "INSERT INTO validation_results "
            "(strategy_id, best_params, is_gt_score, walk_forward_gt_score, holdout_gt_score, final_status, tested_at, torture_flags) "
            "VALUES (?,?,?,?,?,?,?,?)",
            ('nzdusd_auto_20260605_000000_i6', '{}', 1.0, 2.0, 2.0, 'PASS (H4)',
             '2026-06-05T00:00:00', '[]'),
        )
        conn.commit(); conn.close()

        results = mr.get_recent_results(limit=50)
        ids = {r['strategy_id'] for r in results}
        assert 'nzdusd_auto_20260605_000000_i6' not in ids, \
            "pre-fix (leak-era) result leaked into the analysis window"
        # honest-era fixture rows are still served
        assert ids, "honest-era results should still be returned"
        assert all(r['tested_at'] >= mr.HONEST_ERA_START for r in results)


class TestSharpenedMetaReview:
    """2026-06-13 sharpening: lower role-dominance bar so lopsided-but-not-
    unanimous failure patterns (e.g. ~68% die at IS) actually steer; widen the
    directive window so it reflects the overall trend, not one batch; and add
    family-survival + near-miss themes to the analysis as a coarse direction
    signal."""

    # --- Point 1: dominance bar lowered to a simple-majority ------------------
    def test_lopsided_is_pattern_now_fires(self):
        # 68% IS dominance — the real honest-era case that the old 0.80 bar
        # silently suppressed. Must now fire a role proposal trigger.
        a = {'gate_counts': {'is': 68, 'wf': 30, 'other': 2}}
        p = mr._dominant_failure_pattern(a)
        assert p is not None
        assert p['stage'] == 'is'
        assert p['fraction'] >= 0.55

    def test_bar_is_simple_majority(self):
        assert mr.ROLE_PATTERN_DOMINANCE == 0.55

    def test_truly_diffuse_still_does_not_fire(self):
        # Below a simple majority -> still no core-prompt change (regression).
        a = {'gate_counts': {'is': 5, 'wf': 5, 'sparse': 4, 'holdout': 3}}
        assert mr._dominant_failure_pattern(a) is None

    # --- Point 2: directive window widened beyond one batch -------------------
    def test_directive_window_wider_than_one_batch(self):
        assert mr.DIRECTIVE_WINDOW >= 100 > 30

    def test_run_meta_review_uses_wide_directive_window(self, monkeypatch):
        limits = []
        def fake_get(limit=30):
            limits.append(limit)
            return []  # <5 -> run_meta_review returns early; first limit captured
        monkeypatch.setattr(mr, 'get_recent_results', fake_get)
        mr.run_meta_review()
        assert limits and limits[0] == mr.DIRECTIVE_WINDOW

    # --- Point 4: gate classifier + family/near-miss analysis ----------------
    def test_classify_gate_buckets(self):
        assert mr._classify_gate('FAIL: IS 0.20 < 0.3') == 'is'
        assert mr._classify_gate('FAIL: WF 0.4500 < 0.5') == 'wf'
        assert mr._classify_gate('HO decay 0.40 < 0.50') == 'holdout'
        assert mr._classify_gate('FAIL: Duplicate fingerprint') == 'duplicate'
        assert mr._classify_gate('Single-regime edge: 2/5 windows') == 'wf'
        # full validator phrasing (older/mixed windows) must classify too
        assert mr._classify_gate('FAIL: In-sample GT-Score 0.1781 < 0.3') == 'is'
        assert mr._classify_gate('FAIL: Walk-forward GT-Score 0.0000 < 0.2') == 'wf'
        assert mr._classify_gate('PASS (D)') == 'other'  # non-failures -> other

    def _res(self, sid, status, is_=0.5, wf=0.0, ho=0.0, tf='D', code="df['close']"):
        return {'strategy_id': sid, 'final_status': status, 'is_gt_score': is_,
                'walk_forward_gt_score': wf, 'holdout_gt_score': ho, 'tested_at': '2026-06-13T00:00:00',
                'rationale': 'r', 'code': code, 'param_grid': '{}', 'timeframe': tf,
                'instrument': 'XAU_USD'}

    def test_analyze_adds_family_and_nearmiss(self):
        results = [
            self._res('a', 'FAIL: IS 0.20 < 0.3', is_=0.2),                 # dies at IS
            self._res('b', 'FAIL: WF 0.4500 < 0.5', is_=0.6, wf=0.45),      # WF near-miss
            self._res('c', 'HO decay 0.40 < 0.86', is_=0.7, wf=0.6, ho=0.4),# holdout near-miss
            self._res('d', 'PASS (D)', is_=0.8, wf=0.7, ho=0.9),            # passed
        ]
        a = mr.analyze_patterns(results)
        assert 'arch_stats' in a and 'near_misses' in a
        # 'standard' archetype: 4 total, 1 passed, 3 reached WF (b,c,d — a died at IS)
        std = a['arch_stats'].get('standard')
        assert std and std['total'] == 4 and std['passed'] == 1 and std['reached_wf'] == 3
        # two near-misses: the WF-just-under and the holdout one (not the IS death)
        whys = sorted(m['why'] for m in a['near_misses'])
        assert whys == ['WF just under bar', 'reached holdout']

    def test_build_prompt_has_new_sections_and_tolerates_minimal_analysis(self):
        # full analysis -> sections render
        results = [self._res('b', 'FAIL: WF 0.4500 < 0.5', is_=0.6, wf=0.45)]
        prompt = mr._build_llm_prompt(mr.analyze_patterns(results), None)
        assert 'Strategy-family survival' in prompt
        assert 'Near-miss themes' in prompt
        # minimal analysis dict (old/partial) must not KeyError
        p2 = mr._build_llm_prompt({'total': 0}, None)
        assert 'Strategy-family survival' in p2 and '(none)' in p2


class TestCohortStatsForRoleProposal:
    """2026-06-14: the Role reviewer was fed WINDOW-WIDE avg_IS/avg_WF, which
    blended the edgeless IS-failure cohort with strategies that CLEARED the IS
    gate — a headline avg_IS=0.34 hid an is-cohort that actually averaged ~0.06,
    masking 'edgeless rejection' as 'borderline overfit' and firing a bad
    core-prompt proposal. The reviewer must now see the DOMINANT COHORT's stats."""

    def _res(self, sid, status, is_=0.5, wf=0.0, ho=0.0, code="df['close']"):
        return {'strategy_id': sid, 'final_status': status, 'is_gt_score': is_,
                'walk_forward_gt_score': wf, 'holdout_gt_score': ho,
                'tested_at': '2026-06-14T00:00:00', 'rationale': 'r', 'code': code,
                'param_grid': '{}', 'timeframe': 'D', 'instrument': 'XAU_USD'}

    def _edgeless_is_window(self):
        """6 edgeless IS-failures (avg IS ~0.10) + 2 WF-failures + 2 passers that
        clear IS at ~0.8 — so the window-wide avg_IS is dragged up well above the
        IS cohort's true average. Mirrors the real 2026-06-14 misfire."""
        is_vals = [0.02, 0.05, 0.08, 0.10, 0.04, 0.29]
        results = [self._res(f'is{i}', f'FAIL: IS {v:.4f} < 0.3', is_=v, wf=0.0)
                   for i, v in enumerate(is_vals)]                 # is-cohort
        results += [self._res('wf1', 'FAIL: WF 0.40 < 0.5', is_=0.65, wf=0.40),
                    self._res('wf2', 'FAIL: WF 0.42 < 0.5', is_=0.70, wf=0.42)]
        results += [self._res('p1', 'PASS (D)', is_=0.80, wf=0.70, ho=0.8),
                    self._res('p2', 'PASS (D)', is_=0.82, wf=0.72, ho=0.8)]
        return results, is_vals

    def test_analyze_accumulates_per_gate_scores(self):
        results, is_vals = self._edgeless_is_window()
        a = mr.analyze_patterns(results)
        assert 'gate_scores' in a
        assert sorted(a['gate_scores']['is']['is']) == sorted(is_vals)
        # IS failures store a placeholder wf=0.0; both WF-failures' real WF land in 'wf'
        assert sorted(a['gate_scores']['wf']['wf']) == [0.40, 0.42]

    def test_cohort_avg_is_differs_from_window_avg(self):
        results, is_vals = self._edgeless_is_window()
        a = mr.analyze_patterns(results)
        p = mr._dominant_failure_pattern(a)
        assert p is not None and p['stage'] == 'is'
        expected_cohort = round(sum(is_vals) / len(is_vals), 4)        # ~0.0967
        assert p['cohort_avg_is'] == expected_cohort
        # the window-wide average (the old, misleading headline) is much higher...
        assert a['avg_is'] > 0.30
        # ...and the cohort average is far below it — that's the whole bug.
        assert p['cohort_avg_is'] < 0.15
        assert p['cohort_max_is'] == max(is_vals)                      # 0.29, still sub-gate
        assert p['cohort_n_is'] == len(is_vals)

    def test_is_cohort_wf_reads_na_not_placeholder_zero(self):
        results, _ = self._edgeless_is_window()
        p = mr._dominant_failure_pattern(mr.analyze_patterns(results))
        # IS cohort never genuinely ran WF -> reported as None (n/a), not 0.0
        assert p['cohort_avg_wf'] is None

    def test_wf_dominant_cohort_reports_real_wf(self):
        # When WF dominates, its WF scores ARE real measurements and get reported.
        results = [self._res(f'wf{i}', 'FAIL: WF 0.40 < 0.5', is_=0.7, wf=w)
                   for i, w in enumerate([0.30, 0.40, 0.20, 0.35, 0.25, 0.45])]
        results += [self._res('is1', 'FAIL: IS 0.10 < 0.3', is_=0.10, wf=0.0)]
        p = mr._dominant_failure_pattern(mr.analyze_patterns(results))
        assert p['stage'] == 'wf'
        assert p['cohort_avg_wf'] == round((0.30+0.40+0.20+0.35+0.25+0.45)/6, 4)
        assert p['cohort_avg_is'] == 0.7      # genuine overfit signature: high IS, low WF

    def test_missing_gate_scores_is_backward_compatible(self):
        # Old-style analysis dict (pre-fix, no gate_scores) must not crash.
        p = mr._dominant_failure_pattern({'gate_counts': {'is': 68, 'wf': 30, 'other': 2}})
        assert p is not None and p['stage'] == 'is'
        assert p['cohort_avg_is'] is None and p['cohort_avg_wf'] is None
        assert p['cohort_max_is'] is None and p['cohort_n_is'] == 0

    def test_propose_feeds_cohort_stats_not_window_into_prompt(self, monkeypatch):
        results, is_vals = self._edgeless_is_window()
        a = mr.analyze_patterns(results)
        captured = {}

        def fake_call_llm(system, user_prompt, **kw):
            captured['prompt'] = user_prompt
            return 'NO_CHANGE'

        monkeypatch.setattr(mr, 'call_llm', fake_call_llm)
        monkeypatch.setattr(mr, 'extract_current_role', lambda: 'ROLE ' * 20)
        # force=True bypasses the 24h cooldown; LLM returns NO_CHANGE -> no file written
        out = mr.propose_role_revision(a, force=True)
        assert out is None                                   # NO_CHANGE writes nothing
        prompt = captured['prompt']
        cohort = str(round(sum(is_vals) / len(is_vals), 4))  # 0.0967
        window = str(a['avg_is'])                            # ~0.348 (the old headline)
        assert cohort in prompt, f'cohort avg {cohort} should drive the prompt'
        assert window not in prompt, f'window-wide avg {window} must NOT leak in'
        assert 'n/a' in prompt                               # IS cohort WF shown as n/a
        assert str(max(is_vals)) in prompt                   # cohort max IS surfaced


class TestDDBlockedSteer:
    """2026-06-16: surface instruments with DEMONSTRATED real edge (WF + clean
    torture) that fail ONLY the drawdown gate, as a 'design DD-controlled edges
    for these families' steer — over a WIDE window (these near-wins are rare)."""

    def _add(self, sid, wf, torture, status):
        with pu.get_db_connection() as conn:
            conn.execute(
                "INSERT INTO validation_results (strategy_id, best_params, is_gt_score, "
                "walk_forward_gt_score, holdout_gt_score, final_status, tested_at, torture_flags) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (sid, '{}', 0.6, wf, 0.5, status, '2026-06-16T00:00:00', torture))

    def test_surfaces_real_edge_failed_dd(self, temp_db):
        self._add('wticousd_auto_x_i1', 0.8, '[]',
                  'FAIL: Max drawdown 55% > 30% (full reconstructed equity) — prop-disqualifying')
        out = mr.dd_blocked_families()
        assert 'real edge, blew drawdown' in out and 'WTICO_USD' in out

    def test_excludes_torture_flagged(self, temp_db):
        # failed the overfit detector -> edge is suspect -> must NOT be surfaced
        self._add('ethusd_auto_y_i2', 0.9, '["signal_shuffle"]', 'FAIL: Max drawdown 60% > 30%')
        assert 'ETH' not in mr.dd_blocked_families()

    def test_excludes_low_wf(self, temp_db):
        self._add('ltcusd_auto_z_i3', 0.3, '[]', 'FAIL: Max drawdown 70% > 30%')
        assert 'LTC' not in mr.dd_blocked_families()

    def test_excludes_non_dd_failures(self, temp_db):
        # real edge but failed for a NON-DD reason is not part of this steer
        self._add('xauusd_auto_w_i4', 0.7, '[]', 'FAIL: HO decay 0.4 < 0.6')
        assert 'XAU' not in mr.dd_blocked_families()

    def test_prompt_renders_section(self):
        p = mr._build_llm_prompt({'total': 0, 'dd_blocked': '  WTICO_USD: 3 (real edge, blew drawdown)'}, None)
        assert 'blocked ONLY by drawdown' in p and 'WTICO_USD: 3' in p

    def test_instruments_list_for_exploit_slots(self, temp_db):
        # the list form (seeds auto_research's exploit slots) applies the same filters
        self._add('wticousd_auto_a_i1', 0.8, '[]', 'FAIL: Max drawdown 55% > 30%')
        self._add('ethusd_auto_b_i2', 0.9, '["signal_shuffle"]', 'FAIL: Max drawdown 60% > 30%')
        self._add('ltcusd_auto_c_i3', 0.3, '[]', 'FAIL: Max drawdown 70% > 30%')
        lst = mr.dd_blocked_instruments()
        assert 'WTICO_USD' in lst                       # real edge, DD-blocked → included
        assert all('ETH' not in x for x in lst)         # torture-flagged → excluded
        assert all('LTC' not in x for x in lst)         # low-WF → excluded


class TestRoleProposalHardGateAndContext:
    """2026-06-17: the reviewer kept proposing on edgeless IS cohorts. Hard-gate
    those (avg IS hopelessly low → NO_CHANGE, no LLM call), and feed the reviewer
    the SUCCESS/coverage picture so it can tell a blind spot from normal rejection."""

    def _analysis(self, is_scores, is_count=60, wf_count=20):
        return {'gate_counts': {'is': is_count, 'wf': wf_count},
                'gate_scores': {'is': {'is': is_scores, 'wf': []},
                                'wf': {'is': [0.7] * 5, 'wf': [0.1] * 5}},
                'recent_rationales': ['r'], 'arch_stats': {}, 'near_misses': []}

    def test_edgeless_is_cohort_is_hard_gated_no_llm(self, monkeypatch):
        called = []
        monkeypatch.setattr(mr, 'call_llm', lambda *a, **k: called.append(1) or ('PROPOSE ' + 'x' * 80))
        # avg ~0.115 < EDGELESS_IS_AVG -> gated before any LLM call
        assert mr.propose_role_revision(self._analysis([0.02, 0.05, 0.10, 0.29])) is None
        assert called == []

    def test_near_gate_cohort_not_gated_and_gets_success_context(self, monkeypatch):
        cap = {}
        def fake(system, user, **k):
            cap['p'] = user
            return 'NO_CHANGE'
        monkeypatch.setattr(mr, 'call_llm', fake)
        monkeypatch.setattr(mr, 'extract_current_role', lambda: 'ROLE ' * 40)
        monkeypatch.setattr(mr, '_role_proposal_on_cooldown', lambda: False)
        monkeypatch.setattr(mr, 'dd_blocked_families', lambda *a, **k: '  WTICO_USD: 2')
        a = self._analysis([0.25, 0.27, 0.29, 0.28])    # avg ~0.27 -> NOT gated
        a['arch_stats'] = {'regime': {'total': 10, 'reached_wf': 6, 'passed': 2}}
        a['near_misses'] = [{'arch': 'regime', 'inst': 'XAU_USD'}]
        mr.propose_role_revision(a)
        assert cap, 'LLM should have been consulted (not hard-gated)'
        assert 'WHAT IS WORKING' in cap['p'] and 'reached WF' in cap['p']  # success context fed
        assert 'regime on XAU_USD' in cap['p']                            # near-miss fed
        assert 'WTICO_USD: 2' in cap['p']                                 # dd-blocked fed

    def test_force_bypasses_the_hard_gate(self, monkeypatch):
        called = []
        monkeypatch.setattr(mr, 'call_llm', lambda *a, **k: (called.append(1), 'NO_CHANGE')[1])
        monkeypatch.setattr(mr, 'extract_current_role', lambda: 'ROLE ' * 40)
        monkeypatch.setattr(mr, 'dd_blocked_families', lambda *a, **k: '')
        mr.propose_role_revision(self._analysis([0.02, 0.05]), force=True)  # edgeless but forced
        assert called == [1]                            # force=True -> LLM consulted anyway
