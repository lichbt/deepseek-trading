"""BASE_RISK is the canonical base-risk knob; FIX_RISK stays a working alias.

Base risk is the only term in size_units() that scales the whole book uniformly —
everything else is per-sleeve (ws, kelly, corr, decay) or a clamp (MAXRISK). So the
precedence between the two names decides live position size, and getting it wrong is
silent: the book just trades at a magnitude nobody chose.

FIX_RISK must keep working because the Zeabur pod env is a hand-maintained list in the
dashboard, independent of .env, and the two drift without anything checking. Honouring
only BASE_RISK would have sized the live book off the code default the moment the
dashboard still said FIX_RISK.
"""
import importlib
import os

import pytest

RISK_VARS = ('BASE_RISK', 'FIX_RISK')


def _reload_with(monkeypatch, **env):
    """Re-import fix_runner under a controlled env and return its resolved RISK."""
    for var in RISK_VARS:
        monkeypatch.delenv(var, raising=False)
    for key, val in env.items():
        monkeypatch.setenv(key, val)
    import fix_runner
    importlib.reload(fix_runner)
    return fix_runner.RISK


@pytest.fixture(autouse=True)
def _restore_module():
    """Leave fix_runner reloaded under the real env, so ordering can't leak."""
    yield
    import fix_runner
    importlib.reload(fix_runner)


def test_base_risk_wins_over_fix_risk(monkeypatch):
    assert _reload_with(monkeypatch, BASE_RISK='0.004', FIX_RISK='0.005') == 0.004


def test_base_risk_alone(monkeypatch):
    assert _reload_with(monkeypatch, BASE_RISK='0.003') == 0.003


def test_fix_risk_alone_still_works(monkeypatch):
    """The Zeabur dashboard sets FIX_RISK today. Dropping it would resize the book."""
    assert _reload_with(monkeypatch, FIX_RISK='0.007') == 0.007


def test_empty_base_risk_falls_through_rather_than_zeroing(monkeypatch):
    """`BASE_RISK=` in .env must not resolve to 0.0 — that would size every trade to zero."""
    assert _reload_with(monkeypatch, BASE_RISK='', FIX_RISK='0.005') == 0.005


def test_resolved_risk_is_never_zero_or_negative(monkeypatch):
    risk = _reload_with(monkeypatch, BASE_RISK='0.005')
    assert 0 < risk < 0.05, 'base risk outside a sane band would silently resize the book'


def test_live_env_resolves_to_the_locked_sizing():
    """The binding decision: base 0.005, per-trade cap 0.02. Do not scale up."""
    import fix_runner
    importlib.reload(fix_runner)
    assert fix_runner.RISK == 0.005
    assert fix_runner.MAXRISK == 0.02
