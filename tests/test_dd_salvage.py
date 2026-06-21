"""Tests for the DD-blocked edge-vs-beta screener primitives (dd_salvage).

These are pure functions over signal/return series — no data fetch — so they
pin the logic that separates a genuine risk-controllable edge from directional
beta dressed up with a good Calmar.
"""
import numpy as np
import pandas as pd

import dd_salvage as S


def test_directionality_counts():
    sig = pd.Series([1, 1, 0, -1, 0])
    d = S.directionality(sig)
    assert d['long'] == 2 / 5 and d['short'] == 1 / 5 and d['flat'] == 2 / 5


def test_holding_entries_and_max_hold():
    # one long run of 3, one short run of 2 -> 2 entries, max_hold 3
    sig = pd.Series([0, 1, 1, 1, 0, -1, -1, 0])
    h = S.holding(sig)
    assert h['entries'] == 2 and h['max_hold'] == 3


def test_holding_flip_without_flat_counts_two_entries():
    sig = pd.Series([1, 1, -1, -1])      # long then flip short, never flat
    h = S.holding(sig)
    assert h['entries'] == 2 and h['max_hold'] == 4   # in-market the whole time


def _yearly(returns_by_year):
    idx = [pd.Timestamp(f"{y}-06-01") for y, _ in returns_by_year]
    return pd.Series([v for _, v in returns_by_year], index=idx)


def test_top2year_share_concentrated():
    rr = _yearly([(2020, 1.0), (2021, 0.5), (2022, 0.1), (2023, 0.1)])
    assert S.top2year_share(rr) > 0.80      # 2 bull years dominate -> beta


def test_top2year_share_spread():
    rr = _yearly([(y, 0.10) for y in range(2015, 2025)])   # even across 10 years
    assert S.top2year_share(rr) < 0.30


def test_top2year_share_no_positive_year_is_concentrated():
    rr = _yearly([(2020, -0.1), (2021, -0.2)])
    assert S.top2year_share(rr) == 1.0


# ---- the combined verdict ----

def _dirn(long, short):
    return {'long': long, 'short': short, 'flat': 1 - long - short}


def test_screen_passes_selective_one_sided_edge():
    # DE30-like: long-only but selective (30% in market), spread, exit works
    ok, reasons = S.screen(_dirn(0.30, 0.0), {'max_hold': 35}, top2=0.43,
                           n_bars=2893, tf='D', calmar=1.0, ho=0.4)
    assert ok and reasons == []


def test_screen_rejects_directional_beta():
    # mostly-long the instrument
    ok, reasons = S.screen(_dirn(0.74, 0.0), {'max_hold': 60}, top2=0.50,
                           n_bars=1700, tf='D', calmar=1.0, ho=0.4)
    assert not ok and any("beta" in r for r in reasons)


def test_screen_rejects_rally_concentration():
    ok, reasons = S.screen(_dirn(0.21, 0.02), {'max_hold': 32}, top2=0.74,
                           n_bars=1700, tf='D', calmar=0.9, ho=0.4)
    assert not ok and any("top2yr" in r for r in reasons)


def test_screen_rejects_broken_exit_always_in():
    # flat 5% -> always in market (the broken-& exit signature)
    ok, reasons = S.screen(_dirn(0.62, 0.33), {'max_hold': 1664}, top2=0.50,
                           n_bars=1758, tf='D', calmar=1.4, ho=0.4)
    assert not ok and any("always-in" in r for r in reasons)


def test_screen_rejects_low_calmar_and_zero_ho():
    ok, reasons = S.screen(_dirn(0.20, 0.10), {'max_hold': 10}, top2=0.30,
                           n_bars=1700, tf='D', calmar=0.2, ho=0.0)
    assert not ok and any("Calmar" in r for r in reasons) and any("HO" in r for r in reasons)
