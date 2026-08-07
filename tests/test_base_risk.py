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
ENV_PATH = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), '.env')


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


def test_fix_risk_is_retired_and_no_longer_sizes_anything(monkeypatch):
    """FIX_RISK is DEAD. This asserted the opposite until 2026-08-08.

    The alias existed only to cover the window where the Zeabur dashboard still
    said FIX_RISK while .env had moved to BASE_RISK. The pod now carries BASE_RISK
    and no FIX_RISK, so the alias is retired — and this test is inverted rather
    than deleted, so re-adding FIX_RISK to an env somewhere fails loudly here
    instead of silently doing nothing to the book.
    """
    assert _reload_with(monkeypatch, FIX_RISK='0.007') == 0.005   # code default, not 0.007


def test_empty_base_risk_falls_through_rather_than_zeroing(monkeypatch):
    """`BASE_RISK=` in .env must not resolve to 0.0 — that would size every trade to zero."""
    assert _reload_with(monkeypatch, BASE_RISK='') == 0.005


def test_resolved_risk_is_never_zero_or_negative(monkeypatch):
    risk = _reload_with(monkeypatch, BASE_RISK='0.005')
    assert 0 < risk < 0.05, 'base risk outside a sane band would silently resize the book'


def _dotenv_value(name):
    """Read `name` straight from the .env FILE, last assignment winning.

    Deliberately not os.getenv: fix_runner resolves from os.environ, which .env is
    loaded into transitively (fix_runner -> portfolio -> load_dotenv). Comparing
    os.getenv to os.getenv would be a tautology that passes even if the resolution
    order broke. Reading the file independently makes this an end-to-end check that
    what .env DECLARES is what the book actually trades.
    """
    found = None
    with open(ENV_PATH) as fh:
        for line in fh:
            line = line.strip()
            if line.startswith('#') or not line.startswith(name + '='):
                continue
            found = line.split('=', 1)[1].strip().strip('\'"') or None
    return found


def test_live_env_resolves_to_what_dotenv_declares():
    """Sizing is derived from .env, not pinned to a literal.

    This used to assert RISK == 0.005. Sizing moved to 0.002 on 2026-08-05 and the
    literal went stale, so the test sat permanently red — which is worse than no
    test, because a genuinely unintended resize could no longer be distinguished
    from the known failure. Deriving the expectation keeps it green across retunes
    while still catching the failure that matters: .env saying one thing and the
    live book resolving another.
    """
    # Import FIRST: .env reaches os.environ only as a side effect of this import
    # (fix_runner -> portfolio -> load_dotenv). Checking os.environ before it would
    # skip on a fresh process and hide the very mismatch this test exists to catch.
    import fix_runner
    importlib.reload(fix_runner)
    expected = _dotenv_value('BASE_RISK') or _dotenv_value('FIX_RISK')
    if expected is None:
        pytest.skip('.env declares neither BASE_RISK nor FIX_RISK')
    assert fix_runner.RISK == float(expected), (
        f'.env declares {expected} but fix_runner resolved {fix_runner.RISK}')
    # Still a hard literal: the per-trade cap is a binding decision, not a knob
    # that moves when base risk is retuned.
    assert fix_runner.MAXRISK == 0.02


def test_dotenv_no_longer_carries_the_retired_alias():
    """.env once held BOTH names DISAGREEING — BASE_RISK=0.002 vs FIX_RISK=0.005.

    A local runner therefore sized at 0.2% while the pod ran 0.5%, and deleting
    either line would have moved the book with nothing in the logs. The alias is
    retired and .env is down to one name; this keeps it that way.
    """
    assert _dotenv_value('FIX_RISK') is None, (
        '.env re-declares the retired FIX_RISK — fix_runner ignores it, so it is '
        'documentation that lies about live position size')


def test_resolved_risk_stays_inside_a_sane_band():
    """Derivation must not become a blank cheque: a fat-fingered .env still fails."""
    import fix_runner
    importlib.reload(fix_runner)
    assert 0 < fix_runner.RISK <= 0.02, (
        f'base risk {fix_runner.RISK} outside the sane band — check .env')
