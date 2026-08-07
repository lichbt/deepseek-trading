"""WEIGHTING selects the allocation scheme, and 'equal' must mean equal.

Measured 2026-08-08 out of sample (weights fit on 2024-01..2025-04, scored on the
following sixteen months): equal weight returns ~45% more per unit of tail than
the live inverse-vol x conviction vector. The mechanism is anti-selection — the
pre-conviction layer gives LESS weight to higher-Sharpe sleeves (-0.382) while
conviction pushes the other way (+0.248), leaving a 25x spread that correlates
with sleeve quality at -0.058.

The knob defaults to the incumbent so the code change is inert until an env says
otherwise, and so rollback is unsetting a variable rather than reverting a commit.
"""
import importlib

import numpy as np
import pandas as pd
import pytest

import portfolio


@pytest.fixture
def book(monkeypatch):
    """Three sleeves with deliberately different vols, and no decay machinery."""
    monkeypatch.setattr(portfolio, "recent_decay_status",
                        lambda ret, sig, wf: {"status": "OK", "kelly_scale": 1.0})
    idx = pd.date_range("2024-01-01", periods=60, freq="D")
    rng = np.random.default_rng(7)
    return {
        "lowvol":  pd.Series(rng.normal(0, 0.001, 60), index=idx),
        "midvol":  pd.Series(rng.normal(0, 0.005, 60), index=idx),
        "highvol": pd.Series(rng.normal(0, 0.020, 60), index=idx),
    }


def _weights(monkeypatch, returns, scheme):
    monkeypatch.setattr(portfolio, "WEIGHTING", scheme)
    w, vols, decay = portfolio.inverse_vol_weights(returns)
    return w


# ---------------------------------------------------------------------------

def test_default_is_the_incumbent_scheme():
    """Importing the module must not change how the book is weighted."""
    importlib.reload(portfolio)
    assert portfolio.WEIGHTING == "invvol"


def test_invvol_still_favours_the_low_vol_sleeve(monkeypatch, book):
    w = _weights(monkeypatch, book, "invvol")
    assert w["lowvol"] > w["midvol"] > w["highvol"]


def test_equal_gives_every_sleeve_the_same_weight(monkeypatch, book):
    w = _weights(monkeypatch, book, "equal")
    assert len(set(round(x, 12) for x in w.values())) == 1
    assert sum(w.values()) == pytest.approx(1.0)


def test_equal_bypasses_conviction(monkeypatch, book):
    """Conviction trims exist to correct inverse-vol over-weighting (Family A) or
    react to trailing Sharpe (Family C, measured anti-predictive). Neither applies
    once the weights are flat."""
    monkeypatch.setitem(portfolio.CONVICTION, "lowvol", 0.13)
    w = _weights(monkeypatch, book, "equal")
    assert w["lowvol"] == pytest.approx(w["highvol"])


def test_invvol_still_honours_conviction(monkeypatch, book):
    """The incumbent path must be untouched by this change."""
    base = _weights(monkeypatch, book, "invvol")["lowvol"]
    monkeypatch.setitem(portfolio.CONVICTION, "lowvol", 0.13)
    assert _weights(monkeypatch, book, "invvol")["lowvol"] < base


# ---------------------------------------------------------------------------
# The guard that must survive the switch
# ---------------------------------------------------------------------------

def test_equal_keeps_the_no_history_sleeve_at_zero(monkeypatch, book):
    """A sleeve with under 5 in-position days has NaN vol and must stay at 0.

    Not academic: wheatusd_auto_20260630_155412_i15 has zero in-position days over
    the current 674-bar window. Naive equal weight would hand it a full share on no
    evidence at all.
    """
    idx = book["lowvol"].index
    book["untraded"] = pd.Series(0.0, index=idx)          # never in position
    w = _weights(monkeypatch, book, "equal")
    assert w["untraded"] == 0.0
    assert w["lowvol"] > 0.0


def test_equal_weights_still_sum_to_one_with_a_zeroed_sleeve(monkeypatch, book):
    """live_test treats weights that don't sum to 1.0 as an upstream bug and
    clamps on it (live_test.py:265), so the invariant must hold in both schemes."""
    book["untraded"] = pd.Series(0.0, index=book["lowvol"].index)
    for scheme in ("invvol", "equal"):
        assert sum(_weights(monkeypatch, book, scheme).values()) == pytest.approx(1.0)


def test_unknown_scheme_fails_loudly(monkeypatch, book):
    """A typo must not silently fall back to a different book magnitude."""
    with pytest.raises(ValueError, match="WEIGHTING"):
        _weights(monkeypatch, book, "equal-weight")


def test_weight_scale_of_an_equal_book_is_one(monkeypatch, book):
    """Consumers derive weight_scale = weight * n. Equal weight must land on
    exactly 1.0 so BOOK_SCALE is the only magnitude term."""
    w = _weights(monkeypatch, book, "equal")
    n = len(w)
    for sid in w:
        assert w[sid] * n == pytest.approx(1.0)
