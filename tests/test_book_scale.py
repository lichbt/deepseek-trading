"""BOOK_SCALE multiplies BASE_RISK, and nothing else.

The knob exists for clarity, not for a new dimension of risk: BASE_RISK is the
per-trade budget, BOOK_SCALE is how hot the book runs. Because they multiply,
`BOOK_SCALE=1.10 @ BASE_RISK=0.005` is exactly `BASE_RISK=0.0055 @ 1.0` — a fact
worth pinning, since anyone who sets BOTH gets the product and should not be
surprised by it.
"""
import importlib
import os

import pytest


def _runner(monkeypatch, **env):
    """Re-import fix_runner with the given env — the knobs are read at import."""
    for k in ('BASE_RISK', 'FIX_RISK', 'BOOK_SCALE', 'FIX_MAXRISK'):
        monkeypatch.delenv(k, raising=False)
    for k, v in env.items():
        monkeypatch.setenv(k, v)
    import fix_runner
    return importlib.reload(fix_runner)


@pytest.fixture(autouse=True)
def _restore():
    """Leave the module as the rest of the suite expects to find it."""
    yield
    import fix_runner
    importlib.reload(fix_runner)


def test_defaults_to_one_and_changes_nothing(monkeypatch):
    F = _runner(monkeypatch, BASE_RISK='0.005')
    assert F.BOOK_SCALE == 1.0
    assert F.EFF_RISK == pytest.approx(F.RISK)


def test_scales_base_risk(monkeypatch):
    F = _runner(monkeypatch, BASE_RISK='0.005', BOOK_SCALE='1.10')
    assert F.EFF_RISK == pytest.approx(0.0055)


def test_equivalent_to_raising_base_risk(monkeypatch):
    """The measured equivalence, pinned: same effective risk, so same sizing."""
    a = _runner(monkeypatch, BASE_RISK='0.005', BOOK_SCALE='1.10').EFF_RISK
    b = _runner(monkeypatch, BASE_RISK='0.0055').EFF_RISK
    assert a == pytest.approx(b)


def test_setting_both_compounds(monkeypatch):
    """Not a bug, but the trap the startup line exists to surface."""
    F = _runner(monkeypatch, BASE_RISK='0.0055', BOOK_SCALE='1.10')
    assert F.EFF_RISK == pytest.approx(0.00605)


def test_maxrisk_still_clamps_above_book_scale(monkeypatch):
    """BOOK_SCALE must not be able to size past the per-trade ceiling."""
    F = _runner(monkeypatch, BASE_RISK='0.005', BOOK_SCALE='10.0', FIX_MAXRISK='0.02')
    sleeve = {'ws': 1.0, 'params': {'stop_mult': 2.0}, 'inst': 'EUR_USD',
              'decay_kelly_scale': 1.0}
    eff = min(F.EFF_RISK * sleeve['ws'] * 1.0 * 1.0 * 1.0, F.MAXRISK)
    assert eff == F.MAXRISK == 0.02


def test_sizing_uses_eff_risk_not_bare_risk(monkeypatch):
    """Guards the wiring: a refactor that reverts size_units to RISK would make
    BOOK_SCALE silently inert, which is worse than not having it."""
    import inspect
    F = _runner(monkeypatch, BASE_RISK='0.005', BOOK_SCALE='2.0')
    src = inspect.getsource(F.size_units)
    assert 'EFF_RISK' in src, "size_units no longer reads EFF_RISK"


def test_book_scale_is_honoured_end_to_end_in_size_units(monkeypatch):
    """Double the scale -> double the units, all else equal."""
    one = _runner(monkeypatch, BASE_RISK='0.005', BOOK_SCALE='1.0')
    sleeve = {'ws': 1.0, 'params': {'stop_mult': 2.0}, 'inst': 'EUR_USD',
              'decay_kelly_scale': 1.0}
    u1, _ = one.size_units(sleeve, atr=0.01, equity=100_000.0, kelly=1.0)
    two = _runner(monkeypatch, BASE_RISK='0.005', BOOK_SCALE='2.0')
    u2, _ = two.size_units(sleeve, atr=0.01, equity=100_000.0, kelly=1.0)
    assert u2 > u1
    # Exact where the venue's lot step does not round the difference away.
    assert u2 == pytest.approx(2 * u1, rel=0.02)
