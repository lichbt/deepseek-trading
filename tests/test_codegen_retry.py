"""Code-gen transient-retry (replaces the dropped paid deepseek-chat backstop).

Transient failures (429 / network blip) retry the whole free list after a backoff;
model-specific failures (empty/parse) do not, so a bad generation doesn't waste time.
"""
import auto_research as A


def test_is_transient_classification():
    assert A._is_transient_err('HTTP 429: too many requests')
    assert A._is_transient_err('Request error: Failed to resolve openrouter.ai')
    assert A._is_transient_err('Request error: Read timed out')
    assert A._is_transient_err('Parse error: No ```python block')
    assert not A._is_transient_err('HTTP 404: model unavailable')
    assert A._is_transient_err('Empty content (finish_reason=length)')


class _Resp:
    def __init__(self, status, payload=None, text=''):
        self.status_code = status; self._p = payload or {}; self.text = text
    def json(self): return self._p


def test_retries_full_list_on_429(monkeypatch):
    n = {'c': 0}
    def fake_post(url, **kw):
        n['c'] += 1
        return _Resp(429, text='rate limited')
    monkeypatch.setattr(A.requests, 'post', fake_post)
    monkeypatch.setattr(A.time, 'sleep', lambda s: None)   # no real backoff
    r = A.generate_code_via_openrouter('prompt')
    assert r['success'] is False
    # 3 backoff passes (0,5,15) over the whole free list
    assert n['c'] == 3 * len(A.CODE_FALLBACK_MODELS)


def test_retries_full_list_on_empty_content(monkeypatch):
    n = {'c': 0}
    def fake_post(url, **kw):
        n['c'] += 1
        return _Resp(200, {'choices': [{'message': {'content': ''}, 'finish_reason': 'length'}]})
    monkeypatch.setattr(A.requests, 'post', fake_post)
    monkeypatch.setattr(A.time, 'sleep', lambda s: None)
    r = A.generate_code_via_openrouter('prompt')
    assert r['success'] is False
    assert n['c'] == 3 * len(A.CODE_FALLBACK_MODELS)


def test_no_retry_on_nontransient_http(monkeypatch):
    n = {'c': 0}
    def fake_post(url, **kw):
        n['c'] += 1
        return _Resp(404, text='model unavailable')
    monkeypatch.setattr(A.requests, 'post', fake_post)
    monkeypatch.setattr(A.time, 'sleep', lambda s: None)
    r = A.generate_code_via_openrouter('prompt')
    assert r['success'] is False
    assert n['c'] == len(A.CODE_FALLBACK_MODELS)


def test_retries_full_list_on_parse_error(monkeypatch):
    n = {'c': 0}
    def fake_post(url, **kw):
        n['c'] += 1
        return _Resp(200, {'choices': [{'message': {'content': '```json\n{"param_grid": {}}\n```'}, 'finish_reason': 'stop'}]})
    monkeypatch.setattr(A.requests, 'post', fake_post)
    monkeypatch.setattr(A.time, 'sleep', lambda s: None)
    r = A.generate_code_via_openrouter('prompt')
    assert r['success'] is False
    assert n['c'] == 3 * len(A.CODE_FALLBACK_MODELS)


# ── Opaque-400 reasoning strip (2026-07-24) ─────────────────────────────────
# opencode:glm-5.2 400s on every request carrying `reasoning`, with a body that
# never names the field. The strip-retry used to require the word "reasoning"
# in the response, so it never fired and the chain lead failed 151x/day.

_OPAQUE_400 = '{"error":{"message":"Error from provider (Console Go): Upstream request failed"}}'
_GOOD = {'choices': [{'message': {'content':
    '```python\ndef generate_signals(df, **p):\n    return df\n```\n'
    '```json\n{"param_grid": {"n": [7, 14]}}\n```'}, 'finish_reason': 'stop'}]}


def test_opaque_400_strips_reasoning_and_retries(monkeypatch):
    """A 400 that never says 'reasoning' still triggers the strip-retry."""
    monkeypatch.setattr(A, '_REASONING_UNSUPPORTED', set())
    monkeypatch.setattr(A, '_REASONING_OVERRIDES', {})   # isolate from .env overrides
    # Follow the REAL chain head rather than pinning a provider. This test was
    # pinned to 'opencode:glm-5.2' and started failing the moment opencode was
    # dropped for alibaba (2026-08-20): _route_model returned an unset base/key,
    # the entry was skipped before any HTTP call, and the retry never ran. The
    # strip-retry under test is provider-independent; the pin was not.
    # NOTE: _REASONING_PROVIDERS is ('opencode:', 'cline:') and BOTH providers
    # were dropped on 2026-08-20, so no live chain entry carries `reasoning`
    # and the strip-retry is currently DORMANT in production. Exercise the
    # mechanism anyway, against whatever the chain head is: it is the retry
    # logic under test, and it must still work if a reasoning provider returns.
    model = A.CODE_FALLBACK_MODELS[0]
    monkeypatch.setattr(A, 'CODE_FALLBACK_MODELS', [model])
    monkeypatch.setattr(A, '_REASONING_PROVIDERS', (model.split(':')[0] + ':',))
    seen = []
    def fake_post(url, **kw):
        seen.append('reasoning' in kw['json'])
        return _Resp(200, _GOOD) if len(seen) > 1 else _Resp(400, text=_OPAQUE_400)
    monkeypatch.setattr(A.requests, 'post', fake_post)
    monkeypatch.setattr(A.time, 'sleep', lambda s: None)
    r = A.generate_code_via_openrouter('prompt')
    assert r['success'] is True
    assert seen == [True, False]        # first call carried it, retry dropped it
    assert model in A._REASONING_UNSUPPORTED


def test_reasoning_rejection_is_remembered(monkeypatch):
    """Once marked, the model never pays the wasted round-trip again."""
    monkeypatch.setattr(A, '_REASONING_UNSUPPORTED', set())
    monkeypatch.setattr(A, '_REASONING_OVERRIDES', {})   # isolate from .env overrides
    assert A._reasoning_param('opencode:glm-5.2') == {'effort': A.REASONING_EFFORT}
    A._mark_reasoning_unsupported('opencode:glm-5.2')
    assert A._reasoning_param('opencode:glm-5.2') is None
    assert A._reasoning_param('opencode:minimax-m3') == {'effort': A.REASONING_EFFORT}


def test_per_model_reasoning_override(monkeypatch):
    """A per-model override diverges from the global effort; others keep the default."""
    monkeypatch.setattr(A, '_REASONING_UNSUPPORTED', set())
    monkeypatch.setattr(A, 'REASONING_EFFORT', 'low')
    monkeypatch.setattr(A, '_REASONING_OVERRIDES',
                        {'opencode:deepseek-v4-flash': 'none'})
    # overridden model uses its own effort
    assert A._reasoning_param('opencode:deepseek-v4-flash') == {'effort': 'none'}
    # non-overridden gateway models keep the global default
    assert A._reasoning_param('opencode:glm-5.2') == {'effort': 'low'}
    # an override to empty string omits the field entirely for that model
    monkeypatch.setattr(A, '_REASONING_OVERRIDES', {'opencode:glm-5.2': ''})
    assert A._reasoning_param('opencode:glm-5.2') is None
    # a marked-unsupported model still wins over any override
    A._mark_reasoning_unsupported('opencode:deepseek-v4-flash')
    monkeypatch.setattr(A, '_REASONING_OVERRIDES',
                        {'opencode:deepseek-v4-flash': 'none'})
    assert A._reasoning_param('opencode:deepseek-v4-flash') is None


def test_reasoning_override_parsing():
    """Comma-separated model=effort pairs parse into a dict; junk entries drop."""
    out = A._parse_reasoning_overrides(
        'opencode:deepseek-v4-flash=none, opencode:foo=low ,bad-entry,=orphan')
    assert out == {'opencode:deepseek-v4-flash': 'none',
                   'opencode:foo': 'low', '': 'orphan'}
    assert A._parse_reasoning_overrides('') == {}


def test_unrelated_400_does_not_mark_model(monkeypatch):
    """A 400 on a payload with no `reasoning` field must not blame the model."""
    monkeypatch.setattr(A, '_REASONING_UNSUPPORTED', set())
    monkeypatch.setattr(A, 'REASONING_EFFORT', '')     # field never attached
    monkeypatch.setattr(A, 'CODE_FALLBACK_MODELS', ['opencode:glm-5.2'])
    monkeypatch.setattr(A.requests, 'post', lambda url, **kw: _Resp(400, text='bad request'))
    monkeypatch.setattr(A.time, 'sleep', lambda s: None)
    r = A.generate_code_via_openrouter('prompt')
    assert r['success'] is False
    assert A._REASONING_UNSUPPORTED == set()


# ── Codegen thinking policy (2026-09-15) ────────────────────────────────────
# Codegen used to KEEP DeepSeek thinking ON, on the theory that its CoT prevented
# the signal-density retry storm. The billing data disproved it: "min 5 needed"
# density failures run at the same rate per iteration either way (0.29/0.22 OFF
# vs 0.24 ON), thinking-ON codegen cost 1.8x more per iteration ($0.0023 vs
# $0.0012), and 23% of its calls hit finish_reason=length with the CoT eating the
# entire budget. Codegen now disables thinking like every other DeepSeek leg.

def test_codegen_disables_deepseek_thinking(monkeypatch):
    """The codegen payload carries thinking:disabled and no reasoning_effort."""
    monkeypatch.setattr(A, 'DEEPSEEK_BASE', 'https://api.deepseek.com')
    monkeypatch.setattr(A, 'DEEPSEEK_KEY', 'sk-test')
    monkeypatch.setattr(A, 'CODE_FALLBACK_MODELS', ['deepseek:deepseek-flash'])
    monkeypatch.setattr(A, '_PROVIDER_HEALTH', {})     # isolate from earlier 429s
    monkeypatch.delenv('DEEPSEEK_THINKING', raising=False)
    seen = {}

    def fake_post(url, **kw):
        seen.update(kw['json'])
        return _Resp(200, _GOOD)

    monkeypatch.setattr(A.requests, 'post', fake_post)
    monkeypatch.setattr(A.time, 'sleep', lambda s: None)
    r = A.generate_code_via_openrouter('prompt')
    assert r['success'] is True
    assert seen['model'] == 'deepseek-flash'
    assert seen['thinking'] == {'type': 'disabled'}
    assert 'reasoning_effort' not in seen
    assert seen['max_tokens'] == 4000


def test_deepseek_thinking_env_forces_it_back_on(monkeypatch):
    """DEEPSEEK_THINKING=1 is the escape hatch back to CoT on every leg."""
    monkeypatch.setattr(A, '_PROVIDER_HEALTH', {})
    monkeypatch.setenv('DEEPSEEK_THINKING', '1')
    assert A._deepseek_thinking_off('deepseek:deepseek-flash') == {}
    monkeypatch.delenv('DEEPSEEK_THINKING')
    monkeypatch.setattr(A, 'DEEPSEEK_KEY', 'sk-test')
    assert A._deepseek_thinking_off('deepseek:deepseek-flash') == {
        'thinking': {'type': 'disabled'}}
    # Non-DeepSeek entries are untouched by this helper.
    assert A._deepseek_thinking_off('alibaba:deepseek-v4-flash-0731') == {}


def test_codegen_keeps_thinking_disabled_for_other_deepseek_models(monkeypatch):
    """deepseek-v4-pro (the reasoning head) gets the same toggle as flash."""
    monkeypatch.setattr(A, 'DEEPSEEK_BASE', 'https://api.deepseek.com')
    monkeypatch.setattr(A, 'DEEPSEEK_KEY', 'sk-test')
    monkeypatch.setattr(A, 'CODE_FALLBACK_MODELS', ['deepseek:deepseek-v4-pro'])
    monkeypatch.setattr(A, '_PROVIDER_HEALTH', {})
    monkeypatch.delenv('DEEPSEEK_THINKING', raising=False)
    seen = {}

    def fake_post(url, **kw):
        seen.update(kw['json'])
        return _Resp(200, _GOOD)

    monkeypatch.setattr(A.requests, 'post', fake_post)
    monkeypatch.setattr(A.time, 'sleep', lambda s: None)
    r = A.generate_code_via_openrouter('prompt')
    assert r['success'] is True
    assert seen['thinking'] == {'type': 'disabled'}
    assert 'reasoning_effort' not in seen

# ── ninerouter:codegen primary (2026-09-16) ─────────────────────────────────
# 9router's `codegen` combo serves ds/deepseek-flash. Its /chat/completions
# defaults to SSE (body `{json}data: [DONE]`), which resp.json() cannot parse —
# so the leg must ask for stream:false. The combo's DeepSeek honours the
# thinking:disabled toggle (measured: reasoning_tokens 1345 -> 0).

def test_ninerouter_codegen_payload(monkeypatch):
    monkeypatch.setattr(A, 'NINEROUTER_BASE', 'http://localhost:20128/v1')
    monkeypatch.setattr(A, 'NINEROUTER_KEY', 'sk-test')
    monkeypatch.setattr(A, 'CODE_FALLBACK_MODELS', ['ninerouter:codegen'])
    monkeypatch.setattr(A, '_PROVIDER_HEALTH', {})
    monkeypatch.delenv('DEEPSEEK_THINKING', raising=False)
    seen = {}

    def fake_post(url, **kw):
        seen.update(kw['json'])
        return _Resp(200, _GOOD)

    monkeypatch.setattr(A.requests, 'post', fake_post)
    assert A.generate_code_via_openrouter('prompt')['success'] is True
    assert seen['model'] == 'codegen'
    assert seen['stream'] is False
    assert seen['thinking'] == {'type': 'disabled'}
    assert seen['max_tokens'] == 4000

def test_ninerouter_thinking_can_be_left_on_alone(monkeypatch):
    """NINEROUTER_THINKING=1 spares the 9router leg without touching deepseek:."""
    monkeypatch.delenv('DEEPSEEK_THINKING', raising=False)
    monkeypatch.delenv('NINEROUTER_THINKING', raising=False)
    assert A._deepseek_thinking_off('ninerouter:codegen') == {
        'thinking': {'type': 'disabled'}}
    monkeypatch.setenv('NINEROUTER_THINKING', '1')
    assert A._deepseek_thinking_off('ninerouter:codegen') == {}
    assert A._deepseek_thinking_off('deepseek:deepseek-flash') == {
        'thinking': {'type': 'disabled'}}
    # The global escape still covers both legs.
    monkeypatch.setenv('DEEPSEEK_THINKING', '1')
    assert A._deepseek_thinking_off('ninerouter:codegen') == {}
    assert A._deepseek_thinking_off('deepseek:deepseek-flash') == {}
    # Unrelated providers stay untouched either way.
    assert A._deepseek_thinking_off('alibaba:deepseek-v4-flash-0731') == {}


def test_400_on_thinking_drops_it_and_retries(monkeypatch):
    """A 9router combo can fall through to a non-DeepSeek member that rejects the
    `thinking` toggle. One retry without the field, like the `reasoning` cap."""
    monkeypatch.setattr(A, 'NINEROUTER_BASE', 'http://localhost:20128/v1')
    monkeypatch.setattr(A, 'NINEROUTER_KEY', 'sk-test')
    monkeypatch.setattr(A, 'CODE_FALLBACK_MODELS', ['ninerouter:critique'])
    monkeypatch.setattr(A, '_PROVIDER_HEALTH', {})
    monkeypatch.delenv('DEEPSEEK_THINKING', raising=False)
    seen = []

    def fake_post(url, **kw):
        seen.append(dict(kw['json']))
        return _Resp(200, _GOOD) if 'thinking' not in kw['json'] else _Resp(400, text='bad request')

    monkeypatch.setattr(A.requests, 'post', fake_post)
    monkeypatch.setattr(A.time, 'sleep', lambda s: None)
    assert A.generate_code_via_openrouter('prompt')['success'] is True
    assert len(seen) == 2
    assert seen[0]['thinking'] == {'type': 'disabled'}
    assert 'thinking' not in seen[1]
