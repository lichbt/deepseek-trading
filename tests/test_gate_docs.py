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
