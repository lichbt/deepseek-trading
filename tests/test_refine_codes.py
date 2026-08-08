"""The refinement-safety partition must fail CLOSED.

Every test here is a leak control, not documentation. The partition decides what
gets fed back to the generator, and the pinned validation windows
(validator.py:173-175) mean a wrong MECHANICAL is a strategy hill-climbing the
same holdout its parent failed. A wrong VERDICT costs one missed refinement.
Those are not symmetric, so anything undecidable must land outside MECHANICAL.

The exact-zero cases are the ones that matter. 125 of 156 walk_forward_failed
rows in the live DB carry the string "GT-Score 0.0000 < 0.2" and are
indistinguishable from each other; per the 2026-08-03 binding correction that
zero is a SENTINEL from one of three early-return guards in compute_gt_score,
not a computed score. Resolving it from stored prose is the leak.
"""

import pipeline_utils as pu
import refine_codes as R


# --- the exact-zero sentinel ----------------------------------------------

def test_exact_zero_without_a_rerun_refuses_to_decide():
    """The whole point. Prose alone cannot tell a coverage guard from a real
    zero, so it must not try."""
    got = R.classify('walk_forward_failed', 'FAIL: Walk-forward GT-Score 0.0000 < 0.2')
    assert got == R.NEEDS_RERUN
    assert not R.is_refinable(got)


def test_exact_zero_resolves_mechanical_only_with_a_coverage_guard():
    for zr in (pu.GT_ZERO_TOO_SHORT, pu.GT_ZERO_NO_VOL, f'{pu.GT_ZERO_FEW_ACTIVE}:12'):
        got = R.classify('walk_forward_failed',
                         'FAIL: Walk-forward GT-Score 0.0000 < 0.2', zero_reason=zr)
        assert got == R.MECHANICAL, zr
        assert R.is_refinable(got)


def test_exact_zero_with_a_real_score_is_a_verdict():
    """negative_clamped and genuinely_zero are measurements, not coverage bugs."""
    for zr in (f'{pu.GT_ZERO_CLAMPED}:-0.1234', pu.GT_ZERO_EXACT):
        got = R.classify('walk_forward_failed',
                         'FAIL: Walk-forward GT-Score 0.0000 < 0.2', zero_reason=zr)
        assert got == R.VERDICT, zr
        assert not R.is_refinable(got)


def test_unrecognised_zero_reason_is_not_refinable():
    got = R.classify('walk_forward_failed',
                     'FAIL: Walk-forward GT-Score 0.0000 < 0.2', zero_reason='something_new')
    assert not R.is_refinable(got)


# --- ordinary verdicts ------------------------------------------------------

def test_a_nonzero_score_miss_is_a_verdict_even_when_close():
    """A near-miss is the most tempting thing to refine and the least safe."""
    got = R.classify('walk_forward_failed', 'FAIL: Walk-forward GT-Score 0.1977 < 0.2')
    assert got == R.VERDICT
    assert not R.is_refinable(got)


def test_holdout_decay_is_a_verdict():
    got = R.classify('holdout_failed', 'FAIL: Holdout decay 0.61 below 0.7 of WF')
    assert not R.is_refinable(got)


# --- integrity outranks everything -----------------------------------------

def test_lookahead_is_never_refinable():
    """Refining a leaking strategy is an attempt to get it past the gate that
    caught it, not a repair."""
    got = R.classify('research_failed', 'FAIL: Look-ahead gate failed — non-causal signal')
    assert got == R.INTEGRITY
    assert not R.is_refinable(got)


def test_integrity_wins_over_an_exact_zero_in_the_same_reason():
    got = R.classify('research_failed',
                     'FAIL: Look-ahead gate failed — scan-and-fill, GT-Score 0.0000 < 0.2')
    assert got == R.INTEGRITY


# --- infra ------------------------------------------------------------------

def test_timeout_and_missing_data_are_infra_not_refinable():
    """A timeout says nothing about the idea; the fix is a re-run."""
    assert not R.is_refinable(R.classify('research_failed', 'Strategy timed out during grid search'))
    assert not R.is_refinable(R.classify('research_failed', 'No valid data for instrument'))


# --- the scrubber -----------------------------------------------------------

def test_scrub_drops_a_score_but_keeps_a_trade_count():
    assert '0.1234' not in R.scrub('negative_clamped:-0.1234')
    assert R.scrub(f'{pu.GT_ZERO_FEW_ACTIVE}:12') == f'{pu.GT_ZERO_FEW_ACTIVE}:12'


def test_scrub_is_none_safe():
    assert R.scrub(None) is None
    assert R.scrub('') == ''


# --- the constants must stay wired to pipeline_utils ------------------------

def test_partition_uses_the_real_pipeline_utils_constants():
    """Guards the bug this module shipped with once: hand-written prefixes that
    did not match GT_ZERO_* and silently sent every zero to UNKNOWN."""
    assert R._ZERO_MECHANICAL == {pu.GT_ZERO_TOO_SHORT, pu.GT_ZERO_FEW_ACTIVE, pu.GT_ZERO_NO_VOL}
    assert R._ZERO_VERDICT == {pu.GT_ZERO_CLAMPED, pu.GT_ZERO_EXACT}


# --- the validator wiring ---------------------------------------------------

def test_record_validation_populates_failure_cause(tmp_path, monkeypatch):
    """The partition has to reach the database, not just exist as a function.

    Exercised against a temp DB: record_validation writes to DB_PATH, and the
    real pipeline.db is 331MB of research state that no test may touch.
    """
    import sqlite3
    db = tmp_path / 'p.db'
    monkeypatch.setattr(pu, 'DB_PATH', db)
    pu.init_db()

    con = sqlite3.connect(str(db))
    con.execute("INSERT INTO strategies(id,fingerprint,code,param_grid,status,created_at) "
                "VALUES('s1','fp1','','{}','proposed','now')")
    con.commit()
    con.close()

    # A near-miss on walk-forward is a verdict, and must be recorded as one.
    # Both spellings of this gate now route the same way — see
    # test_both_spellings_of_the_wf_gate_route_the_same below.
    pu.record_validation('s1', {}, 0.42, 0.1977, None,
                         'FAIL: Walk-forward GT-Score 0.1977 < 0.5')

    con = sqlite3.connect(str(db))
    cause, status = con.execute(
        "SELECT v.failure_cause, s.status FROM validation_results v "
        "JOIN strategies s ON s.id=v.strategy_id WHERE v.strategy_id='s1'").fetchone()
    con.close()

    assert status == 'walk_forward_failed'
    assert cause == R.VERDICT
    assert not R.is_refinable(cause)


def test_record_validation_marks_an_exact_zero_as_needing_a_rerun(tmp_path, monkeypatch):
    """Prose alone cannot resolve the sentinel, so the stored value must say so
    rather than committing to a partition it cannot justify."""
    import sqlite3
    db = tmp_path / 'p.db'
    monkeypatch.setattr(pu, 'DB_PATH', db)
    pu.init_db()
    con = sqlite3.connect(str(db))
    con.execute("INSERT INTO strategies(id,fingerprint,code,param_grid,status,created_at) "
                "VALUES('s2','fp2','','{}','proposed','now')")
    con.commit(); con.close()

    pu.record_validation('s2', {}, 0.42, 0.0, None, 'FAIL: Walk-forward GT-Score 0.0000 < 0.2')

    con = sqlite3.connect(str(db))
    cause, = con.execute(
        "SELECT failure_cause FROM validation_results WHERE strategy_id='s2'").fetchone()
    con.close()
    assert cause == R.NEEDS_RERUN


# --- the two spellings of the walk-forward gate -----------------------------

def _status_for(reason, tmp_path, monkeypatch, sid):
    import sqlite3
    db = tmp_path / f'{sid}.db'
    monkeypatch.setattr(pu, 'DB_PATH', db)
    pu.init_db()
    con = sqlite3.connect(str(db))
    con.execute("INSERT INTO strategies(id,fingerprint,code,param_grid,status,created_at) "
                f"VALUES('{sid}','fp_{sid}','','{{}}','proposed','now')")
    con.commit(); con.close()
    pu.record_validation(sid, {}, 0.42, 0.4210, None, reason)
    con = sqlite3.connect(str(db))
    st, = con.execute(f"SELECT status FROM strategies WHERE id='{sid}'").fetchone()
    con.close()
    return st


def test_both_spellings_of_the_wf_gate_route_the_same(tmp_path, monkeypatch):
    """validator.py emits "WF 0.4210 < 0.5" (line 499) and "Walk-forward
    GT-Score 0.4210 < 0.5" for the SAME gate. Only the second contained both
    "walk" and "forward", so 22,430 terse ones were filed as research_failed —
    28% of that bucket, and enough to distort every status-partitioned count."""
    verbose = _status_for('FAIL: Walk-forward GT-Score 0.4210 < 0.5', tmp_path, monkeypatch, 'v1')
    terse = _status_for('FAIL: WF 0.4210 < 0.5', tmp_path, monkeypatch, 't1')
    assert verbose == 'walk_forward_failed'
    assert terse == verbose


def test_a_bare_wf_substring_does_not_hijack_the_status(tmp_path, monkeypatch):
    """The matcher is anchored and shape-checked on purpose: loosening it to a
    bare 'wf' substring would drag unrelated failures into the bucket."""
    assert _status_for('FAIL: code error in wf_helper()', tmp_path, monkeypatch,
                       'b1') == 'research_failed'
