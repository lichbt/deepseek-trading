"""Meta-review model chain: provider routing, fall-through, thinking-off.

Regression guard for 2026-09-06: the chain was hardcoded to a byteplus account
whose subscription had expired, and `if OPENROUTER_API_KEY` gated providers that
have nothing to do with OpenRouter — so an alibaba-only chain was skipped
without a single request being sent.
"""
import pytest
import meta_review as mr


class _Resp:
    def __init__(self, status=200, content='- a\n- b\n- c', model='served-x'):
        self.status_code = status
        self._content = content
        self._model = model

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f'{self.status_code} Client Error')

    def json(self):
        return {'model': self._model,
                'choices': [{'message': {'content': self._content},
                             'finish_reason': 'stop'}]}


@pytest.fixture(autouse=True)
def _creds(monkeypatch):
    monkeypatch.setattr(mr, 'ALIBABA_BASE', 'https://ali/v1')
    monkeypatch.setattr(mr, 'ALIBABA_KEY', 'ali-key')
    monkeypatch.setattr(mr, 'BYTEPLUS_BASE', 'https://bp/v1')
    monkeypatch.setattr(mr, 'BYTEPLUS_KEY', 'bp-key')
    monkeypatch.setattr(mr, 'OPENROUTER_API_KEY', '')


def _capture(monkeypatch, responder):
    calls = []

    def fake(base, headers, payload, timeout, stage):
        calls.append({'base': base, 'model': payload['model'], 'stage': stage,
                      'thinking': payload.get('enable_thinking', 'unset'),
                      'key': headers['Authorization']})
        return responder(payload['model'])

    monkeypatch.setattr(mr, '_post_chat_logged', fake)
    return calls


def test_route_model_picks_endpoint_per_prefix():
    assert mr._route_model('alibaba:qwen3.8-max') == (mr.ALIBABA_BASE, mr.ALIBABA_KEY, 'qwen3.8-max')
    assert mr._route_model('byteplus:deepseek-v4-pro') == (mr.BYTEPLUS_BASE, mr.BYTEPLUS_KEY, 'deepseek-v4-pro')
    # unprefixed still falls through to OpenRouter
    assert mr._route_model('some/model')[2] == 'some/model'


def test_alibaba_gets_thinking_off_and_byteplus_does_not(monkeypatch):
    calls = _capture(monkeypatch, lambda m: _Resp())
    mr.call_llm('sys', 'usr', models=['alibaba:qwen3.8-max'])
    assert calls[0]['thinking'] is False
    calls.clear()
    mr.call_llm('sys', 'usr', models=['byteplus:deepseek-v4-pro'])
    assert calls[0]['thinking'] == 'unset'


def test_thinking_stays_on_when_env_asks(monkeypatch):
    monkeypatch.setenv('ALIBABA_THINKING', '1')
    calls = _capture(monkeypatch, lambda m: _Resp())
    mr.call_llm('sys', 'usr', models=['alibaba:qwen3.8-max'])
    assert calls[0]['thinking'] == 'unset'


def test_chain_falls_through_alibaba_alibaba_byteplus(monkeypatch):
    """The exact production failure: primary 400s, chain must keep going."""
    def responder(model):
        if model == 'qwen3.8-max':
            raise RuntimeError('boom')          # network-level failure
        if model == 'qwen3.7-plus':
            return _Resp(status=400)            # InvalidSubscription shape
        return _Resp(content='- x\n- y')
    calls = _capture(monkeypatch, responder)
    out = mr.call_llm('sys', 'usr',
                      models=['alibaba:qwen3.8-max', 'alibaba:qwen3.7-plus',
                              'byteplus:deepseek-v4-pro'])
    assert [c['model'] for c in calls] == ['qwen3.8-max', 'qwen3.7-plus', 'deepseek-v4-pro']
    assert [c['base'] for c in calls] == ['https://ali/v1', 'https://ali/v1', 'https://bp/v1']
    assert out == '- x\n- y'


def test_whole_chain_dead_returns_none_not_crash(monkeypatch):
    _capture(monkeypatch, lambda m: _Resp(status=400))
    assert mr.call_llm('sys', 'usr', models=['alibaba:a', 'byteplus:b']) is None


def test_empty_content_is_not_accepted(monkeypatch):
    """A thinking model that burns max_tokens returns '' — must fall through."""
    responder = lambda m: _Resp(content='') if m == 'a' else _Resp(content='- ok\n- ok2')
    calls = _capture(monkeypatch, responder)
    assert mr.call_llm('sys', 'usr', models=['alibaba:a', 'alibaba:b']) == '- ok\n- ok2'
    assert len(calls) == 2


def test_alibaba_only_chain_runs_without_openrouter_key(monkeypatch):
    """The gate bug: no OPENROUTER_API_KEY must not skip an alibaba chain."""
    assert mr.OPENROUTER_API_KEY == ''
    calls = _capture(monkeypatch, lambda m: _Resp())
    assert mr.call_llm('sys', 'usr', models=['alibaba:qwen3.8-max']) is not None
    assert len(calls) == 1


def test_no_credentials_at_all_skips(monkeypatch):
    monkeypatch.setattr(mr, 'ALIBABA_KEY', '')
    monkeypatch.setattr(mr, 'BYTEPLUS_KEY', '')
    assert mr._llm_available() is False
    assert mr.call_llm('sys', 'usr', models=['alibaba:x']) is None


def test_role_and_meta_chains_are_alibaba_first_byteplus_last():
    for chain in (mr.META_MODELS, mr.ROLE_MODELS):
        assert chain[0].startswith('alibaba:'), chain
        assert chain[-1].startswith('byteplus:'), chain


def test_stage_is_tagged_for_usage_accounting(monkeypatch):
    calls = _capture(monkeypatch, lambda m: _Resp())
    mr.call_llm('sys', 'usr', models=['alibaba:x'])
    assert calls[0]['stage'] == 'meta_review'
    calls.clear()
    mr.call_llm('sys', 'usr', models=['alibaba:x'], stage='role_proposal')
    assert calls[0]['stage'] == 'role_proposal'
