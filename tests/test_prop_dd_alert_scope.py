"""PROP_DD_ALERT must silence one agent without silencing the prop account.

The OANDA book is incubation-only but is scored against the same The5ers limits
as the funded cTrader account. On 2026-09-03 it sat at 90% of the static total
limit and re-sent "PROP DD WARN" every day, which is exactly the message the
funded account would use to say it is about to be disqualified. A per-agent
switch is only safe if it is per-agent: a default that drifts to OFF, or a
'breach' setting that also eats the breach message, would turn the one alert
that matters into silence.
"""
import pytest

import prop_guard


@pytest.fixture
def sent(monkeypatch, tmp_path):
    """Capture notify_html calls; keep alert de-dup state in a scratch file."""
    monkeypatch.setattr(prop_guard, "STATE_FILE", str(tmp_path / "state.json"))
    out = []
    import telegram_bot
    monkeypatch.setattr(telegram_bot, "notify_html", lambda msg, *a, **k: out.append(msg))
    return out


def _metrics(daily=0.0, total=-0.090):
    return {
        "daily_dd_worst": daily,
        "daily_dd_now": daily,
        "total_dd_now": total,
        "nav": 92_840.0,
    }


def test_default_still_warns(sent, monkeypatch):
    """Unset env = the behaviour every existing agent already relies on."""
    monkeypatch.setattr(prop_guard, "ALERT_MODE", "1")
    prop_guard._maybe_alert(_metrics())
    assert len(sent) == 1 and "WARN" in sent[0]


@pytest.mark.parametrize("mode", ["0", "off", "none", "false"])
def test_off_silences_warn_and_breach(sent, monkeypatch, mode):
    monkeypatch.setattr(prop_guard, "ALERT_MODE", mode)
    prop_guard._maybe_alert(_metrics())
    prop_guard._maybe_alert(_metrics(total=-0.11))
    assert sent == []


def test_breach_only_drops_the_warn_but_keeps_the_breach(sent, monkeypatch):
    """The half-measure must not eat the message it exists to preserve."""
    monkeypatch.setattr(prop_guard, "ALERT_MODE", "breach")
    prop_guard._maybe_alert(_metrics(total=-0.090))
    assert sent == []
    prop_guard._maybe_alert(_metrics(total=-0.11))
    assert len(sent) == 1 and "BREACH" in sent[0]


def test_the_switch_is_read_from_the_environment_not_hard_coded(monkeypatch):
    """A constant that ignores the env would silence every agent at once."""
    import importlib
    monkeypatch.setenv("PROP_DD_ALERT", "0")
    assert importlib.reload(prop_guard).ALERT_MODE == "0"
    monkeypatch.delenv("PROP_DD_ALERT")
    assert importlib.reload(prop_guard).ALERT_MODE == "1"
