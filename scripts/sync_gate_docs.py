#!/usr/bin/env python3
"""Render the validation-gate table into ARCHITECTURE.md from validator.py.

WHY THIS EXISTS. The gates were written by hand into four documents and every one
of them drifted. ARCHITECTURE.md claimed "IS > 0.5 / WF > 1.0 combined, > 0.3 min
/ decay < 30%" while the code ran IS 0.3 / WF 0.5 / min window 0.0 / decay 0.6 —
five stated numbers, five wrong — and omitted MIN_HO_SCORE and MIN_HO_ENTRIES
entirely. README.md, QUICKSTART.md and PROJECT_COMPLETION_SUMMARY.md carried the
same stale set. A 2026-07-29 decision had ALREADY recorded that the documented
gates "were never accurate to the code"; the decision log was corrected and the
documents were not, so the wrong numbers stayed readable for another eight months.

Copying them correctly once would only restart that clock. So the table is
generated, and tests/test_gate_docs.py fails the suite when the file is stale.

    python3 scripts/sync_gate_docs.py           # rewrite the block
    python3 scripts/sync_gate_docs.py --check   # exit 1 if stale (what the test runs)
"""
from __future__ import annotations

import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DOC = os.path.join(REPO, 'ARCHITECTURE.md')
BEGIN = '<!-- GATES:BEGIN'
END = '<!-- GATES:END -->'

# (constant, what it gates, how it is applied). The third column is prose because
# several gates are relative or conditional and a bare number misleads without it.
GATES = [
    ('MIN_IS_SCORE',              'in-sample GT-Score',   'dev-window grid search must reach this'),
    ('MIN_WF_SCORE',              'walk-forward GT-Score', 'the out-of-sample quality gate'),
    ('MIN_WINDOW_SCORE',          'worst WF window',      'no losing window; breakeven allowed'),
    ('MIN_HO_SCORE',              'hold-out GT-Score',    'absolute floor, independent of WF'),
    ('HOLDOUT_DECLINE_THRESHOLD', 'hold-out vs WF',       'HO must reach this FRACTION of WF, so 1-x is the max relative decay'),
    ('MIN_HO_ENTRIES',            'hold-out trades',      'DISTINCT entries, not bars in position'),
    ('MAX_OOS_DRAWDOWN',          'max drawdown',         'hard gate on reconstructed full-history equity'),
    ('MIN_CALMAR_RATIO',          'Calmar',               'soft gate — flags, does not fail'),
    ('LOOKAHEAD_MAX_FLIP_RATE',   'look-ahead flip rate', 'fraction of sampled bars whose signal changes under truncation'),
    ('DSR_MIN',                   'deflated Sharpe',      'DSR_GATE is OFF — descriptive only, see the traps in sleeve-ops'),
]


def render() -> str:
    import validator as V
    rows = []
    for name, gates, how in GATES:
        val = getattr(V, name, None)
        if val is None:
            continue
        rows.append(f'| `{name}` | {val} | {gates} | {how} |')
    body = ['', '| constant | value | gates | how it applies |',
            '|---|---|---|---|', *rows, '']
    return '\n'.join(body)


def _split(text):
    i = text.index(BEGIN)
    j = text.index(END, i)
    head = text[:text.index('-->', i) + 3]
    return head, text[j:]


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument('--check', action='store_true')
    a = ap.parse_args()

    text = open(DOC).read()
    if BEGIN not in text or END not in text:
        raise SystemExit(f'{DOC} is missing the GATES markers')
    head, tail = _split(text)
    new = head + render() + tail

    if a.check:
        if new != text:
            print('ARCHITECTURE.md gate table is STALE — run scripts/sync_gate_docs.py')
            raise SystemExit(1)
        print('ARCHITECTURE.md gate table matches validator.py')
        return
    open(DOC, 'w').write(new)
    print(f'wrote {len(GATES)} gates into {DOC}')


if __name__ == '__main__':
    main()
