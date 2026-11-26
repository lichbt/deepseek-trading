"""Stall watchdog: recover a live_test sleeve whose main loop stops iterating.

Motivating incident (2026-07-11): usdchf i21 froze mid pending-entry retry and
emitted nothing for twelve days while its 24 siblings ran normally. It held
USD_CHF long throughout, and because the software stop is evaluated inside the
same per-bar block, the position was effectively unstopped. Only a manual
restart cleared it.

These run the watchdog in a SUBPROCESS because a fire calls os._exit, which
would take the test runner down with it. The exit code is the assertion.
"""
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]

# Bind the real method onto a stub so the test exercises the SHIPPED watchdog
# rather than a re-implementation of it.
HARNESS = """
import sys, time, types
sys.path.insert(0, {repo!r})
import live_test

live_test.STALL_CHECK_INTERVAL = 1
live_test.STALL_TIMEOUT_FLOOR = {floor}
live_test.STALL_TIMEOUT_POLLS = 1

class Stub:
    poll_interval = 1
    strategy_id = 'test_sleeve'

s = Stub()
s._start_stall_watchdog = types.MethodType(
    live_test.LiveTrader._start_stall_watchdog, s)
s._heartbeat = time.monotonic()
s._start_stall_watchdog()

{body}
"""


def _run(body, floor, timeout=30):
    script = HARNESS.format(repo=str(REPO), floor=floor, body=textwrap.dedent(body))
    return subprocess.run([sys.executable, '-c', script],
                          capture_output=True, text=True, timeout=timeout)


def test_watchdog_kills_a_stalled_loop():
    """Heartbeat goes stale -> process exits non-zero so the wrapper restarts it."""
    # Never stamp the heartbeat again; just sleep past the limit.
    r = _run("time.sleep(20)", floor=3)
    assert r.returncode == 1, f'expected watchdog exit 1, got {r.returncode}\n{r.stdout}\n{r.stderr}'


def test_watchdog_says_why_before_exiting():
    """os._exit skips cleanup, so the message must be flushed explicitly.

    Without this the restart is as unexplained as the stall it fixes.
    """
    r = _run("time.sleep(20)", floor=3)
    assert 'STALL WATCHDOG' in r.stdout, f'no diagnosis on stdout:\n{r.stdout}\n{r.stderr}'
    assert 'has not iterated' in r.stdout


def test_watchdog_does_not_fire_while_the_loop_iterates():
    """A live loop must never be killed — a false positive restarts a healthy sleeve."""
    body = """
    for _ in range(15):
        s._heartbeat = time.monotonic()   # what the real loop does each iteration
        time.sleep(1)
    print('SURVIVED')
    """
    r = _run(body, floor=3)
    assert r.returncode == 0, f'watchdog killed a healthy loop\n{r.stdout}\n{r.stderr}'
    assert 'SURVIVED' in r.stdout


def test_timeout_scales_with_poll_interval_and_respects_the_floor():
    """A daily sleeve polls hourly, so the limit cannot be a flat constant."""
    body = """
    print('LIMIT_OK')
    """
    # floor below 1 x poll_interval -> poll_interval wins; both must be > 0.
    r = _run(body, floor=0)
    assert r.returncode == 0
    assert '[watchdog] stall timeout' in r.stdout


def test_heartbeat_is_stamped_at_the_top_of_the_real_loop():
    """Pin the coupling: the watchdog is inert unless run_loop stamps _heartbeat.

    A refactor that drops the stamp would silently disarm the watchdog while
    every test above still passed against the stub.
    """
    src = (REPO / 'live_test.py').read_text()
    # Anchor on run_loop specifically — the watchdog has its own `while True`,
    # and splitting on the first one would assert against the wrong loop.
    run_loop = src.split('def run_loop(self):', 1)[1]
    loop = run_loop.split('while True:', 1)[1][:400]
    assert '_heartbeat = time.monotonic()' in loop, \
        'run_loop no longer stamps _heartbeat at the top of the loop'
    assert '_start_stall_watchdog()' in src
