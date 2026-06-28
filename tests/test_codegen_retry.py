"""Code-gen transient-retry (replaces the dropped paid deepseek-chat backstop).

Transient failures (429 / network blip) retry the whole free list after a backoff;
model-specific failures (empty/parse) do not, so a bad generation doesn't waste time.
"""
import auto_research as A


def test_is_transient_classification():
    assert A._is_transient_err('HTTP 429: too many requests')
    assert A._is_transient_err('Request error: Failed to resolve openrouter.ai')
    assert A._is_transient_err('Request error: Read timed out')
    assert not A._is_transient_err('Parse error: No ```python block')
    assert not A._is_transient_err('HTTP 404: model unavailable')
    assert not A._is_transient_err('Empty content (finish_reason=length)')


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


def test_no_retry_on_nontransient(monkeypatch):
    n = {'c': 0}
    def fake_post(url, **kw):
        n['c'] += 1
        return _Resp(200, {'choices': [{'message': {'content': ''}, 'finish_reason': 'length'}]})
    monkeypatch.setattr(A.requests, 'post', fake_post)
    monkeypatch.setattr(A.time, 'sleep', lambda s: None)
    r = A.generate_code_via_openrouter('prompt')
    assert r['success'] is False
    assert n['c'] == len(A.CODE_FALLBACK_MODELS)   # single pass, no wasted backoff
