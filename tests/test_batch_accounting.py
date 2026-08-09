"""Every batch outcome bucket must reach the human-facing report.

Three bugs on 2026-08-09 shared one shape: the code was correct and the surface a
human reads was stale.

  * forced slots logged as `constraint[N]` — the label was recomputed from the
    iteration instead of taken from the schedule that ran;
  * the cTrader book warned about FIX_BROKER_BALANCE, a variable retired at the
    2026-07-27 venue cutover;
  * `fingerprint_rejected` and `guarded` were added to AutoResearcher.run and
    never threaded into notify_research_complete, so a 20-iteration batch
    reported as 18.

Each was found by a human squinting at output, which does not scale. These tests
make the omission mechanical: add a bucket to BATCH_OUTCOME_KEYS without
rendering it and the suite goes red.
"""
import inspect
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

import auto_research as ar
import telegram_bot as tb


class TestBucketsReachTheReport:
    def test_every_bucket_is_a_parameter_of_the_report(self):
        """The report must be ABLE to receive each bucket."""
        params = inspect.signature(tb.notify_research_complete).parameters
        missing = [k for k in ar.BATCH_OUTCOME_KEYS if k not in params]
        assert not missing, (
            f'notify_research_complete cannot receive {missing}; '
            f'add the parameter and render it')

    def test_every_bucket_is_actually_passed_at_the_call_site(self):
        """...and must actually BE given it. A default of 0 silently renders a
        real count as zero, which is worse than not printing it at all."""
        src = inspect.getsource(ar.AutoResearcher.run)
        call = src[src.index('notify_research_complete('):]
        call = call[:call.index(')\n')]
        missing = [k for k in ar.BATCH_OUTCOME_KEYS
                   if f"{k}=" not in call and f"results['{k}']" not in call]
        assert not missing, f'call site does not pass {missing}'

    def test_every_bucket_is_rendered_in_the_console_summary(self):
        src = inspect.getsource(ar.AutoResearcher.run)
        for k in ar.BATCH_OUTCOME_KEYS:
            assert f"results['{k}']" in src, f'console summary never reads {k}'

    def test_every_bucket_appears_in_the_rendered_message(self, monkeypatch):
        out = {}
        monkeypatch.setattr(tb, 'notify_html', lambda b: out.setdefault('b', b) or True)
        counts = {k: i + 1 for i, k in enumerate(ar.BATCH_OUTCOME_KEYS)}
        counts['passed'] = ['sid_a']
        tb.notify_research_complete(iterations=sum(
            len(v) if isinstance(v, list) else v for v in counts.values()),
            duration=1.0, **counts)
        body = out['b']
        for k, v in counts.items():
            n = len(v) if isinstance(v, list) else v
            assert str(n) in body, f'{k}={n} not visible in the report'
        assert 'unaccounted' not in body


class TestReconciliation:
    def _results(self, **kw):
        r = {'iterations': 20, 'passed': [], 'failed': ['a'] * 14, 'errors': 1,
             'critiqued_out': 3, 'fingerprint_rejected': 1, 'guarded': 1}
        r.update(kw)
        return r

    def test_counts_handles_list_and_int_buckets(self):
        c = ar.batch_counts(self._results())
        assert c['failed'] == 14 and c['passed'] == 0 and c['guarded'] == 1
        assert set(c) == set(ar.BATCH_OUTCOME_KEYS)

    def test_the_real_batch_reconciles(self):
        # forever_20260809_131826: 0+14+1+3+1+1 = 20
        assert ar.batch_unaccounted(self._results()) == 0

    def test_a_dropped_bucket_is_detected(self):
        r = self._results()
        r.pop('guarded')
        assert ar.batch_unaccounted(r) == 1

    def test_early_stop_shows_as_unaccounted_not_as_zero(self):
        # target-reached stops before the batch ends — a real, benign mismatch
        assert ar.batch_unaccounted(self._results(iterations=31)) == 11

    def test_missing_keys_do_not_raise(self):
        assert ar.batch_unaccounted({'iterations': 5}) == 5
        assert ar.batch_counts({}) == {k: 0 for k in ar.BATCH_OUTCOME_KEYS}
