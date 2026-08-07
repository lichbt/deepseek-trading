"""The static total-loss anchor must never chase equity.

The 10% total limit is measured from a FIXED anchor. If that anchor is re-seeded
from the current NAV on each sample, `total_dd_now` reads ~0.00% no matter how far
the account has fallen and the limit can never fire. Since the daily wall is
handled by the breaker, the total limit carries essentially all of the residual
risk of ruin — so an anchor that silently tracks equity is the single most
dangerous failure this file can have.
"""
import json

import pytest

import prop_guard


@pytest.fixture
def guard(tmp_path, monkeypatch):
    """prop_guard pointed at a scratch state file, with no network."""
    monkeypatch.setattr(prop_guard, "STATE_FILE", str(tmp_path / "state.json"))
    monkeypatch.setattr(prop_guard, "_fetch_balance", lambda: None)
    monkeypatch.setattr(prop_guard, "_fetch_account", lambda: (None, None))
    return prop_guard


def _drive(guard, navs, balance=None):
    """Feed a NAV sequence through update(), -> list of metrics dicts."""
    return [guard.update(nav=nav, balance=balance) for nav in navs]


# ---------------------------------------------------------------------------
# The defect
# ---------------------------------------------------------------------------

def test_rejected_start_balance_does_not_re_anchor_every_tick(guard, monkeypatch):
    """A REJECTED PROP_START_BALANCE must not turn into a moving anchor.

    _sane_start_balance falls back to NAV when the configured value cannot be
    true. That fallback is correct for SEEDING, but the self-heal branch then
    compared state against a `seed` that moves with NAV, so it rewrote start_nav
    on every sample. Equity fell 100k -> 97k -> 94k while total_dd_now stayed at
    0.00%.
    """
    monkeypatch.setattr(guard, "START_BALANCE", 2_500.0)   # stale FIX-era figure

    out = _drive(guard, [100_000.0, 97_000.0, 94_000.0])

    anchors = [m["start_nav"] for m in out]
    assert anchors[0] == anchors[1] == anchors[2], (
        "start_nav moved with NAV: %s" % anchors)
    # And the drawdown must actually be visible.
    assert out[-1]["total_dd_now"] == pytest.approx(-0.06, abs=1e-9)


def test_rejected_start_balance_still_reaches_the_total_limit(guard, monkeypatch):
    """The whole point: a 10% fall must be reportable as a 10% fall."""
    monkeypatch.setattr(guard, "START_BALANCE", 2_500.0)
    out = _drive(guard, [100_000.0, 95_000.0, 90_000.0])
    assert out[-1]["total_dd_now"] <= -0.10 + 1e-9


# ---------------------------------------------------------------------------
# The behaviour the self-heal exists for must survive the fix
# ---------------------------------------------------------------------------

def test_accepted_start_balance_still_self_heals_stale_state(guard, monkeypatch, tmp_path):
    """State seeded before the anchor was configured must still be corrected."""
    state = tmp_path / "state.json"
    state.write_text(json.dumps({
        "peak_nav": 99_000.0, "start_nav": 99_000.0, "day": "1970-01-01",
        "day_anchor_nav": 99_000.0, "day_low_nav": 99_000.0,
        "max_total_dd": 0.0, "worst_daily_dd": 0.0}))
    monkeypatch.setattr(guard, "STATE_FILE", str(state))
    monkeypatch.setattr(guard, "START_BALANCE", 100_000.0)   # credible vs NAV

    m = guard.update(nav=99_000.0, balance=99_000.0)
    assert m["start_nav"] == 100_000.0, "configured anchor was not applied"


def test_accepted_start_balance_is_the_anchor_not_the_nav(guard, monkeypatch):
    monkeypatch.setattr(guard, "START_BALANCE", 100_000.0)
    out = _drive(guard, [100_000.0, 96_000.0])
    assert out[-1]["start_nav"] == 100_000.0
    assert out[-1]["total_dd_now"] == pytest.approx(-0.04)


def test_unconfigured_anchor_seeds_once_and_then_holds(guard, monkeypatch):
    """PROP_START_BALANCE unset keeps the documented behaviour: seed from the
    first NAV observed, then never move."""
    monkeypatch.setattr(guard, "START_BALANCE", None)
    out = _drive(guard, [100_000.0, 97_000.0, 94_000.0])
    assert {m["start_nav"] for m in out} == {100_000.0}
    assert out[-1]["total_dd_now"] == pytest.approx(-0.06)


def test_anchor_survives_a_restart(guard, monkeypatch):
    """The anchor is persisted, so a process restart must not re-seed it."""
    monkeypatch.setattr(guard, "START_BALANCE", None)
    guard.update(nav=100_000.0, balance=100_000.0)
    # A restart is just another update() against the same state file.
    m = guard.update(nav=93_000.0, balance=93_000.0)
    assert m["start_nav"] == 100_000.0
    assert m["total_dd_now"] == pytest.approx(-0.07)
