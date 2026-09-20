"""Tests for scripts/token_budget.py — the gate that stops the research loop
before it blows the subscription's token cap.

Contract: exit 0 = clear to run, exit 1 = hold. A hold comes from either the
rolling token cap or the local-time run window.
"""
import json
import os
import sys
from datetime import datetime, timedelta, timezone

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(__file__)), 'scripts'))
import token_budget as tb


def _write(path, records):
    path.write_text('\n'.join(json.dumps(r) for r in records) + '\n')


def _rec(ts, total=1000):
    return {'ts': ts.isoformat(), 'stage': 'codegen', 'total_tokens': total}


class TestRollingWindow:
    def test_only_counts_records_inside_the_window(self, tmp_path):
        now = datetime.now(timezone.utc)
        log = tmp_path / 'u.jsonl'
        _write(log, [
            _rec(now - timedelta(days=1), 100),
            _rec(now - timedelta(days=6), 200),
            _rec(now - timedelta(days=8), 9999),   # older than the 7d window
        ])
        used, calls, _cost, _unp = tb.tokens_since(log, now - timedelta(days=7))
        assert used == 300
        assert calls == 2

    def test_unmetered_call_counts_as_a_call_not_as_absent(self, tmp_path):
        """A call with no token counters still happened — it must not vanish."""
        now = datetime.now(timezone.utc)
        log = tmp_path / 'u.jsonl'
        _write(log, [_rec(now, 500), {'ts': now.isoformat(), 'stage': 'codegen'}])
        used, calls, _cost, _unp = tb.tokens_since(log, now - timedelta(days=7))
        assert used == 500
        assert calls == 2

    def test_malformed_lines_are_skipped_not_fatal(self, tmp_path):
        now = datetime.now(timezone.utc)
        log = tmp_path / 'u.jsonl'
        log.write_text(json.dumps(_rec(now, 42)) + '\n{not json\n\n')
        used, calls, _cost, _unp = tb.tokens_since(log, now - timedelta(days=7))
        assert (used, calls) == (42, 1)

    def test_missing_log_reads_zero(self, tmp_path):
        used, calls, _cost, _unp = tb.tokens_since(tmp_path / 'nope.jsonl',
                                      datetime.now(timezone.utc) - timedelta(days=7))
        assert (used, calls) == (0, 0)


class TestRunWindow:
    @pytest.mark.parametrize('window,hhmm,expected', [
        ('06:00-12:00', '08:30', True),
        ('06:00-12:00', '12:00', False),    # end is exclusive
        ('06:00-12:00', '06:00', True),     # start is inclusive
        ('06:00-12:00', '05:59', False),
        ('22:00-04:00', '23:30', True),     # wraps midnight
        ('22:00-04:00', '03:59', True),
        ('22:00-04:00', '12:00', False),
    ])
    def test_window_boundaries(self, window, hhmm, expected):
        now = datetime.strptime(hhmm, '%H:%M')
        assert tb.in_run_window(window, now)[0] is expected

    def test_empty_window_is_always_open(self):
        assert tb.in_run_window('', datetime.now())[0] is True

    def test_unparseable_window_fails_OPEN(self):
        """A typo'd window must not silently stop all research forever."""
        assert tb.in_run_window('not-a-window', datetime.now())[0] is True


class TestExitCodes:
    def test_holds_when_cap_reached(self, tmp_path, capsys):
        now = datetime.now(timezone.utc)
        log = tmp_path / 'u.jsonl'
        _write(log, [_rec(now, 5000)])
        assert tb.main(['--log', str(log), '--cap', '4000', '--cap-usd', '0']) == 1
        assert 'HOLD' in capsys.readouterr().out

    def test_clear_when_under_cap(self, tmp_path, capsys):
        now = datetime.now(timezone.utc)
        log = tmp_path / 'u.jsonl'
        _write(log, [_rec(now, 100)])
        assert tb.main(['--log', str(log), '--cap', '4000', '--cap-usd', '0', '--run-window', '']) == 0
        assert 'OK' in capsys.readouterr().out

    def test_zero_cap_disables_the_cap(self, tmp_path):
        now = datetime.now(timezone.utc)
        log = tmp_path / 'u.jsonl'
        _write(log, [_rec(now, 10 ** 9)])
        assert tb.main(['--log', str(log), '--cap', '0', '--cap-usd', '0', '--run-window', '']) == 0

    def test_status_never_holds(self, tmp_path, capsys):
        now = datetime.now(timezone.utc)
        log = tmp_path / 'u.jsonl'
        _write(log, [_rec(now, 5000)])
        assert tb.main(['--log', str(log), '--cap', '4000', '--cap-usd', '0', '--status']) == 0


class TestFailsSafeWithoutEnv:
    """A wiped .env must not silently disable the gate.

    Regression: on 2026-08-22 a budget block appended to .env was rewritten away
    within the hour by something outside this repo. With the cap living only in
    .env, that turned the gate into a no-op — the loop ran unthrottled and
    nothing said so. Defaults now live in tracked code.
    """

    def test_defaults_are_real_values_not_disabled(self):
        assert tb.DEFAULT_CAP_USD > 0, 'a zero default cost cap disables the gate'
        assert tb.DEFAULT_CAP > 0, 'a zero default token cap disables the fallback'
        assert tb.DEFAULT_RUN_WINDOW, 'an empty default window leaves the loop always-open'
        # Mid-afternoon is outside the night band under any reading of it.
        assert tb.in_run_window(tb.DEFAULT_RUN_WINDOW, datetime(2026, 1, 1, 14, 0))[0] is False

    def test_default_window_sits_inside_the_discount_band(self):
        """Every hour the window runs must be inside DeepSeek's off-peak band.

        The window may be NARROWER than the band but never wider, because an hour
        outside bills at 2x and would silently double the run. DeepSeek's band is
        now UTC peak (01-04 & 06-10 UTC, Mon-Fri) with everything else off-peak,
        so `is_offpeak` converts LOCAL window times to UTC before checking. Assert
        containment: local window hours -> aware local datetimes -> off-peak.
        """
        import llm_prices as lp
        local_tz = datetime.now(timezone.utc).astimezone().tzinfo
        ran = [h for h in range(24)
               if tb.in_run_window(tb.DEFAULT_RUN_WINDOW, datetime(2026, 1, 1, h, 30))[0]]
        assert ran, 'default window matches no hour — the loop would never run'
        for hour in ran:
            # Aware LOCAL datetime; is_offpeak converts to UTC and must still
            # find the hour off-peak.
            local_dt = datetime(2026, 1, 1, hour, 0, tzinfo=local_tz)
            assert lp.is_offpeak(local_dt), hour

    def test_cap_binds_when_env_is_absent(self, tmp_path, monkeypatch, capsys):
        monkeypatch.delenv('LLM_TOKEN_CAP', raising=False)
        monkeypatch.delenv('LLM_CAP_DAYS', raising=False)
        now = datetime.now(timezone.utc)
        log = tmp_path / 'u.jsonl'
        _write(log, [_rec(now, tb.DEFAULT_CAP + 1)])
        # Window pinned open so this asserts the CAP, not the window.
        assert tb.main(['--log', str(log), '--cap-usd', '0', '--run-window', '']) == 1
        assert 'cap reached' in capsys.readouterr().out


class TestUnpricedModelsCannotReadAsFree:
    """A model missing from llm_prices.PRICES must not be billed at zero.

    Regression risk: cost_of returns None for an unknown model. Summing that as
    0.0 would let a whole stage run free in the guard's eyes and blow the plan
    while the report showed headroom.
    """

    def test_unpriced_tokens_are_reported(self, tmp_path):
        now = datetime.now(timezone.utc)
        log = tmp_path / 'u.jsonl'
        rec = {'ts': now.isoformat(), 'stage': 'critique', 'served': 'not-a-real-model',
               'prompt_tokens': 1000, 'completion_tokens': 100, 'total_tokens': 1100}
        _write(log, [rec])
        used, calls, cost, unpriced = tb.tokens_since(log, now - timedelta(days=7))
        assert used == 1100
        assert cost == 0.0
        assert unpriced == 1100, 'unpriced tokens must be surfaced, not silently free'

    def test_priced_model_is_not_flagged(self, tmp_path):
        now = datetime.now(timezone.utc)
        log = tmp_path / 'u.jsonl'
        rec = {'ts': now.isoformat(), 'stage': 'codegen', 'served': 'deepseek-v4-pro-0813',
               'prompt_tokens': 1000, 'completion_tokens': 100, 'total_tokens': 1100}
        _write(log, [rec])
        _used, _calls, cost, unpriced = tb.tokens_since(log, now - timedelta(days=7))
        assert cost > 0
        assert unpriced == 0

class TestWeekendFullDay:
    """WEEKEND_FULL_DAY=1 opens the gate around the clock on Sat+Sun.

    Default is OFF, so a weekend without the flag keeps the normal night window.
    """

    def test_off_by_default(self, monkeypatch):
        monkeypatch.delenv('WEEKEND_FULL_DAY', raising=False)
        assert tb.weekend_full_day(datetime(2026, 1, 3, 14, 0)) is False   # Saturday

    def test_saturday_and_sunday_open(self, monkeypatch):
        monkeypatch.setenv('WEEKEND_FULL_DAY', '1')
        assert tb.weekend_full_day(datetime(2026, 1, 3, 14, 0)) is True    # Saturday
        assert tb.weekend_full_day(datetime(2026, 1, 4, 14, 0)) is True    # Sunday

    def test_weekdays_stay_on_the_normal_window(self, monkeypatch):
        monkeypatch.setenv('WEEKEND_FULL_DAY', '1')
        assert tb.weekend_full_day(datetime(2026, 1, 2, 14, 0)) is False   # Friday
        assert tb.weekend_full_day(datetime(2026, 1, 5, 14, 0)) is False   # Monday

    def test_accepts_true_like_values_only(self, monkeypatch):
        for v in ('1', 'true', 'YES', 'on'):
            monkeypatch.setenv('WEEKEND_FULL_DAY', v)
            assert tb.weekend_full_day(datetime(2026, 1, 3, 14, 0)) is True, v
        for v in ('0', 'false', 'no', 'off', ''):
            monkeypatch.setenv('WEEKEND_FULL_DAY', v)
            assert tb.weekend_full_day(datetime(2026, 1, 3, 14, 0)) is False, v
