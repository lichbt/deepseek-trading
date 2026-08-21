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
