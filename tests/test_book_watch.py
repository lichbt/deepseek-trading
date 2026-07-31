"""Pin book_watch's silence rules and its dedup.

The rules are the product here, not the queries. A watcher that fires on
weekends, on freshly deployed sleeves, or once per bar for the same ongoing
stall is worse than no watcher — it trains you to ignore the one alert that
matters (the orphan-sweep lesson, 2026-07-31).
"""
import os
import sqlite3
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 'scripts'))
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import book_watch as bw


# --------------------------------------------------------------------------
# book_bars — the reference calendar
# --------------------------------------------------------------------------
def test_book_bar_needs_a_quorum_of_sleeves():
    """A bar only a couple of sleeves could trade is not a book bar."""
    rows = [('b1', 'a'), ('b1', 'b'), ('b1', 'c'), ('b1', 'd'),
            ('b2', 'a')]
    assert bw.book_bars(rows, n_live=4) == ['b1']


def test_crypto_only_weekend_bars_do_not_make_the_fx_book_stale():
    """BTC and ETH trade weekends; FX and the indices do not. Counting every
    distinct bar_time would mark 8 of 10 sleeves two bars behind every Monday."""
    live = [f's{i}' for i in range(10)]
    crypto = ('s0', 's1')
    fri, sat, sun = ('2026-07-24 21:00:00+00:00', '2026-07-25 21:00:00+00:00',
                     '2026-07-26 21:00:00+00:00')
    # Everyone traded Friday; only the two crypto sleeves have weekend bars.
    # The watcher runs on Sunday, before Monday's bar exists.
    rows = [(fri, s) for s in live] + [(bt, s) for bt in (sat, sun) for s in crypto]
    last = {}
    for bt, s in rows:
        last[s] = max(bt, last.get(s, ''))

    assert bw.book_bars(rows, n_live=10) == [fri]
    assert bw.stale_sleeves(bw.book_bars(rows, n_live=10), last, set(live), threshold=1) == []

    # Without the quorum every distinct bar_time would be a book bar, and the
    # eight FX sleeves would read two bars behind every single weekend.
    naive = sorted({bt for bt, _ in rows})
    assert naive == [fri, sat, sun]
    assert len(bw.stale_sleeves(naive, last, set(live), threshold=1)) == 8


# --------------------------------------------------------------------------
# stale_sleeves — the failure this script exists for
# --------------------------------------------------------------------------
def test_a_sleeve_that_stopped_evaluating_is_reported():
    """The usdchf_i21 shape: the book moved on nine bars, one sleeve did not."""
    bars = [f'b{i}' for i in range(10)]
    live = {'ok', 'stuck'}
    last = {'ok': 'b9', 'stuck': 'b0'}
    assert bw.stale_sleeves(bars, last, live) == [('stuck', 'b0', 9)]


def test_a_sleeve_one_bar_behind_is_not_reported():
    bars = ['b0', 'b1']
    assert bw.stale_sleeves(bars, {'s': 'b0'}, {'s'}, threshold=3) == []


def test_a_retired_sleeve_is_ignored():
    """A retired sleeve stops writing rows BY DESIGN and would alert forever."""
    bars = [f'b{i}' for i in range(10)]
    assert bw.stale_sleeves(bars, {'gone': 'b0'}, live=set()) == []


def test_a_sleeve_with_no_rows_at_all_is_not_reported():
    """Indistinguishable from a deploy an hour ago. main() lists it without
    alerting; inventing an alarm here would be the wolf-crying failure."""
    bars = [f'b{i}' for i in range(10)]
    assert bw.stale_sleeves(bars, {}, {'brand_new'}) == []


def test_lag_is_counted_from_a_non_book_bar_too():
    """A sleeve whose newest row is a crypto weekend bar still gets measured
    against the book calendar rather than silently skipped. Uses real ISO
    bar_times because the ordering is lexicographic — see book_bars()."""
    sun = '2026-07-26 21:00:00+00:00'
    bars = ['2026-07-27 21:00:00+00:00', '2026-07-28 21:00:00+00:00',
            '2026-07-29 21:00:00+00:00', '2026-07-30 21:00:00+00:00']
    assert bw.stale_sleeves(bars, {'s': sun}, {'s'}, threshold=3) == [('s', sun, 4)]


# --------------------------------------------------------------------------
# losing_bars
# --------------------------------------------------------------------------
def test_a_bad_day_is_reported_and_a_good_one_is_not():
    out = bw.losing_bars([('good', 950.73), ('bad', -3978.55)], equity=100_000, pct=0.015)
    assert [b for b, _, _ in out] == ['bad']
    assert out[0][2] == pytest.approx(-0.0397, abs=1e-4)


def test_a_null_pnl_bar_is_skipped_not_read_as_flat():
    """sleeve_pnl is NULL on log-backfilled rows and on everything before the
    currency columns existed. Treating no-data as zero is how a missing bar
    becomes an invisible one."""
    assert bw.losing_bars([('nodata', None)], equity=100_000, pct=0.015) == []


def test_threshold_is_a_fraction_of_nominal_equity():
    rows = [('x', -1400.0)]
    assert bw.losing_bars(rows, equity=100_000, pct=0.015) == []
    assert len(bw.losing_bars(rows, equity=100_000, pct=0.013)) == 1


# --------------------------------------------------------------------------
# dedup
# --------------------------------------------------------------------------
def test_an_already_announced_finding_is_suppressed():
    f = [(bw.SLEEVE_STALE, 's', 'b0', 'detail')]
    assert bw.suppress_recorded(f, {(bw.SLEEVE_STALE, 's', 'b0')}) == []
    assert bw.suppress_recorded(f, {(bw.SLEEVE_STALE, 's', 'b9')}) == f


def test_an_ongoing_stall_alerts_once_not_once_per_bar():
    """The key to the dedup: bar_time is the sleeve's LAST OBSERVED bar, which
    does not move while the sleeve is stuck. As the gap widens from 3 to 9 the
    finding keys the same, so it is announced exactly once per episode."""
    recorded = set()
    for behind in range(3, 10):
        f = bw.suppress_recorded([(bw.SLEEVE_STALE, 's', 'b0', f'{behind} behind')], recorded)
        for code, sid, bar, _ in f:
            recorded.add((code, sid, bar))
    assert recorded == {(bw.SLEEVE_STALE, 's', 'b0')}


def test_a_later_separate_stall_alerts_again():
    recorded = {(bw.SLEEVE_STALE, 's', 'b0')}
    later = [(bw.SLEEVE_STALE, 's', 'b40', 'stuck again')]
    assert bw.suppress_recorded(later, recorded) == later


# --------------------------------------------------------------------------
# the store
# --------------------------------------------------------------------------
@pytest.fixture
def db(tmp_path):
    """A temp DB — the real book_events is sealed against UPDATE/DELETE, so
    tests must never point at it."""
    path = tmp_path / 'x.db'
    conn = sqlite3.connect(path)
    conn.executescript(open(os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
        'migrations', '006_book_events.sql')).read())
    yield conn
    conn.close()


def test_the_store_is_append_only(db):
    bw.record(db, bw.BOOK_LOSS, '', 'b1', 'detail')
    with pytest.raises(sqlite3.IntegrityError):
        db.execute("UPDATE book_events SET detail='x'")
    with pytest.raises(sqlite3.IntegrityError):
        db.execute("DELETE FROM book_events")


def test_recording_the_same_finding_twice_is_a_no_op(db):
    bw.record(db, bw.SLEEVE_STALE, 's', 'b0', 'first')
    bw.record(db, bw.SLEEVE_STALE, 's', 'b0', 'second')
    assert db.execute("SELECT COUNT(*) FROM book_events").fetchone()[0] == 1
    assert db.execute("SELECT detail FROM book_events").fetchone()[0] == 'first'


def test_book_level_rows_dedup_despite_having_no_sleeve(db):
    """sleeve_id is '' rather than NULL precisely so UNIQUE still bites —
    NULLs compare distinct in SQLite and every book-level row would re-alert."""
    bw.record(db, bw.BOOK_LOSS, '', 'b1', 'a')
    bw.record(db, bw.BOOK_LOSS, '', 'b1', 'b')
    bw.record(db, bw.BOOK_LOSS, '', 'b2', 'c')
    assert db.execute("SELECT COUNT(*) FROM book_events").fetchone()[0] == 2
