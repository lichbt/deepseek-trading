"""The 4-hourly prop-Zeabur health check must fail LOUDLY and never falsely pass.

The two failures this guards against, both of which have happened in this
project in other forms:

  * A green pod is not a trading pod. The runner is RUNNER_MODE=cron and places
    nothing on boot, so "pod Running" alone says nothing. The verdict has to come
    from the last_pass.json receipt, and a missing/failed/stale receipt must not
    be allowed to read as healthy.
  * A health checker must not report a clean check it never ran. An unreadable
    probe is UNKNOWN, never OK — the whole point of messaging every run is that
    silence and blindness are distinguishable.

The guard/auth verdicts are imported from book_watch, so these tests pin that the
import is wired and the thresholds are applied, not a re-implementation of them.
"""
import base64
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 'scripts'))
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import prop_health as ph  # noqa: E402

NOW = datetime(2026, 9, 16, 10, 0, tzinfo=timezone.utc)


def _b64(obj):
    return base64.b64encode(json.dumps(obj).encode()).decode()


def _probe(**over):
    """A healthy baseline snapshot, with fields overridable per test."""
    env = {
        'VENUE': 'ctrader', 'RUNNER_MODE': 'cron', 'TRIGGER_POLL': '60',
        'PROP_GUARD_HALT': '1', 'PROP_GUARD_EVERY': '1',
        'PROP_DAILY_DD_LIMIT': '0.03', 'PROP_TOTAL_DD_LIMIT': '0.10',
        'PROP_HALT_FRACTION': '0.80', 'PROP_START_BALANCE': '100000',
    }
    env.update(over.pop('env', {}))
    fields = {
        'HEALTH_NOW': str(int(NOW.timestamp())),
        'DEPLOY_DESIRED': '1', 'DEPLOY_READY': '1', 'DEPLOY_AVAILABLE': '1',
        'POD_JSON': 'service-abc-xyz|Running|0|2026-09-04T12:00:00Z;',
        'FIX_RUNNER_PROCS': '1',
        'TRADE_NOW_PRESENT': '0', 'TRADE_NOW_MTIME': '0',
        'OANDA_STATUS': 'OK', 'OANDA_LAST': '2026-09-16T09:00:00.000000000Z',
        'LAST_PASS_B64': _b64({'started': '2026-09-16T00:15:00Z',
                               'finished': '2026-09-16T00:19:00Z',
                               'ok': True, 'error': None}),
        'GUARD_FILE': 'prop_guard_state_ctrader.json',
        'GUARD_B64': _b64({'day': '2026-09-16', 'day_anchor_nav': 100000.0,
                           'day_low_nav': 99800.0, 'last_nav': 99900.0,
                           'max_total_dd': -0.004, 'peak_nav': 100100.0,
                           'start_nav': 99500.0,
                           'last_updated': '2026-09-16T09:59:00+00:00'}),
    }
    fields.update(over)
    lines = [f'{k}={v}' for k, v in fields.items() if k not in ('env', 'guards')]
    snap = ph.parse_probe('\n'.join(lines))
    snap['env'].update(env)
    return snap


def _by_name(checks, name):
    return next(c for c in checks if c.name == name)


# --- parsing ---------------------------------------------------------------

def test_parse_probe_reads_env_and_guard_pairs():
    snap = _probe()
    assert snap['env']['RUNNER_MODE'] == 'cron'
    assert snap['guards']['prop_guard_state_ctrader.json']['last_nav'] == 99900.0


def test_parse_probe_none_receipt_is_none():
    snap = _probe(LAST_PASS_B64='NONE')
    assert ph._b64json(snap['LAST_PASS_B64']) is None


def test_parse_probe_tolerates_a_corrupt_b64():
    snap = _probe(GUARD_B64='not-base64!!')
    assert snap['guards']['prop_guard_state_ctrader.json'] is None


# --- pod -------------------------------------------------------------------

def test_pod_running_is_ok():
    assert _by_name(ph.check_pod(_probe(), NOW), 'pod').status == ph.OK


def test_scaled_to_zero_is_warn_never_ok():
    """The interlock scales to 0 on purpose; that must not read as healthy."""
    c = _by_name(ph.check_pod(_probe(DEPLOY_DESIRED='0', DEPLOY_READY='0'), NOW), 'pod')
    assert c.status == ph.WARN
    assert 'not trading' in c.detail.lower() or 'NOT trading' in c.detail


def test_pod_not_running_fails():
    snap = _probe(DEPLOY_READY='0',
                  POD_JSON='service-abc-xyz|CrashLoopBackOff|5|2026-09-16T09:00:00Z;')
    assert _by_name(ph.check_pod(snap, NOW), 'pod').status == ph.FAIL


def test_repeated_restarts_warn():
    snap = _probe(POD_JSON='service-abc-xyz|Running|7|2026-09-04T12:00:00Z;')
    assert _by_name(ph.check_pod(snap, NOW), 'pod').status == ph.WARN


# --- runner ----------------------------------------------------------------

def test_no_runner_process_fails():
    assert _by_name(ph.check_runner(_probe(FIX_RUNNER_PROCS='0')), 'runner').status == ph.FAIL


def test_two_runners_fail():
    """Two runners on one broker account trade it blind to each other."""
    c = _by_name(ph.check_runner(_probe(FIX_RUNNER_PROCS='2')), 'runner')
    assert c.status == ph.FAIL and 'ONE' in c.detail


def test_one_runner_is_ok():
    assert _by_name(ph.check_runner(_probe()), 'runner').status == ph.OK


# --- pass receipt ----------------------------------------------------------

def test_fresh_ok_receipt_is_ok():
    assert _by_name(ph.check_pass(_probe(), NOW), 'pass').status == ph.OK


def test_failed_receipt_fails():
    snap = _probe(LAST_PASS_B64=_b64(
        {'started': '2026-09-16T00:15:00Z', 'finished': '2026-09-16T00:17:00Z',
         'ok': False, 'error': 'CTraderError("boom")'}))
    c = _by_name(ph.check_pass(snap, NOW), 'pass')
    assert c.status == ph.FAIL and 'FAILED' in c.detail


def test_stale_receipt_fails():
    snap = _probe(LAST_PASS_B64=_b64(
        {'started': '2026-09-14T00:15:00Z', 'finished': '2026-09-14T00:19:00Z',
         'ok': True, 'error': None}))
    assert _by_name(ph.check_pass(snap, NOW), 'pass').status == ph.FAIL


def test_missing_receipt_on_an_old_pod_fails():
    """No receipt after the pod has been up a while = no trigger ever consumed."""
    snap = _probe(LAST_PASS_B64='NONE')
    c = _by_name(ph.check_pass(snap, NOW), 'pass')
    assert c.status == ph.FAIL and 'NO pass receipt' in c.detail


def test_missing_receipt_on_a_young_pod_is_unknown():
    snap = _probe(LAST_PASS_B64='NONE',
                  POD_JSON='service-abc-xyz|Running|0|2026-09-16T09:30:00Z;')
    assert _by_name(ph.check_pass(snap, NOW), 'pass').status == ph.UNKNOWN


def test_stuck_trigger_fails():
    snap = _probe(TRADE_NOW_PRESENT='1',
                  TRADE_NOW_MTIME=str(int(NOW.timestamp()) - 3600))
    c = _by_name(ph.check_pass(snap, NOW), 'trigger')
    assert c.status == ph.FAIL and 'unconsumed' in c.detail


def test_non_cron_mode_pass_is_unknown_not_ok():
    """Without cron there is no receipt to age, so the heartbeat is inapplicable."""
    snap = _probe(env={'RUNNER_MODE': ''}, LAST_PASS_B64='NONE')
    assert _by_name(ph.check_pass(snap, NOW), 'pass').status == ph.UNKNOWN


# --- guard (reuses book_watch) ---------------------------------------------

def test_guard_armed_and_fresh_is_ok():
    assert _by_name(ph.check_guard(_probe(), NOW), 'guard').status == ph.OK


def test_guard_unarmed_fails():
    c = _by_name(ph.check_guard(_probe(env={'PROP_GUARD_HALT': '0'}), NOW), 'guard')
    assert c.status == ph.FAIL and 'DISARMED' in c.detail


def test_guard_stale_fails():
    snap = _probe(GUARD_B64=_b64({'last_updated': '2026-09-16T06:00:00+00:00'}))
    assert _by_name(ph.check_guard(snap, NOW), 'guard').status == ph.FAIL


def test_guard_missing_state_and_env_is_unknown():
    snap = _probe(env={'PROP_GUARD_HALT': 'UNSET'}, GUARD_B64='NONE')
    assert _by_name(ph.check_guard(snap, NOW), 'guard').status == ph.UNKNOWN


# --- drawdown limits -------------------------------------------------------

def test_daily_dd_breach_fails():
    snap = _probe(GUARD_B64=_b64({'day_anchor_nav': 100000.0, 'day_low_nav': 96000.0,
                                  'max_total_dd': -0.004}))
    assert _by_name(ph.check_limits(snap), 'daily DD').status == ph.FAIL


def test_daily_dd_near_the_halt_warns():
    # 2.5% against a 3% limit with PROP_HALT_FRACTION=0.80 -> halt at 2.4%.
    snap = _probe(GUARD_B64=_b64({'day_anchor_nav': 100000.0, 'day_low_nav': 97500.0,
                                  'max_total_dd': -0.004}))
    assert _by_name(ph.check_limits(snap), 'daily DD').status == ph.WARN


def test_total_dd_breach_fails():
    snap = _probe(GUARD_B64=_b64({'day_anchor_nav': 100000.0, 'day_low_nav': 99900.0,
                                  'max_total_dd': -0.12}))
    assert _by_name(ph.check_limits(snap), 'total DD').status == ph.FAIL


def test_guard_state_picks_the_pod_venue_file():
    """A stale other-venue file on the volume must never be read as this book's."""
    snap = ph.parse_probe('\n'.join([
        'ENV_VENUE=ctrader',
        'GUARD_FILE=prop_guard_state.json', 'GUARD_B64=' + _b64({'last_nav': 1.0}),
        'GUARD_FILE=prop_guard_state_ctrader.json', 'GUARD_B64=' + _b64({'last_nav': 2.0}),
    ]))
    assert ph.pick_guard_state(snap)['last_nav'] == 2.0


# --- auth (reuses book_watch markers, but distinguishes unreadable) ---------

def _script(tmp_path, body):
    p = Path(tmp_path) / 'fake_interlock.sh'
    p.write_text('#!/bin/bash\n' + body)
    return str(p)


def test_auth_rejection_fails(tmp_path):
    script = _script(tmp_path, 'echo "ctrader auth failed: CH_ACCESS_TOKEN_INVALID"\n')
    assert ph.check_auth(NOW, script=script)[0].status == ph.FAIL


def test_auth_clean_log_is_ok(tmp_path):
    script = _script(tmp_path, 'echo "pass complete, 22 sleeves"\n')
    assert ph.check_auth(NOW, script=script)[0].status == ph.OK


def test_unreadable_log_is_unknown_not_ok(tmp_path):
    """A clean-looking check that never ran is the failure mode this forbids."""
    script = _script(tmp_path, 'true\n')
    assert ph.check_auth(NOW, script=script)[0].status == ph.UNKNOWN


# --- oanda: the DATA source, a different dependency from cTrader execution ---

def test_oanda_reachable_is_ok():
    c = _by_name(ph.check_oanda(_probe(OANDA_STATUS='OK',
                                       OANDA_LAST='2026-09-16T09:00:00.000000000Z')), 'oanda (data)')
    assert c.status == ph.OK and '2026-09-16T09:00' in c.detail


def test_oanda_rejection_fails():
    """cTrader auth can be perfect while the price feed is dead."""
    snap = _probe(OANDA_STATUS='FAIL', OANDA_ERROR='HTTP 401 {"errorMessage":"Invalid token"}')
    c = _by_name(ph.check_oanda(snap), 'oanda (data)')
    assert c.status == ph.FAIL and 'Invalid token' in c.detail


def test_oanda_missing_token_fails():
    assert _by_name(ph.check_oanda(_probe(OANDA_STATUS='NOT_CONFIGURED')),
                    'oanda (data)').status == ph.FAIL


def test_oanda_empty_response_warns():
    assert _by_name(ph.check_oanda(_probe(OANDA_STATUS='EMPTY')),
                    'oanda (data)').status == ph.WARN


def test_oanda_unprobed_is_unknown_never_ok():
    assert _by_name(ph.check_oanda(_probe(OANDA_STATUS='UNREACHABLE')),
                    'oanda (data)').status == ph.UNKNOWN
    assert _by_name(ph.check_oanda(_probe(OANDA_STATUS='')),
                    'oanda (data)').status == ph.UNKNOWN


def test_exec_and_data_are_reported_separately():
    """The whole point: one can be green while the other is red."""
    snap = _probe(OANDA_STATUS='FAIL', OANDA_ERROR='HTTP 503')
    oanda = _by_name(ph.check_oanda(snap), 'oanda (data)')
    assert oanda.status == ph.FAIL


# --- rendering -------------------------------------------------------------

def test_render_counts_failures_and_escapes_html():
    checks = [ph.Check('pass', ph.FAIL, 'error: a < b & c'),
              ph.Check('pod', ph.OK, 'Running 1/1')]
    out = ph.render(checks, NOW)
    assert '1 FAILED' in out
    assert '&lt;' in out and '&amp;' in out
    assert '<b>pass</b>' in out          # our own tags survive


def test_probe_error_is_reported_as_unknown_not_silence():
    checks = ph.unknown_checks('could not read the pod')
    assert all(c.status == ph.UNKNOWN for c in checks)
    assert 'could not read the pod' in ph.render(checks, NOW)
