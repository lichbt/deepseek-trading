"""Tests for the LLM usage accounting added to auto_research.

Contract: every chat-completions POST goes through _post_chat, which appends one
JSON line per call carrying the BILLED counters (not the char-based estimate),
the stage that spent them, and the model the gateway actually served. Recording
must never raise and must never swallow the caller's own error.
"""
import json
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
import auto_research as ar


class FakeResp:
    def __init__(self, body, status=200, text=None):
        self._body = body
        self.status_code = status
        self.text = text if text is not None else json.dumps(body)

    def json(self):
        if self._body is None:
            raise ValueError('not json')
        return self._body

    def raise_for_status(self):
        if self.status_code >= 400:
            raise ar.requests.exceptions.HTTPError(f'HTTP {self.status_code}')


def _body(**usage):
    return {
        'model': 'deepseek-v4-pro-0813',
        'choices': [{'message': {'content': '{}'}, 'finish_reason': 'stop'}],
        'usage': usage or {'prompt_tokens': 11, 'completion_tokens': 22, 'total_tokens': 33},
    }


@pytest.fixture
def sink(tmp_path, monkeypatch):
    path = tmp_path / 'usage.jsonl'
    monkeypatch.setattr(ar, '_USAGE_LOG_PATH', str(path))

    def read():
        return [json.loads(line) for line in path.read_text().splitlines()]

    read.path = path
    return read


def _post_ok(monkeypatch, resp):
    monkeypatch.setattr(ar.requests, 'post', lambda *a, **k: resp)


class TestRecording:
    def test_billed_counters_are_recorded(self, sink, monkeypatch):
        _post_ok(monkeypatch, FakeResp(_body(
            prompt_tokens=1200, completion_tokens=8400, total_tokens=9600,
            prompt_tokens_details={'cached_tokens': 1024},
            completion_tokens_details={'reasoning_tokens': 8000},
        )))
        ar._post_chat('https://api.example/v1', {}, {
            'model': 'alibaba-thing', 'max_tokens': 12000,
            'messages': [{'role': 'system', 'content': 'abc'},
                         {'role': 'user', 'content': 'de'}],
        }, 60, stage='codegen')

        rec, = sink()
        assert rec['stage'] == 'codegen'
        assert rec['prompt_tokens'] == 1200
        assert rec['completion_tokens'] == 8400
        # Cached input and reasoning output are the two lines that move under a
        # caching or thinking change — they must not be folded into the totals.
        assert rec['cached_tokens'] == 1024
        assert rec['reasoning_tokens'] == 8000
        assert rec['prompt_chars'] == 5
        assert rec['status'] == 200
        assert rec['finish_reason'] == 'stop'

    def test_served_model_is_recorded_not_the_requested_one(self, sink, monkeypatch):
        """A chain that falls through bills under a model we did not request."""
        _post_ok(monkeypatch, FakeResp(_body()))
        ar._post_chat('https://api.example/v1', {},
                      {'model': 'asked-for-this', 'messages': []}, 60, stage='thesis_batch')
        rec, = sink()
        assert rec['requested'] == 'asked-for-this'
        assert rec['served'] == 'deepseek-v4-pro-0813'

    def test_gateway_nested_body_is_unwrapped(self, sink, monkeypatch):
        _post_ok(monkeypatch, FakeResp({'data': _body()}))
        ar._post_chat('https://api.example/v1', {}, {'model': 'm', 'messages': []}, 60)
        rec, = sink()
        assert rec['served'] == 'deepseek-v4-pro-0813'
        assert rec['completion_tokens'] == 22
        assert rec['stage'] == 'other'      # untagged paths must be visible, not guessed

    def test_non_json_body_records_without_tokens(self, sink, monkeypatch):
        _post_ok(monkeypatch, FakeResp(None, text='data: {"choices":[]}'))
        ar._post_chat('https://api.example/v1', {}, {'model': 'm', 'messages': []}, 60)
        rec, = sink()
        assert rec['status'] == 200
        assert 'prompt_tokens' not in rec

    def test_failed_call_is_still_counted(self, sink, monkeypatch):
        """A burned-but-failed call is pure loss — the first thing a cut targets."""
        def boom(*a, **k):
            raise ar.requests.exceptions.Timeout('too slow')
        monkeypatch.setattr(ar.requests, 'post', boom)

        with pytest.raises(ar.requests.exceptions.Timeout):
            ar._post_chat('https://api.example/v1', {}, {'model': 'm', 'messages': []},
                          60, stage='thesis_batch')
        rec, = sink()
        assert rec['stage'] == 'thesis_batch'
        assert 'Timeout' in rec['error']
        assert 'status' not in rec

    def test_http_error_status_is_recorded(self, sink, monkeypatch):
        _post_ok(monkeypatch, FakeResp({'error': {'message': 'nope'}}, status=400))
        ar._post_chat('https://api.example/v1', {}, {'model': 'm', 'messages': []},
                      60, stage='codegen', note='retry-no-reasoning')
        rec, = sink()
        assert rec['status'] == 400
        assert rec['note'] == 'retry-no-reasoning'

    def test_accounting_never_breaks_generation(self, tmp_path, monkeypatch):
        """An unwritable sink must not take the research loop down with it."""
        monkeypatch.setattr(ar, '_USAGE_LOG_PATH', str(tmp_path / 'nope' / 'x.jsonl'))
        monkeypatch.setattr(ar.Path, 'mkdir',
                            lambda *a, **k: (_ for _ in ()).throw(OSError('read-only fs')))
        _post_ok(monkeypatch, FakeResp(_body()))
        resp = ar._post_chat('https://api.example/v1', {}, {'model': 'm', 'messages': []}, 60)
        assert resp.status_code == 200


class TestStageThreading:
    def test_call_openrouter_threads_its_stage_to_the_record(self, sink, monkeypatch):
        monkeypatch.setattr(ar, '_route_model',
                            lambda m, k=None: ('https://api.example/v1', 'key', m, True))
        _post_ok(monkeypatch, FakeResp(_body()))
        res = ar.call_openrouter('sys', 'user', model='alibaba:qwen3.7-plus',
                                 api_key='key', stage='critique_thesis')
        assert res['success']
        rec, = sink()
        assert rec['stage'] == 'critique_thesis'
        assert rec['requested'] == 'alibaba:qwen3.7-plus'

    def test_every_llm_call_site_carries_a_stage(self):
        """Guard against a new call site silently landing in the 'other' bucket."""
        src = open(os.path.join(os.path.dirname(__file__), '..', 'auto_research.py')).read()
        # _post_chat is the only place allowed to call the endpoint directly.
        assert src.count('requests.post(') == 1
        for stage in ('thesis_batch', 'thesis_single', 'thesis_repair',
                      'critique_thesis', 'critique_fidelity', 'codegen'):
            assert f"stage='{stage}'" in src, f'{stage} lost its tag'


class TestSuiteDoesNotPolluteTheRealLog:
    def test_mocked_calls_never_reach_the_production_sink(self, tmp_path, monkeypatch):
        """The suite mocks the POST layer widely; none of it may be billed.

        Regression: the first live run of this accounting wrote 110 fake records
        (prompt_chars=2, latency 0.0, base opencode.example) into the real
        usage.jsonl, which a cost report would have averaged in as real calls.
        """
        real = tmp_path / 'production.jsonl'
        monkeypatch.setattr(ar, '_USAGE_DEFAULT_LOG_PATH', str(real))
        monkeypatch.setattr(ar, '_USAGE_LOG_PATH', str(real))
        _post_ok(monkeypatch, FakeResp(_body()))

        ar._post_chat('https://api.example/v1', {}, {'model': 'm', 'messages': []}, 60)

        assert not real.exists(), 'a mocked call wrote to the default sink'

    def test_an_explicitly_pointed_sink_still_records(self, sink, monkeypatch):
        """...but a test that names its own path must still get its records."""
        _post_ok(monkeypatch, FakeResp(_body()))
        ar._post_chat('https://api.example/v1', {}, {'model': 'm', 'messages': []}, 60)
        assert len(sink()) == 1
