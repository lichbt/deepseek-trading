"""A rejected holdout must record what the strategy DID, not a placeholder.

The entry-count gate used to return a hardcoded 'ho_score': 0.0 without ever
calling evaluate_on_data. The consequence was measured on 2026-08-08: all 241
holdout_failed rows carried exactly 0.0 — ONE distinct value — while
is_gt_score had 239 and walk_forward_gt_score had 240.

That is worse than missing data, because a placeholder reads like a
measurement. It made "traded well but too few entries to trust" and "decayed to
nothing" indistinguishable in the database, and it misled analysis of this
bucket repeatedly before the cause was found in the code rather than the data.

Scoring before the gate changes no decision — the gate still rejects on entry
count — so these tests pin both halves: the record becomes informative, and the
verdict does not move.
"""

import inspect
import re

import validator as V


def _holdout_section() -> str:
    """Source between the holdout step and the decay check."""
    src = inspect.getsource(V.validate_single_timeframe) if hasattr(
        V, 'validate_single_timeframe') else inspect.getsource(V)
    i = src.find('Step 7: Hold-out validation')
    j = src.find('Calculate acceptable HO threshold')
    assert i != -1 and j != -1 and j > i, 'holdout section not found — test needs updating'
    return src[i:j]


def test_the_holdout_is_scored_before_the_entry_count_gate():
    """If evaluate_on_data comes after the gate, the rejection can only record a
    placeholder — which is exactly the defect."""
    sec = _holdout_section()
    score_at = sec.find('ho_score = evaluate_on_data')
    gate_at = sec.find('if ho_entries < MIN_HO_ENTRIES')
    assert score_at != -1, 'holdout is never scored'
    assert gate_at != -1, 'entry-count gate not found'
    assert score_at < gate_at, (
        'the entry-count gate returns before the holdout is scored, so every '
        'rejected row records a placeholder instead of a measurement')


def test_the_entry_count_rejection_records_a_real_score():
    sec = _holdout_section()
    gate_at = sec.find('if ho_entries < MIN_HO_ENTRIES')
    rejection = sec[gate_at:gate_at + 900]
    assert "'ho_score': float(ho_score)" in rejection, (
        'the entry-count rejection still hardcodes a placeholder score')
    assert "'ho_score': 0.0" not in rejection


def test_the_gate_still_rejects_on_entry_count():
    """Scoring earlier must not turn a rejection into a pass — the whole point
    is that a high score from a handful of entries is noise, not edge."""
    sec = _holdout_section()
    gate_at = sec.find('if ho_entries < MIN_HO_ENTRIES')
    rejection = sec[gate_at:gate_at + 900]
    assert "'passed': False" in rejection
    assert 'not statistically reliable' in rejection


def test_the_zero_trade_path_still_records_zero():
    """A strategy that never fired has no score to record, and 0.0 there is
    honest rather than a placeholder."""
    sec = _holdout_section()
    zero_at = sec.find('if ho_trade_count == 0')
    assert zero_at != -1
    assert zero_at < sec.find('ho_score = evaluate_on_data'), (
        'the zero-trade case must short-circuit before scoring — there is '
        'nothing to score')


def test_the_holdout_is_scored_exactly_once():
    """The earlier computation was moved, not copied; a duplicate would double
    the cost of every validation for no gain."""
    assert _holdout_section().count('ho_score = evaluate_on_data') == 1
