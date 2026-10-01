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
    got = jc.shortlist({'instrument': 'EUR_USD',
                        'rationale': 'donchian channel momentum breakout'}, k=2)
    assert list(got) == ['a']


def test_record_jev_triage_writes_shadow_row(tmp_path, monkeypatch):
    log = tmp_path / 'jev.jsonl'
    monkeypatch.setattr(ar, 'JEV_TRIAGE_LOG', str(log))
    ar._record_jev_triage({'rationale': 'x'}, 'EUR_USD',
                          {'available': True, 'noul': {'a': 0.9}, 'closest': 'a',
                           'error': None, 'closest_probability': 0.9},
                          'block', 'a', 0.9)
    row = json.loads(log.read_text().strip())
    assert row['route'] == 'block' and row['match_id'] == 'a'


def test_judge_failure_modes_returns_four_independent_probs():
    def _call(payload):
        qs = payload['questions']
        assert list(qs) == list(jev_client.FAILURE_MODES)
        return {'answers': {q: {'type': 'noul', 'noul': p}
                            for q, p in zip(qs, (0.05, 0.90, 0.02, 0.03))}}
    out = jev_client.judge_failure_modes({'final_status': 'FAIL: sparse trades'},
                                         _call=_call)
    assert out['available'] is True
    assert out['probs']['never_fires'] == 0.90
    assert out['combined'] == 'never_fires'


def test_judge_failure_modes_fails_open():
    out = jev_client.judge_failure_modes('x', _call=lambda p: (_ for _ in ()).throw(ValueError('bad')))
    assert out['available'] is False and out['combined'] is None


def test_meta_review_shadow_writes_rows_and_changes_nothing(tmp_path, monkeypatch):
    import meta_review as mr
    log = tmp_path / 'cat.jsonl'
    monkeypatch.setattr(mr, 'JEV_CATEGORY_LOG', str(log))
    monkeypatch.setattr(jev_client, 'judge_failure_modes',
                        lambda ctx: {'available': True, 'probs': {'never_fires': 0.9},
                                     'combined': 'never_fires', 'error': None})
    n = mr.record_jev_failure_modes([
        {'strategy_id': 's1', 'final_status': 'FAIL: sparse trades'},
        {'strategy_id': 's2', 'final_status': 'passed'}])
    assert n == 1
    row = json.loads(log.read_text().strip())
    assert row['strategy_id'] == 's1' and row['combined'] == 'never_fires'
    assert row['gate_bucket'] == 'sparse'


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
