"""book_watch's prop-breaker probe.

The breaker failed silently three times in two days and every one was found by
luck: ARMED-but-blind (get_price re-subscribing, sampled once per pod lifetime),
silently DISARMED by an unrelated dashboard env edit, and — because guard_tick
returns before it samples when unarmed — a state file that was frozen rather
than merely coarse. A dead breaker and a healthy one are indistinguishable from
outside, which is the same reason this script exists for sleeves.

These pin the decision layer. The remote probe itself is I/O and is exercised
against the live pod, not here.
"""
import os
import sys
from datetime import datetime, timedelta, timezone

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 'scripts'))
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from book_watch import GUARD_STALE, GUARD_UNARMED, guard_findings

NOW = datetime(2026, 8, 7, 12, 0, tzinfo=timezone.utc)


def _codes(findings):
    return sorted(c for c, _, _, _ in findings)


def _at(minutes_ago):
    return (NOW - timedelta(minutes=minutes_ago)).isoformat()


class TestHealthy:
    def test_armed_and_sampling_is_silent(self):
        assert guard_findings(True, _at(3), None, NOW) == []

    def test_a_long_trading_pass_is_not_a_stall(self):
        """The wait loop does not tick during a pass, and the FRED fetches alone
        run minutes. 25 min must stay quiet or the alert trains you to ignore it."""
        assert guard_findings(True, _at(25), None, NOW) == []


class TestDisarmed:
    def test_unarmed_alerts_even_while_sampling(self):
        """The 2026-08-07 case: the state file was fine, the flag was 0."""
        assert _codes(guard_findings(False, _at(3), None, NOW)) == [GUARD_UNARMED]

    def test_unarmed_and_frozen_reports_both(self):
        """Which is the real 2026-08-07 shape — guard_tick returns BEFORE
        sampling when unarmed, so the file freezes too. Two distinct problems,
        two distinct fixes, so they are not collapsed into one line."""
        assert _codes(guard_findings(False, _at(600), None, NOW)) == [
            GUARD_STALE, GUARD_UNARMED]

    def test_unarmed_keys_on_the_day_so_it_nags(self):
        """Daily, not once ever: someone has to go and set it, and silence is
        exactly what let the last one run a full day."""
        (_, _, key, _), = guard_findings(False, _at(3), None, NOW)
        assert key == '2026-08-07'


class TestStale:
    def test_frozen_state_alerts(self):
        assert _codes(guard_findings(True, _at(45), None, NOW)) == [GUARD_STALE]

    def test_stale_keys_on_the_frozen_timestamp_so_it_alerts_once(self):
        """A stalled guard keeps reporting the same last_updated, so the dedup
        row collides and the alert fires once per EPISODE, not every 4h forever."""
        frozen = _at(45)
        (_, _, key, _), = guard_findings(True, frozen, None, NOW)
        assert key == frozen

    def test_a_resumed_guard_gets_a_new_key(self):
        """New timestamp -> new row -> a second stall does alert again."""
        first = guard_findings(True, _at(45), None, NOW)[0][2]
        second = guard_findings(True, _at(90), None, NOW)[0][2]
        assert first != second

    def test_missing_state_file_is_a_stall_not_silence(self):
        """No file at all means it never sampled — or it is writing to ephemeral
        /app again, which is how start_nav re-based itself on every restart."""
        assert _codes(guard_findings(True, None, None, NOW)) == [GUARD_STALE]

    def test_naive_timestamp_is_read_as_utc(self):
        naive = (NOW - timedelta(minutes=45)).replace(tzinfo=None).isoformat()
        assert _codes(guard_findings(True, naive, None, NOW)) == [GUARD_STALE]

    def test_unparseable_timestamp_alerts_rather_than_passing(self):
        assert _codes(guard_findings(True, 'not-a-date', None, NOW)) == [GUARD_STALE]

    def test_threshold_is_configurable(self):
        assert guard_findings(True, _at(45), None, NOW, max_age=3600) == []
        assert _codes(guard_findings(True, _at(45), None, NOW, max_age=600)) == [
            GUARD_STALE]


class TestUnreachable:
    """A probe failure is not a breach, so it does not alert — only a crossed
    threshold does. It stays visible in the book_watch log (see main()); the
    trade-off accepted here is that a guard unreadable for days is not shouted
    about, and the log becomes the only thing separating "healthy" from "never
    actually checked"."""

    def test_probe_failure_does_not_alert(self):
        assert guard_findings(None, None, 'ssh: connect timed out', NOW) == []

    def test_probe_failure_never_claims_disarmed(self):
        """armed=None must not be read as False — crying DISARMED at a flaky
        network is how an alert gets muted."""
        assert GUARD_UNARMED not in _codes(
            guard_findings(None, None, 'ssh: connect timed out', NOW))

    def test_a_failure_cannot_be_masked_by_a_stale_looking_timestamp(self):
        """error wins over every other input: a half-read probe must not emit a
        stall it did not actually observe."""
        assert guard_findings(None, _at(600), 'ssh: connect timed out', NOW) == []
