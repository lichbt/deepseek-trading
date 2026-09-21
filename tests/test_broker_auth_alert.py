"""BROKER_AUTH_REJECTED — the one exception to \"unreachable is not a breach\".

book_watch deliberately suppresses probe failures, and says why: alerting on a
flaky SSH round trip is how an alert gets muted. That rule cost three days on
2026-09-07, when the prop book sat dead on a revoked cTrader token while
GUARD_STALE fired twice into a table nobody reads.

The distinction these pin: UNREACHABLE stays silent, REFUSED shouts.
"""
import datetime
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / 'scripts'))
import book_watch as bw  # noqa: E402

NOW = datetime.datetime(2026, 9, 7, 12, 0, tzinfo=datetime.timezone.utc)

REVOKED = ('[cTrader] cTrader token refresh returned no access_token — NOT TRADING')
HEALTHY = ('[cTrader] authenticated account=48171893 env=live\n'
           '  [guard] ARMED — daily 3% / total 10%, sampling every 60s\n'
           '  [session] every sleeve\'s market is open at this pass')


def _fake_logs(monkeypatch, stdout, rc=0, boom=None):
    class _P:
        returncode = rc
        stdout = ''
    _P.stdout = stdout

    def fake_run(cmd, **kw):
        if boom:
            raise boom
        return _P
    monkeypatch.setattr(bw.subprocess if hasattr(bw, 'subprocess') else __import__('subprocess'),
                        'run', fake_run, raising=False)
    import subprocess
    monkeypatch.setattr(subprocess, 'run', fake_run)
    monkeypatch.setattr(bw.os.path, 'exists', lambda p: True)


def test_a_refused_token_produces_a_finding(monkeypatch):
    _fake_logs(monkeypatch, REVOKED)
    reason = bw.probe_broker_auth()
    assert reason and 'NOT TRADING' in reason
    f = bw.broker_auth_findings(reason, NOW)
    assert len(f) == 1
    code, sleeve, key, msg = f[0]
    assert code == bw.BROKER_AUTH_REJECTED
    assert 'manual OAuth re-auth' in msg


def test_a_healthy_log_is_silent(monkeypatch):
    _fake_logs(monkeypatch, HEALTHY)
    assert bw.probe_broker_auth() is None
    assert bw.broker_auth_findings(None, NOW) == []


@pytest.mark.parametrize('line', [
    '[cTrader] CH_ACCESS_TOKEN_INVALID: Invalid access token at connect',
    '[cTrader] OA_AUTH_TOKEN_EXPIRED: Access token has been expired',
    '[cTrader] cTrader auth failed: something',
    'RUNNER: NOT TRADING',
])
def test_every_rejection_shape_is_caught(monkeypatch, line):
    _fake_logs(monkeypatch, line)
    assert bw.probe_broker_auth(), line


def test_an_unreachable_probe_stays_silent(monkeypatch):
    """THE rule this must not break: a failed probe is not a finding."""
    _fake_logs(monkeypatch, '', boom=RuntimeError('ssh died'))
    assert bw.probe_broker_auth() is None


def test_a_missing_interlock_script_is_not_a_finding(monkeypatch):
    monkeypatch.setattr(bw.os.path, 'exists', lambda p: False)
    assert bw.probe_broker_auth() is None


def test_it_keys_on_the_day_so_it_nags(monkeypatch):
    """One alert you happen to miss must not restore the silence."""
    f1 = bw.broker_auth_findings('x', NOW)[0]
    f2 = bw.broker_auth_findings('x', NOW + datetime.timedelta(days=1))[0]
    assert f1[2] != f2[2], 'same key on a later day would dedup into silence'
    assert f1[2] == '2026-09-07'


def test_the_finding_says_what_to_actually_do(monkeypatch):
    msg = bw.broker_auth_findings('pod said x', NOW)[0][3]
    for cue in ('ctrader_auth.py', 'CTRADER_TOKENS', 'NOT '):
        assert cue in msg, cue
