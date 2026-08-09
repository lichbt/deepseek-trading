"""The batch report must account for every iteration.

There are SIX outcome buckets in AutoResearcher.run; the report printed FOUR,
because fingerprint_rejected and guarded were added to the run loop and never
here. A 20-iteration batch reported as 18 and the reader had to guess. Both
omitted buckets are also the ones that are NOT failures — a struct rejection and
a deterministic guard skip are the gates working, and `guarded` exists so
`errors` can keep meaning "crash or transient API failure".
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

import telegram_bot as tb


def _render(monkeypatch, **kw):
    out = {}
    monkeypatch.setattr(tb, 'notify_html', lambda body: out.setdefault('b', body) or True)
    base = dict(iterations=20, passed=[], failed=14, errors=1,
                duration=1.0, critiqued_out=3, fingerprint_rejected=1, guarded=1)
    base.update(kw)
    tb.notify_research_complete(**base)
    return out['b']


def test_all_six_buckets_are_reported(monkeypatch):
    body = _render(monkeypatch)
    for frag in ('Passed: 0', 'Failed: 14', 'Errors: 1',
                 '3 self-critiqued', '1 struct-rejected', '1 guarded'):
        assert frag in body, frag


def test_the_real_batch_reconciles_to_its_iteration_count(monkeypatch):
    # forever_20260809_131826: 0+14+3+1+1+1 = 20
    assert 'unaccounted' not in _render(monkeypatch)


def test_a_missing_bucket_announces_itself(monkeypatch):
    body = _render(monkeypatch, fingerprint_rejected=0, guarded=0)
    assert '2 iteration(s) unaccounted' in body


def test_early_stop_is_flagged_not_hidden(monkeypatch):
    # target-reached early stop leaves iterations > accounted; say so
    body = _render(monkeypatch, iterations=31)
    assert 'unaccounted' in body
