"""Jev near-duplicate triage: fail-open, the Choice escape hatch, band routing.

No HTTP: `judge_duplicate` takes an injectable `_call`, so every case here runs
against a fake OpenRouter Decisions response.
"""
import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

import auto_research as ar
import jev_candidates as jc
import jev_client

CANDS = {'old_1': 'momentum breakout donchian channel daily',
         'old_2': 'mean reversion rsi oversold'}


def _fake_jev(noul, closest):
    def _call(payload):
        qs = payload['questions']
        choice = qs['closest']
        # Contract: one Noul per candidate, then one Choice whose criteria MUST
        # include the 'unclear' escape hatch.
        assert choice['type'] == 'choice'
        assert 'unclear' in choice['criteria']
        noul_qs = {k: v for k, v in qs.items() if k.startswith('noul:')}
        assert noul_qs and all(v['type'] == 'noul' for v in noul_qs.values())
        answers = {k: {'type': 'noul', 'noul': noul[k[5:]]} for k in noul_qs}
        answers['closest'] = {
            'type': 'choice', 'choice': closest,
            'probabilities': {**noul, 'unclear': 0.0}}
        return {'answers': answers}
    return _call


@pytest.fixture(autouse=True)
def _jev_on(monkeypatch):
    monkeypatch.setattr(jev_client, 'JEV_ENABLED', True)
    monkeypatch.setattr(jev_client, 'JEV_API_KEY', 'test-key')
    yield


def test_judge_duplicate_returns_probabilities_and_closest():
    out = jev_client.judge_duplicate(
        'donchian breakout', dict(CANDS), _call=_fake_jev({'old_1': 0.81, 'old_2': 0.10}, 'old_1'))
    assert out['available'] is True
    assert out['noul'] == {'old_1': 0.81, 'old_2': 0.10}
    assert out['closest'] == 'old_1'
    assert out['closest_probability'] == 0.81


def test_judge_duplicate_fails_open_on_transport_error():
    def boom(payload):
        raise TimeoutError('jev down')
    out = jev_client.judge_duplicate('x', dict(CANDS), _call=boom)
    assert out['available'] is False
    assert 'TimeoutError' in out['error']


def test_judge_duplicate_skips_without_pool():
    out = jev_client.judge_duplicate('x', {'only': 'one'}, _call=_fake_jev({}, 'unclear'))
    assert out['available'] is False


def test_judge_duplicate_skips_without_key(monkeypatch):
    monkeypatch.setattr(jev_client, 'JEV_API_KEY', '')
    out = jev_client.judge_duplicate('x', dict(CANDS), _call=_fake_jev({}, 'unclear'))
    assert out['available'] is False and 'OPENROUTER_API_KEY' in out['error']


def test_route_block_review_proceed_bands():
    assert ar._jev_dup_route(
        {'available': True, 'noul': {'old_1': 0.81}, 'closest': 'old_1',
         'closest_probability': 0.81})[0] == 'block'
    assert ar._jev_dup_route(
        {'available': True, 'noul': {'old_1': 0.55}, 'closest': 'old_1',
         'closest_probability': 0.55})[0] == 'review'
    assert ar._jev_dup_route(
        {'available': True, 'noul': {'old_1': 0.10}, 'closest': 'old_1',
         'closest_probability': 0.10})[0] == 'proceed'


def test_route_unclear_is_escape_hatch():
    """A high Noul pair with a Choice of 'unclear' must NOT block: the judge
    looked at every option and named none. Without this, thin matches get a
    forced label -- the documented recruiter-dedupe failure."""
    route, cid, _ = ar._jev_dup_route(
        {'available': True, 'noul': {'old_1': 0.95}, 'closest': 'unclear',
         'closest_probability': None})
    assert route == 'proceed'
    assert cid is None


def test_route_unavailable_proceeds():
    assert ar._jev_dup_route({'available': False})[0] == 'proceed'


def test_shortlist_ranks_token_overlap(monkeypatch):
    rows = [('a', 'momentum donchian channel breakout daily'),
            ('b', 'overnight carry interest rate differential')]
    monkeypatch.setattr(jc, '_recent', lambda instrument, limit: rows)
    monkeypatch.setattr(jc, '_book', lambda instrument, limit=40: [])
    got = jc.shortlist({'instrument': 'EUR_USD',
                        'rationale': 'donchian channel momentum breakout'}, k=2)
    assert list(got) == ['a']


def test_shortlist_seeds_the_live_book_first(monkeypatch):
    """A paraphrase of a passed sleeve must outrank 98k recent failures."""
    monkeypatch.setattr(jc, '_recent',
                        lambda instrument, limit: [('r1', 'donchian momentum breakout daily')])
    monkeypatch.setattr(jc, '_book',
                        lambda instrument, limit=40: [('b1', 'momentum breakout donchian')])
    got = jc.shortlist({'instrument': 'EUR_USD',
                        'rationale': 'donchian momentum breakout'}, k=2)
    assert list(got)[0] == 'b1'


def test_record_jev_triage_writes_shadow_row(tmp_path, monkeypatch):
    log = tmp_path / 'jev.jsonl'
    monkeypatch.setattr(ar, 'JEV_TRIAGE_LOG', str(log))
    ar._record_jev_triage({'rationale': 'x'}, 'EUR_USD',
                          {'available': True, 'noul': {'a': 0.9}, 'closest': 'a',
                           'error': None, 'closest_probability': 0.9},
                          'block', 'a', 0.9)
    row = json.loads(log.read_text().strip())
    assert row['route'] == 'block' and row['match_id'] == 'a'


def test_judge_failure_modes_returns_four_probs_and_recommended_action():
    def _call(payload):
        qs = payload['questions']
        assert set(jev_client.FAILURE_MODES).issubset(qs)
        action = qs['recommended_action']
        assert action['type'] == 'choice'
        assert set(action['criteria']) >= {'fix_prompt', 'fix_density', 'maintain',
                                           'retire', 'OTHER'}
        answers = {m: {'type': 'noul', 'noul': p}
                   for m, p in zip(jev_client.FAILURE_MODES, (0.05, 0.90, 0.02, 0.03))}
        answers['recommended_action'] = {
            'type': 'choice', 'choice': 'fix_density',
            'probabilities': {'fix_prompt': 0.1, 'fix_density': 0.7,
                              'maintain': 0.1, 'retire': 0.05, 'OTHER': 0.05}}
        return {'answers': answers}
    out = jev_client.judge_failure_modes({'final_status': 'FAIL: sparse trades'},
                                         _call=_call)
    assert out['available'] is True
    assert out['probs']['never_fires'] == 0.90
    assert out['combined'] == 'never_fires'
    assert out['recommended_action'] == 'fix_density'


def test_judge_thesis_defects_spec_questions_and_escape_options():
    """The exact four Nouls + dominant_defect Choice from the spec, with NONE
    and OTHER escape options -- without them a clean thesis gets a forced label."""
    def _call(payload):
        qs = payload['questions']
        assert set(qs) == {'filter_independent', 'exit_tied_to_entry',
                           'direction_stated', 'density_risk', 'mechanism_named',
                           'dominant_defect'}
        dom = qs['dominant_defect']
        assert dom['type'] == 'choice'
        assert set(dom['criteria']) == {'fidelity', 'regime', 'mechanism',
                                        'density', 'NONE', 'OTHER'}
        answers = {q: {'type': 'noul', 'noul': p}
                   for q, p in zip(('filter_independent', 'exit_tied_to_entry',
                                    'direction_stated', 'density_risk',
                                    'mechanism_named'),
                                   (0.8, 0.1, 0.05, 0.2, 0.7))}
        answers['dominant_defect'] = {
            'type': 'choice', 'choice': 'regime',
            'probabilities': {'fidelity': 0.1, 'regime': 0.7, 'mechanism': 0.05,
                              'density': 0.1, 'NONE': 0.03, 'OTHER': 0.02}}
        return {'answers': answers}
    out = jev_client.judge_thesis_defects({'rationale': 'x'}, _call=_call)
    assert out['available'] is True
    assert out['probs']['filter_independent'] == 0.8
    assert out['dominant_defect'] == 'regime'
    assert out['choices']['dominant_defect'] == 'regime'


def test_judge_thesis_defects_fails_open():
    out = jev_client.judge_thesis_defects('x', _call=lambda p: (_ for _ in ()).throw(OSError('down')))
    assert out['available'] is False and out['dominant_defect'] is None


def test_judge_failure_modes_fails_open():
    out = jev_client.judge_failure_modes('x', _call=lambda p: (_ for _ in ()).throw(ValueError('bad')))
    assert out['available'] is False and out['combined'] is None


def test_meta_review_shadow_writes_one_batch_row(tmp_path, monkeypatch):
    import meta_review as mr
    log = tmp_path / 'cat.jsonl'
    monkeypatch.setattr(mr, 'JEV_CATEGORY_LOG', str(log))
    seen = {}
    def fake(ctx):
        seen.update(ctx)
        return {'available': True, 'probs': {'generator_broken': 0.9},
                'combined': 'generator_broken', 'recommended_action': 'fix_prompt',
                'error': None}
    monkeypatch.setattr(jev_client, 'judge_failure_modes', fake)
    n = mr.record_jev_failure_modes([
        {'strategy_id': 's1', 'final_status': 'FAIL: sparse trades', 'is_gt_score': 0.0},
        {'strategy_id': 's2', 'final_status': 'FAIL: IS 0.1 < 0.3', 'is_gt_score': 0.1},
        {'strategy_id': 's3', 'final_status': 'passed'}])
    assert n == 1
    # one batch-level call, not one per failed row
    assert seen['failed'] == 2 and seen['passed'] == 1
    assert seen['gate_counts'] == {'sparse': 1, 'is': 1}
    row = json.loads(log.read_text().strip())
    assert row['combined'] == 'generator_broken'
    assert row['recommended_action'] == 'fix_prompt'
    assert row['context']['reject_rate'] == 0.667


def test_meta_review_shadow_no_failures_writes_nothing(tmp_path, monkeypatch):
    import meta_review as mr
    log = tmp_path / 'cat.jsonl'
    monkeypatch.setattr(mr, 'JEV_CATEGORY_LOG', str(log))
    n = mr.record_jev_failure_modes([{'final_status': 'passed'}])
    assert n == 0 and not log.exists()


def test_summarize_shadow_log_counts_routes(tmp_path):
    log = tmp_path / 'jev.jsonl'
    log.write_text('\n'.join(json.dumps(r) for r in [
        {'mode': 'shadow', 'available': True, 'route': 'block',
         'closest': 'a', 'probability': 0.9},
        {'mode': 'shadow', 'available': True, 'route': 'proceed',
         'closest': 'unclear', 'probability': None},
    ]) + '\n')
    text = jev_client.summarize_shadow_log(str(log))
    assert 'rows=2' in text and "'block': 1" in text and '>=0.70=1' in text


def test_default_is_shadow_and_opt_in():
    """Production defaults must never block and never call out: opt-in only."""
    src = Path(jev_client.__file__).read_text()
    assert "JEV_ENABLED = os.getenv('JEV_ENABLED', '0') == '1'" in src
    assert "JEV_TRIAGE_MODE = os.getenv('JEV_TRIAGE_MODE', 'shadow')" in src
    assert "os.getenv('JEV_MODEL', '~typesafe/jev-latest')" in src


def test_jev_client_loads_dotenv_at_import():
    """auto_research imports this module before its own _load_dotenv, so if
    jev_client stops loading .env itself, JEV_ENABLED silently reads False and
    the whole gate disappears. Source-level guard for that import-order trap."""
    src = Path(jev_client.__file__).read_text()
    assert "_load_env(Path(__file__).parent / '.env')" in src


def _write_log(path, rows):
    path.write_text('\n'.join(json.dumps(r) for r in rows) + '\n')


def test_novelty_avoid_reads_recent_blocks_newest_first(tmp_path, monkeypatch):
    log = tmp_path / 'jev.jsonl'
    _write_log(log, [
        {'route': 'block', 'thesis': {'rationale': 'OLD donchian breakout'}},
        {'route': 'proceed', 'thesis': {'rationale': 'unrelated idea'}},
        {'route': 'review', 'thesis': {'rationale': 'NEW bar-geometry exhaustion'}},
        {'route': 'block', 'thesis': {'rationale': 'NEW bar-geometry exhaustion'}},
    ])
    monkeypatch.setattr(ar, 'JEV_TRIAGE_LOG', str(log))
    monkeypatch.setattr(ar, 'JEV_NOVELTY_FEEDBACK', True)
    monkeypatch.setattr(jev_client, 'JEV_ENABLED', True)
    got = ar._jev_avoid_mechanisms()
    assert got == ['NEW bar-geometry exhaustion', 'OLD donchian breakout']
    block = ar._jev_avoid_block()
    assert 'Do NOT restate them in new words' in block
    assert 'NEW bar-geometry exhaustion' in block


def test_novelty_avoid_fail_soft(monkeypatch):
    monkeypatch.setattr(ar, 'JEV_TRIAGE_LOG', '/nonexistent/jev.jsonl')
    monkeypatch.setattr(ar, 'JEV_NOVELTY_FEEDBACK', True)
    monkeypatch.setattr(jev_client, 'JEV_ENABLED', True)
    assert ar._jev_avoid_mechanisms() == []
    assert ar._jev_avoid_block() == ''


def test_novelty_avoid_off_when_disabled(monkeypatch):
    monkeypatch.setattr(ar, 'JEV_NOVELTY_FEEDBACK', False)
    monkeypatch.setattr(jev_client, 'JEV_ENABLED', True)
    assert ar._jev_avoid_mechanisms() == []


def test_diverge_thesis_keeps_schedule_identity(monkeypatch):
    def _call(prompt):
        assert 'NEAR-DUPLICATE' in prompt
        return {'success': True, 'candidate': {
            'rationale': 'a genuinely different driver',
            'instrument': 'WRONG', 'timeframe': 'H4', 'entry_condition': 'x'}}
    out = ar._diverge_thesis({'instrument': 'EUR_USD', 'timeframe': 'D',
                              'rationale': 'old idea'}, 'match1', 0.9, 'EUR_USD',
                             _call=_call)
    assert out['instrument'] == 'EUR_USD'      # schedule wins over the model
    assert out['timeframe'] == 'D'
    assert out['rationale'] == 'a genuinely different driver'


def test_diverge_thesis_fails_open():
    out = ar._diverge_thesis({'instrument': 'EUR_USD', 'rationale': 'old'},
                             'm', 0.9, 'EUR_USD',
                             _call=lambda p: {'success': False, 'candidate': None})
    assert out is None


def test_diverge_thesis_unwraps_single_object_array():
    out = ar._diverge_thesis({'instrument': 'EUR_USD', 'timeframe': 'D', 'rationale': 'old'},
                             'm', 0.9, 'EUR_USD',
                             _call=lambda p: {'success': True, 'candidate': [
                                 {'rationale': 'different', 'entry_condition': 'x'}]})
    assert isinstance(out, dict) and out['rationale'] == 'different'


def test_jev_signal_block_feeds_directive_prompt(tmp_path, monkeypatch):
    import meta_review as mr
    triage = tmp_path / 't.jsonl'
    _write_log(triage, [
        {'route': 'block', 'dominant_defect': 'fidelity'},
        {'route': 'proceed', 'dominant_defect': 'regime'},
        {'route': 'block', 'dominant_defect': 'fidelity'},
    ])
    cat = tmp_path / 'c.jsonl'
    _write_log(cat, [{'combined': 'generator_broken', 'recommended_action': 'fix_prompt'}])
    monkeypatch.setenv('JEV_TRIAGE_LOG', str(triage))
    monkeypatch.setattr(mr, 'JEV_CATEGORY_LOG', str(cat))
    monkeypatch.setattr(jev_client, 'JEV_ENABLED', True)
    block = mr._jev_signal_block()
    assert 'fidelity=2' in block and 'block=2 (66%)' in block
    assert 'generator_broken' in block and 'fix_prompt' in block
    prompt = mr._build_llm_prompt({'total': 0, 'gate_counts': {}, 'tf_stats': {},
                                   'inst_stats': {}, 'arch_stats': {}}, None)
    assert 'Generator defect mix' in prompt
    monkeypatch.setattr(mr, '_jev_signal_block', lambda *a, **k: 'SENTINEL')
    assert 'SENTINEL' in mr._build_llm_prompt(
        {'total': 0, 'gate_counts': {}, 'tf_stats': {}, 'inst_stats': {}, 'arch_stats': {}}, None)


def test_jev_signal_block_empty_when_no_log(tmp_path, monkeypatch):
    import meta_review as mr
    monkeypatch.setenv('JEV_TRIAGE_LOG', str(tmp_path / 'missing.jsonl'))
    monkeypatch.setattr(jev_client, 'JEV_ENABLED', True)
    assert mr._jev_signal_block() == ''


def test_thesis_text_carries_identity_for_density_judgment():
    text = jc.thesis_text({'instrument': 'EUR_USD', 'timeframe': 'M30',
                           'rationale': 'x', 'param_hints': {'lookback': [20, 40]}})
    assert 'instrument=EUR_USD' in text
    assert 'timeframe=M30' in text
    assert 'lookback' in text


def test_jev_recent_stats_rolls_up_routes_and_defects(tmp_path, monkeypatch):
    import meta_review as mr
    log = tmp_path / 't.jsonl'
    _write_log(log, [
        {'route': 'block', 'dominant_defect': 'fidelity'},
        {'route': 'block', 'dominant_defect': 'fidelity'},
        {'route': 'proceed'},
    ])
    monkeypatch.setenv('JEV_TRIAGE_LOG', str(log))
    stats = mr._jev_recent_stats(200)
    assert stats['n'] == 3
    assert stats['routes']['block'] == 2
    assert stats['defects'] == {'fidelity': 2}


def test_jev_recent_stats_cold_log(tmp_path, monkeypatch):
    import meta_review as mr
    monkeypatch.setenv('JEV_TRIAGE_LOG', str(tmp_path / 'missing.jsonl'))
    assert mr._jev_recent_stats(10) == {'n': 0, 'routes': {}, 'defects': {}, 'models': {}}


def test_shortlist_enriches_candidate_from_critique_corpus(tmp_path, monkeypatch):
    """Candidate prose alone hides trigger-level differences; the critique corpus
    carries the full thesis, so a shortlisted id should gain its own conditions."""
    corpus = tmp_path / 'critique.jsonl'
    _write_log(corpus, [{'thesis': {
        'rationale': 'momentum donchian breakout daily',
        'entry_condition': 'break 20-bar high',
        'filter_condition': 'atr above median',
        'exit_condition': 'exit on channel cross'}}])
    monkeypatch.setattr(jc, 'JEV_CRITIQUE_LOG', str(corpus))
    monkeypatch.setattr(jc, 'JEV_CANDIDATE_ENRICH', True)
    monkeypatch.setattr(jc, '_ENRICH_MEMO', {'mtime': None, 'index': {}})
    monkeypatch.setattr(jc, '_recent',
                        lambda instrument, limit: [('a', 'momentum donchian breakout daily')])
    monkeypatch.setattr(jc, '_book', lambda instrument, limit=40: [])
    got = jc.shortlist({'instrument': 'EUR_USD',
                        'rationale': 'momentum donchian breakout'}, k=2)
    assert 'entry/filter/exit' in got['a']
    assert 'break 20-bar high' in got['a']


def test_enrich_disabled_keeps_candidates_plain(tmp_path, monkeypatch):
    monkeypatch.setattr(jc, 'JEV_CANDIDATE_ENRICH', False)
    assert jc._enrich_index() == {}


def test_jev_recent_stats_rolls_up_thesis_heads(tmp_path, monkeypatch):
    """generator_broken vs one-fallback-head-broken is undecidable without this."""
    import meta_review as mr
    log = tmp_path / 't.jsonl'
    _write_log(log, [
        {'route': 'block', 'thesis_model': 'ninerouter:thesis'},
        {'route': 'proceed', 'thesis_model': 'alibaba:qwen3.8-flash'},
        {'route': 'block', 'thesis_model': 'ninerouter:thesis'},
    ])
    monkeypatch.setenv('JEV_TRIAGE_LOG', str(log))
    stats = mr._jev_recent_stats(200)
    assert stats['models'] == {'ninerouter:thesis': 2, 'alibaba:qwen3.8-flash': 1}


def test_triage_log_records_thesis_model(tmp_path, monkeypatch):
    log = tmp_path / 't.jsonl'
    monkeypatch.setattr(ar, 'JEV_TRIAGE_LOG', str(log))
    ar._record_jev_triage({'rationale': 'x', '_served_by': 'ninerouter:thesis'},
                          'EUR_USD', {'available': True, 'noul': {}, 'closest': None},
                          'proceed', None, None)
    row = json.loads(log.read_text().strip())
    assert row['thesis_model'] == 'ninerouter:thesis'


def test_record_jev_outcome_links_decision_to_strategy(tmp_path, monkeypatch):
    log = tmp_path / 'outcomes.jsonl'
    monkeypatch.setattr(ar, '_JEV_OUTCOME_LOG', str(log))
    ar._record_jev_outcome('s_1', 'EUR_USD',
                           {'rationale': 'x', 'timeframe': 'D'}, 'block', 0.81,
                           'match_1', True, {'dominant_defect': 'fidelity'})
    row = json.loads(log.read_text().strip())
    assert row['strategy_id'] == 's_1' and row['regenerated'] is True
    assert row['route'] == 'block' and row['dominant_defect'] == 'fidelity'
    assert row['thesis_sha']


def test_record_jev_outcome_is_fail_soft(monkeypatch):
    monkeypatch.setattr(ar, '_JEV_OUTCOME_LOG', '/nonexistent-dir/out.jsonl')
    ar._record_jev_outcome('s', 'EUR_USD', {}, None, None, None, False, None)


def test_defect_repair_wanted_bands():
    assert ar._defect_repair_wanted(
        {'dominant_defect': 'fidelity', 'dominant_probability': 0.81}, None) == ('fidelity', 0.81)
    assert ar._defect_repair_wanted(
        {'dominant_defect': 'regime', 'dominant_probability': 0.75}, None) == ('regime', 0.75)
    # not repairable / too weak / duplicate already handled by novelty regen
    assert ar._defect_repair_wanted(
        {'dominant_defect': 'density', 'dominant_probability': 0.99}, None) == ('density', 0.99)
    assert ar._defect_repair_wanted(
        {'dominant_defect': 'NONE', 'dominant_probability': 0.99}, None) is None
    assert ar._defect_repair_wanted(
        {'dominant_defect': 'fidelity', 'dominant_probability': 0.5}, None) is None
    assert ar._defect_repair_wanted(
        {'dominant_defect': 'fidelity', 'dominant_probability': 0.9}, 'block') is None
    assert ar._defect_repair_wanted({'available': False}, None) is None


def test_dominant_probability_parsed_from_choice():
    def _call(payload):
        return {'answers': {
            'filter_independent': {'type': 'noul', 'noul': 0.8},
            'exit_tied_to_entry': {'type': 'noul', 'noul': 0.2},
            'direction_stated': {'type': 'noul', 'noul': 0.9},
            'density_risk': {'type': 'noul', 'noul': 0.1},
            'dominant_defect': {'type': 'choice', 'choice': 'fidelity',
                                'probabilities': {'fidelity': 0.72, 'regime': 0.1,
                                                  'mechanism': 0.05, 'density': 0.05,
                                                  'NONE': 0.05, 'OTHER': 0.03}}}}
    out = jev_client.judge_thesis_defects('x', _call=_call)
    assert out['dominant_defect'] == 'fidelity'
    assert out['dominant_probability'] == 0.72


def test_outcome_log_records_defect_repair(tmp_path, monkeypatch):
    log = tmp_path / 'out.jsonl'
    monkeypatch.setattr(ar, '_JEV_OUTCOME_LOG', str(log))
    ar._record_jev_outcome('s', 'EUR_USD', {'rationale': 'x'}, 'review', 0.6,
                           None, False, {'dominant_defect': 'fidelity'},
                           defect_repaired=True)
    row = json.loads(log.read_text().strip())
    assert row['defect_repaired'] is True


def test_defect_strengths_resolves_polarity():
    st = jev_client.defect_strengths({
        'filter_independent': 0.82, 'exit_tied_to_entry': 0.75,
        'direction_stated': 0.86, 'density_risk': 0.24, 'mechanism_named': 0.2})
    assert round(st['fidelity'], 2) == 0.75      # exit tied to entry = bad
    assert round(st['regime'], 2) == 0.18        # 1 - independent filter = bad
    assert round(st['density'], 2) == 0.24
    assert round(st['mechanism'], 2) == 0.80     # 1 - mechanism named = bad


def test_mechanism_strength_none_without_new_noul():
    st = jev_client.defect_strengths({'filter_independent': 0.8})
    assert st['mechanism'] is None


def test_defect_repair_prefers_strengths_over_choice_confidence():
    defects = {'dominant_defect': 'regime', 'dominant_probability': 0.4,
               'strengths': {'fidelity': 0.75, 'regime': 0.18, 'density': 0.2,
                             'mechanism': None}}
    assert ar._defect_repair_wanted(defects, None) == ('fidelity', 0.75)
    weak = {'dominant_defect': 'NONE', 'dominant_probability': 0.9,
            'strengths': {'fidelity': 0.3, 'regime': 0.1, 'density': 0.05,
                          'mechanism': None}}
    assert ar._defect_repair_wanted(weak, None) is None


def test_every_repairable_defect_has_a_brief():
    for d in ar.JEV_DEFECT_REPAIRABLE:
        assert d in ar._DEFECT_REPAIR_BRIEF, d


def test_per_type_repair_bars():
    # measured saturation: mechanism ~always high, density ~always low
    assert ar._defect_repair_wanted({'strengths': {'mechanism': 0.75}}, None) is None
    assert ar._defect_repair_wanted({'strengths': {'mechanism': 0.93}}, None) == ('mechanism', 0.93)
    assert ar._defect_repair_wanted({'strengths': {'density': 0.45}}, None) == ('density', 0.45)
    assert ar._defect_repair_wanted({'strengths': {'density': 0.3}}, None) is None
    # highest above-its-bar wins, not highest raw
    assert ar._defect_repair_wanted(
        {'strengths': {'mechanism': 0.95, 'fidelity': 0.80}}, None) == ('mechanism', 0.95)
    # explicit override still applies to every type (tests/callers)
    assert ar._defect_repair_wanted({'strengths': {'density': 0.3}}, None, 0.2) == ('density', 0.3)
