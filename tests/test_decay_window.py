"""The decay window must be bounded by calendar time, not entry count alone.

Bounding by entries alone let "RECENT30" span 7.4 years on a selective sleeve, so
the verdict was a lifetime-GT-vs-WF comparison wearing a "recent" label (24 of 54
scored sleeves had windows over 3 years, 2026-07-25). These tests pin the two
bounds, the too-thin-to-judge floor, and the agreement between the live verdict
(portfolio) and the review tool (evaluate_strategy).
"""
import sys
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))
import portfolio as P


def make_sig(years=8.0, entries_per_year=4, end='2026-07-23'):
    """Daily series that enters (and exits a bar later) at a fixed cadence."""
    end = pd.Timestamp(end)
    idx = pd.date_range(end - pd.Timedelta(days=int(years * 365)), end, freq='D')
    sig = pd.Series(0, index=idx, dtype=float)
    step = max(1, int(round(365 / entries_per_year)))
    for i in range(0, len(idx) - 2, step):
        sig.iloc[i:i + 2] = 1.0          # 2-bar holding, then flat -> distinct entry
    return sig


def test_calendar_bound_binds_on_a_selective_sleeve():
    # ~4 entries/year: 30 entries reach back ~7.5 years. The 18mo cap must win.
    sig = make_sig(years=8.0, entries_per_year=4)
    w = P.recent_decay_window(sig)
    assert w['capped_by'] == 'calendar'
    span_days = (sig.index[-1] - sig.index[w['start']]).days
    assert span_days <= P.RECENT_DECAY_MAX_MONTHS * 31
    # ...and with only ~6 entries in 18 months it is too thin to judge.
    assert w['in_window'] < P.RECENT_DECAY_MIN_ENTRIES


def test_entry_bound_binds_on_an_active_sleeve():
    # ~100 entries/year: 30 entries fit well inside 18 months.
    sig = make_sig(years=8.0, entries_per_year=100)
    w = P.recent_decay_window(sig)
    assert w['capped_by'] == 'entries'
    assert w['in_window'] == P.RECENT_DECAY_ENTRIES
    span_days = (sig.index[-1] - sig.index[w['start']]).days
    assert span_days < P.RECENT_DECAY_MAX_MONTHS * 31


def test_selective_sleeve_is_insufficient_not_decayed():
    sig = make_sig(years=8.0, entries_per_year=4)
    ret = pd.Series(0.001, index=sig.index)
    out = P.recent_decay_status(ret, sig, wf_score=1.0)
    # Previously this scored a 7-year window against half the WF and read DECAYED.
    assert out['status'] == 'INSUFFICIENT'
    assert out['kelly_scale'] == 1.0          # no haircut on "can't tell"


def test_moderately_active_sleeve_is_scored():
    sig = make_sig(years=8.0, entries_per_year=24)   # ~36 entries in 18mo
    ret = pd.Series(0.001, index=sig.index)
    out = P.recent_decay_status(ret, sig, wf_score=0.1)
    assert out['status'] in ('OK', 'DECAYED')
    assert out['in_window'] >= P.RECENT_DECAY_MIN_ENTRIES


def test_negative_recent_return_still_decays():
    sig = make_sig(years=3.0, entries_per_year=60)
    ret = pd.Series(-0.001, index=sig.index)
    out = P.recent_decay_status(ret, sig, wf_score=1.0)
    assert out['status'] == 'DECAYED'
    assert out['kelly_scale'] == P.DECAY_KELLY_SCALE


def test_asof_ignores_later_entries():
    sig = make_sig(years=8.0, entries_per_year=24)
    asof = sig.index[-1] - pd.Timedelta(days=400)
    w = P.recent_decay_window(sig, asof=asof)
    assert sig.index[w['start']] <= asof
    assert (sig.index[-1] - sig.index[w['start']]).days > 400


def test_review_tool_agrees_with_the_live_verdict():
    """evaluate_strategy must not drift from the verdict that drives weights."""
    import evaluate_strategy as E
    sig = make_sig(years=8.0, entries_per_year=24)
    ret = pd.Series(0.001, index=sig.index)
    live = P.recent_decay_status(ret, sig, wf_score=0.1)
    review = E.recent_entry_decay(sig, ret, 0.1)
    assert review['status'] == live['status']
    assert review['in_window'] == live['in_window']
    assert review['capped_by'] == live['capped_by']
    assert review['recent_gt'] == pytest.approx(live['recent_gt'])


def test_no_entries_is_unscoreable():
    idx = pd.date_range('2025-01-01', '2026-07-23', freq='D')
    sig = pd.Series(0.0, index=idx)
    w = P.recent_decay_window(sig)
    assert w['start'] is None
    out = P.recent_decay_status(pd.Series(0.0, index=idx), sig, wf_score=1.0)
    assert out['status'] == 'INSUFFICIENT'


def make_ret(sig, drift=0.0008, vol=0.006, seed=0):
    """Profitable-but-noisy returns. A CONSTANT series has zero variance, which
    makes the GT score degenerate — it must vary for the near-miss band to mean
    anything."""
    rng = np.random.default_rng(seed)
    return pd.Series(rng.normal(drift, vol, len(sig)), index=sig.index)


def _wf_for_gt_multiple(sig, ret, mult):
    """WF that puts min_gt at `mult` x the GT this sleeve actually achieves."""
    base = P.recent_decay_status(ret, sig, wf_score=0.0)
    assert base['status'] == 'OK', 'fixture must clear a zero bar first'
    return (base['recent_gt'] * mult) / P.RECENT_DECAY_GT_FRACTION


def test_near_miss_is_insufficient_not_decayed():
    """A profitable sleeve a hair under its own WF-scaled bar is noise, not decay."""
    sig = make_sig(years=2.0, entries_per_year=60)
    ret = make_ret(sig)
    wf = _wf_for_gt_multiple(sig, ret, 1.02)      # bar 2% above what it achieved
    out = P.recent_decay_status(ret, sig, wf_score=wf)
    assert out['recent_gt'] < out['min_gt']       # it did fail the raw test
    assert out['status'] == 'INSUFFICIENT'        # ...but is reported as a near miss
    assert out['kelly_scale'] == 1.0              # so no haircut
    assert 'near-miss' in out['note']


def test_clear_decay_is_still_decayed():
    """The near-miss band must not rescue a sleeve that genuinely fell apart."""
    sig = make_sig(years=2.0, entries_per_year=60)
    ret = make_ret(sig)
    wf = _wf_for_gt_multiple(sig, ret, 3.0)       # far out of reach
    out = P.recent_decay_status(ret, sig, wf_score=wf)
    assert out['status'] == 'DECAYED'
    assert out['kelly_scale'] == P.DECAY_KELLY_SCALE
    assert out['note'] is None


def test_negative_return_never_counts_as_near_miss():
    """Losing money is decay however close the GT lands."""
    sig = make_sig(years=2.0, entries_per_year=60)
    ret = make_ret(sig, drift=-0.0008)
    out = P.recent_decay_status(ret, sig, wf_score=0.001)
    assert out['recent_ret'] <= 0
    assert out['status'] == 'DECAYED'
    assert out['kelly_scale'] == P.DECAY_KELLY_SCALE
