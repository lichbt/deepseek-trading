"""ARCHITECTURE.md's gate table must match validator.py.

The gates were hand-written into four documents and all four drifted.
ARCHITECTURE.md said IS > 0.5 / WF > 1.0 combined / min window > 0.3 / decay < 30%
while the code ran 0.3 / 0.5 / 0.0 / 0.6, and omitted MIN_HO_SCORE and
MIN_HO_ENTRIES entirely — five stated numbers, five wrong. A 2026-07-29 decision
had already recorded that the documented gates were never accurate to the code;
the decision log got corrected and the documents did not.

Copying them correctly once would only restart that clock, so the table is
generated and this test is what stops it going stale again.
"""
import re
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
DOC = REPO / 'ARCHITECTURE.md'
SYNC = REPO / 'scripts' / 'sync_gate_docs.py'


def test_the_generated_gate_table_is_current():
    r = subprocess.run([sys.executable, str(SYNC), '--check'],
                       capture_output=True, text=True, cwd=str(REPO))
    assert r.returncode == 0, (
        f'{r.stdout}{r.stderr}\n'
        'ARCHITECTURE.md no longer matches validator.py. Run '
        'scripts/sync_gate_docs.py rather than editing the table by hand.')


def test_every_gate_constant_still_exists_in_validator():
    # a renamed constant would silently vanish from the table instead of failing
    import scripts.sync_gate_docs as S
    import validator as V
    missing = [n for n, _, _ in S.GATES if not hasattr(V, n)]
    assert not missing, f'listed in sync_gate_docs but gone from validator: {missing}'


def test_the_table_actually_carries_the_live_values():
    import validator as V
    body = DOC.read_text()
    table = body[body.index('GATES:BEGIN'):body.index('GATES:END')]
    for name in ('MIN_IS_SCORE', 'MIN_WF_SCORE', 'MIN_HO_SCORE',
                 'HOLDOUT_DECLINE_THRESHOLD', 'MIN_HO_ENTRIES'):
        assert f'`{name}`' in table, f'{name} dropped out of the table'
        assert str(getattr(V, name)) in table, f'{name} value is stale in the table'


@pytest.mark.parametrize('doc', ['README.md', 'QUICKSTART.md',
                                 'PROJECT_COMPLETION_SUMMARY.md'])
def test_the_other_docs_do_not_restate_the_gate_numbers(doc):
    """They must point at the generated table, not carry their own copy.

    All three claimed "GT-Score > 1.0" for walk-forward when the gate is 0.5, and
    "max 30% decay" when HOLDOUT_DECLINE_THRESHOLD 0.6 allows 40%.
    """
    text = (REPO / doc).read_text()
    stale = re.findall(
        r'(?:GT-Score|walk-forward|hold-out|decay)[^\n]{0,60}?'
        r'(?:\*\*)?(?:>|<|>=|<=|≥|≤)\s*(?:\*\*)?\s*(0\.[0-9]+|1\.0|30%|70%)',
        text, re.I)
    assert not stale, (
        f'{doc} restates gate numbers {sorted(set(stale))}. Point at the generated '
        f'table in ARCHITECTURE.md instead — every hand-copied set has drifted.')


class TestTheDsrGateStaysOffAndTheWorkOrderSaysSo:
    """DSR_GATE off by default is a binding decision, not an accident.

    INTEGRATE_honesty.md is a COMPLETED work order whose wiring point 2 says
    "if dsr < 0.95: reject ... do NOT promote". The code deliberately does not do
    that: DSR deflates Sharpe while the pipeline selects on GT-score, so DSR runs
    systematically low for any GT-selected winner and a low value does not mean
    overfit. Overfit is controlled by the locked holdout. Reversed in f1933dd.

    Every other test in test_honesty_wiring.py monkeypatches DSR_GATE_ENABLED, so
    nothing pinned the DEFAULT — the one thing that decides live behaviour. That is
    what these pin, along with the header that stops a reader following the brief.
    """

    DOC = REPO / 'INTEGRATE_honesty.md'

    def test_dsr_gate_defaults_off_in_the_source(self):
        """Asserted against the SOURCE, deliberately, not a reloaded module.

        The first version of this test reloaded validator and read the attribute.
        That is unreliable here: sys.pycache_prefix points outside the repo
        (~/Library/Caches/com.apple.python on this machine), and Python invalidates
        a .pyc on (mtime, size). Editing a one-character constant back and forth
        within the same second changes neither, so the interpreter happily reuses
        bytecode compiled from the edited file. It cost an investigation: the
        reloaded module reported DSR_GATE_ENABLED True with the env var unset and
        the source reading '0', because the executed line was the stale one.
        """
        src = (REPO / 'validator.py').read_text()
        line = next(l for l in src.split('\n')
                    if l.startswith('DSR_GATE_ENABLED'))
        assert "'DSR_GATE', '0'" in line, (
            f'DSR_GATE no longer defaults off: {line.strip()}. That hard-rejects '
            'valid GT-selected strategies on a mismatched axis — re-aim it at '
            'GT-score before ever gating on it.')

    def test_dsr_gate_is_off_at_runtime_with_no_env_var(self, monkeypatch):
        monkeypatch.delenv('DSR_GATE', raising=False)
        import validator
        assert validator.DSR_GATE_ENABLED is False

    def test_the_completed_work_order_is_marked_as_not_a_task(self):
        text = self.DOC.read_text()
        assert 'COMPLETED' in text.split('\n')[0], (
            'INTEGRATE_honesty.md must announce in its title that it is finished. '
            'Read as an open task it instructs setting a DSR reject that '
            'validator.py explicitly warns against.')
        assert 'REVERSED' in text, 'the reversal of wiring point 2 must be stated'

    def test_the_work_order_points_at_the_generated_gate_table(self):
        assert 'ARCHITECTURE.md#validation-gates' in self.DOC.read_text()
