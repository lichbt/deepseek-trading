"""Pin evaluate_strategy.pair_correlation.

These are NEGATIVE CONTROLS, not documentation: the first test FAILS against a
full-sample-only implementation, which is what the whole-book curation branch
did before 2026-08-04. Measured on the live book that day, 0 pairs exceeded 0.5
full-sample while 12 exceeded it both-in-market (worst +0.85 at 100%
same-direction), so a candidate could read "max |corr| 0.39" and be +0.74
against an incumbent whenever it was actually exposed.
"""
import sys
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))
from evaluate_strategy import pair_correlation


def _series(vals, start='2024-01-01'):
    idx = pd.date_range(start, periods=len(vals), freq='D')
    return pd.Series(vals, index=idx, dtype=float)


def test_full_sample_is_diluted_by_flat_bars_but_both_in_market_is_not():
    """THE POINT OF THE FUNCTION. Two sleeves that move identically whenever both
    hold, and are flat the rest of the time, must read ~1.0 both-in-market — while
    full-sample is dragged toward 0 by the shared flat bars."""
    n = 400
    rng = np.random.default_rng(0)
    overlap = 40                       # both in-market for 10% of the history
    sa = np.zeros(n); sb = np.zeros(n)
    ra = np.zeros(n); rb = np.zeros(n)
    sa[:overlap] = 1; sb[:overlap] = 1
    shared = rng.normal(0, 0.01, overlap)
    ra[:overlap] = shared
    rb[:overlap] = shared * 1.02       # near-identical, not degenerate
    # each also trades alone elsewhere, uncorrelated — this is what dilutes `full`
    sa[100:250] = 1; ra[100:250] = rng.normal(0, 0.01, 150)
    sb[250:400] = -1; rb[250:400] = rng.normal(0, 0.01, 150)

    pc = pair_correlation(_series(sa), _series(ra), _series(sb), _series(rb))

    assert pc['n_both'] == overlap
    assert pc['both_in_mkt'] > 0.95, pc
    assert pc['full'] < 0.5, pc                       # the gate would MISS it
    assert pc['both_in_mkt'] - pc['full'] > 0.5, pc   # and by a wide margin


def test_same_direction_is_measured_only_on_overlapping_bars():
    sa = _series([1, 1, 1, 1, 0, 0, -1, -1])
    sb = _series([1, 1, -1, -1, 1, 1, 0, 0])
    r = _series([0.01, -0.01, 0.02, -0.02, 0.01, 0.0, 0.01, 0.0])
    pc = pair_correlation(sa, r, sb, r, min_both=1)
    # bars 0-3 overlap (2 same-sign, 2 opposite); bars 4-7 have one leg flat
    assert pc['n_both'] == 4
    assert pc['same_dir'] == pytest.approx(0.5)


def test_both_in_market_is_nan_below_the_floor_and_never_a_small_sample_number():
    """A ranking must not be topped by a 3-bar correlation."""
    sa = _series([1, 1, 1, 0, 0, 0, 0, 0])
    sb = _series([1, 1, 1, 0, 0, 0, 0, 0])
    r = _series([0.01, -0.01, 0.02, 0.0, 0.0, 0.0, 0.0, 0.0])
    pc = pair_correlation(sa, r, sb, r, min_both=30)
    assert pc['n_both'] == 3
    assert np.isnan(pc['both_in_mkt'])
    # same_dir is a proportion, not a correlation — still reported below the floor
    assert pc['same_dir'] == pytest.approx(1.0)


def test_floor_is_a_parameter_so_the_two_curation_branches_can_differ():
    """The head-to-head branch uses 10, the whole-book ranking uses 30. If this
    ever collapses to one value, one of the two callers changed behaviour."""
    sa = _series([1] * 20 + [0] * 20)
    sb = _series([1] * 20 + [0] * 20)
    rng = np.random.default_rng(1)
    ra = _series(list(rng.normal(0, 0.01, 40)))
    rb = _series(list(rng.normal(0, 0.01, 40)))
    assert not np.isnan(pair_correlation(sa, ra, sb, rb, min_both=10)['both_in_mkt'])
    assert np.isnan(pair_correlation(sa, ra, sb, rb, min_both=30)['both_in_mkt'])


def test_no_overlap_at_all_is_zero_not_a_crash():
    sa = _series([1, 1, 1, 1, 0, 0, 0, 0])
    sb = _series([0, 0, 0, 0, 1, 1, 1, 1])
    r = _series([0.01, -0.01, 0.02, -0.02, 0.01, -0.01, 0.02, -0.02])
    pc = pair_correlation(sa, r, sb, r, min_both=1)
    assert pc['n_both'] == 0
    assert pc['same_dir'] == 0.0
    assert np.isnan(pc['both_in_mkt'])


def test_misaligned_indices_align_rather_than_silently_dropping_everything():
    """The two sleeves are reconstructed independently and need not share an index.
    A naive positional comparison would read garbage here."""
    a_idx = pd.date_range('2024-01-01', periods=60, freq='D')
    b_idx = pd.date_range('2024-01-11', periods=60, freq='D')   # 50 bars overlap
    sa = pd.Series(1.0, index=a_idx)
    sb = pd.Series(1.0, index=b_idx)
    rng = np.random.default_rng(2)
    ra = pd.Series(rng.normal(0, 0.01, 60), index=a_idx)
    # identical values ON THE SHARED DATES; the 10 dates past a_idx get their own
    rb = ra.reindex(b_idx)
    rb[rb.isna()] = rng.normal(0, 0.01, int(rb.isna().sum()))
    pc = pair_correlation(sa, ra, sb, rb, min_both=10)
    assert pc['n_both'] == 50
    assert pc['both_in_mkt'] > 0.99, pc
