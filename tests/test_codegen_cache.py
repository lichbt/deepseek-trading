"""The code-gen prompt must stay split so the provider prefix cache can hold it.

Measured 2026-08-22: every placeholder sat in the first 2% of codegen.md, so the
cacheable prefix was ~44 tokens and codegen billed cached=0 on a model that
caches fine (the thesis path, laid out the other way round, cached 69%). These
tests pin the layout that fixes it — the failure mode is silent and shows up
only as a bigger bill.
"""
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
import auto_research as ar

FIELDS = dict(instrument='EUR_USD', timeframe='D', family='momentum',
              hypothesis='h', entry='e', filter='f', exit='x',
              param_hints='{"lookback": [10]}')


def test_static_half_carries_no_placeholders():
    """One stray {timeframe} in the static half would vary it per call."""
    _spec, static = ar._split_codegen_template()
    stray = re.findall(r'(?<!\{)\{(\w+)\}(?!\})', static)
    assert stray == [], f'static half must be constant, found {stray}'


def test_static_half_is_the_bulk_of_the_prompt():
    """If the split ever inverts, caching is pointless — guard the ratio."""
    spec, static = ar._split_codegen_template()
    assert len(static) > 8000, 'static half suspiciously small — did the marker move?'
    assert len(static) > 10 * len(spec)


def test_system_prompt_is_byte_identical_across_strategies():
    spec_tmpl, static = ar._split_codegen_template()
    a = spec_tmpl.format(**FIELDS)
    b = spec_tmpl.format(**dict(FIELDS, instrument='XAU_USD', hypothesis='different'))
    assert a != b, 'the spec half must still vary per strategy'
    # The system message is what gets cached; it must not move.
    assert static == ar._split_codegen_template()[1]


def test_spec_half_still_fills_every_placeholder():
    spec_tmpl, _static = ar._split_codegen_template()
    out = spec_tmpl.format(**FIELDS)
    assert 'EUR_USD' in out and 'momentum' in out
    assert not re.search(r'(?<!\{)\{(\w+)\}(?!\})', out), 'unfilled placeholder left in spec'


def test_missing_marker_falls_back_to_old_behaviour(monkeypatch):
    """A template without the marker must still work, unsplit."""
    monkeypatch.setattr(ar, '_get_codegen_template', lambda: 'no marker here {instrument}')
    spec, static = ar._split_codegen_template()
    assert spec == 'no marker here {instrument}'
    assert static == ''


def test_codegen_sends_the_static_half_as_system(monkeypatch):
    """The whole point: static text must land in the SYSTEM message."""
    seen = {}

    class FakeResp:
        status_code = 200
        text = '{}'

        def json(self):
            return {'choices': [{'message': {'content':
                    '```python\ndef generate_signals(df):\n    return df\n```\n'
                    '```json\n{"param_grid": {"a": [1]}, "archetype": "standard"}\n```'},
                    'finish_reason': 'stop'}]}

    def fake_post(url, headers=None, json=None, timeout=None):
        seen['payload'] = json
        return FakeResp()

    monkeypatch.setattr(ar.requests, 'post', fake_post)
    monkeypatch.setattr(ar, '_route_model', lambda m, k=None: ('https://x/v1', 'k', m, True))
    _spec, static = ar._split_codegen_template()
    ar.generate_code_via_openrouter('SPEC HERE',
                                    system_prompt=ar._CODE_SYSTEM_PROMPT + '\n\n' + static)

    msgs = {m['role']: m['content'] for m in seen['payload']['messages']}
    assert 'SINGLE TIMEFRAME ONLY' in msgs['system'], 'static rules not in system message'
    assert 'SINGLE TIMEFRAME ONLY' not in msgs['user']
    assert msgs['user'] == 'SPEC HERE'
