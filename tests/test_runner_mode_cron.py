"""RUNNER_MODE=cron — the runner must never start a trading pass by itself.

The property under test is the whole reason the mode exists: booting a pod places no
orders, so `git push` stops being a trading action. Four pushes on 2026-07-27 were four
unintended live passes, because main()'s loop sets first=True and takes the
full-entries-and-exits branch on its first iteration regardless of the clock.

Also covers the stop-attach verification. place_stop returns
{'ord_status':'8','reject':...} on failure, which is TRUTHY, so `if ref` logged
"stop@broker OK" for a REJECTED stop and stored the reject payload as stop_ref — a dict
carrying neither 'order_id' nor 'ref', which makes cancel_stop return None and the runner
then refuse to ever close that position.
"""
import importlib
import json
import os

import pytest


class _Break(Exception):
    """Escape the resident loop after one iteration.

    A dedicated exception, not StopIteration: PEP 479 turns a StopIteration raised
    inside a generator into RuntimeError, which would mask what the test asserts.
    """


def _break_sleep(_secs):
    raise _Break()


@pytest.fixture
def runner(monkeypatch, tmp_path):
    """fix_runner reloaded with its state/trigger/receipt paths inside tmp_path."""
    monkeypatch.setenv('RUNNER_MODE', 'cron')
    monkeypatch.setenv('TRIGGER_POLL', '0')
    import fix_runner
    importlib.reload(fix_runner)
    monkeypatch.setattr(fix_runner, 'TRIGGER_FILE', str(tmp_path / 'trade_now'))
    monkeypatch.setattr(fix_runner, 'RECEIPT_FILE', str(tmp_path / 'last_pass.json'))
    yield fix_runner
    importlib.reload(fix_runner)


# ── the trigger contract ────────────────────────────────────────────────────

def test_no_trigger_means_no_pass(runner, monkeypatch):
    """The boot case. No trigger file -> run_once is never called."""
    calls = []
    monkeypatch.setattr(runner, 'run_once', lambda *a, **k: calls.append(k))
    monkeypatch.setattr(runner.time, 'sleep', _break_sleep)

    with pytest.raises(_Break):
        runner._run_triggered([], {}, True, None)

    assert calls == [], 'a pod boot must place no orders under RUNNER_MODE=cron'


def test_trigger_runs_a_full_pass_and_is_consumed(runner, monkeypatch):
    open(runner.TRIGGER_FILE, 'w').close()
    calls = []
    monkeypatch.setattr(runner, 'run_once', lambda *a, **k: calls.append(k.get('trade')))
    monkeypatch.setattr(runner, 'maybe_reconcile', lambda *a: None)
    monkeypatch.setattr(runner.time, 'sleep', _break_sleep)

    with pytest.raises(_Break):
        runner._run_triggered([], {}, True, None)

    assert calls == [True], 'the trigger must produce a FULL pass, not a stop-check'
    assert not os.path.exists(runner.TRIGGER_FILE), 'trigger must be consumed'


def test_trigger_is_consumed_before_the_pass_runs(runner, monkeypatch):
    """A pass that dies halfway must not leave the trigger to re-fire and re-enter."""
    open(runner.TRIGGER_FILE, 'w').close()
    seen = {}

    def boom(*a, **k):
        seen['trigger_gone'] = not os.path.exists(runner.TRIGGER_FILE)
        raise RuntimeError('broker exploded')

    monkeypatch.setattr(runner, 'run_once', boom)
    monkeypatch.setattr(runner, 'maybe_reconcile', lambda *a: None)
    monkeypatch.setattr(runner.time, 'sleep', _break_sleep)

    with pytest.raises(_Break):
        runner._run_triggered([], {}, True, None)

    assert seen['trigger_gone'], 'consume must happen BEFORE the pass'


def test_failed_pass_writes_a_receipt_and_does_not_kill_the_runner(runner, monkeypatch):
    open(runner.TRIGGER_FILE, 'w').close()
    monkeypatch.setattr(runner, 'run_once', lambda *a, **k: 1 / 0)
    monkeypatch.setattr(runner, 'maybe_reconcile', lambda *a: None)
    monkeypatch.setattr(runner.time, 'sleep', _break_sleep)

    with pytest.raises(_Break):          # _Break, NOT ZeroDivisionError
        runner._run_triggered([], {}, True, None)

    receipt = json.load(open(runner.RECEIPT_FILE))
    assert receipt['ok'] is False
    assert 'ZeroDivisionError' in receipt['error']


def test_successful_pass_receipt_records_ok(runner, monkeypatch):
    open(runner.TRIGGER_FILE, 'w').close()
    monkeypatch.setattr(runner, 'run_once', lambda *a, **k: None)
    monkeypatch.setattr(runner, 'maybe_reconcile', lambda *a: None)
    monkeypatch.setattr(runner.time, 'sleep', _break_sleep)

    with pytest.raises(_Break):
        runner._run_triggered([], {}, True, None)

    receipt = json.load(open(runner.RECEIPT_FILE))
    assert receipt['ok'] is True and receipt['error'] is None
    assert receipt['started'] and receipt['finished']


# ── stop-attach verification ────────────────────────────────────────────────

def test_stop_ok_rejects_the_reject_payload():
    import fix_runner
    assert fix_runner._stop_ok({'ord_status': '0', 'ref': '4387582'}) is True
    # The regression: truthy, but NOT attached.
    assert fix_runner._stop_ok({'ord_status': '8', 'reject': 'MARKET_CLOSED'}) is False
    assert fix_runner._stop_ok(None) is False
    assert fix_runner._stop_ok('4387582') is False


def test_reject_payload_is_never_stored_as_stop_ref():
    """stop_ref must be None on failure, so cancel_stop stays able to close the position."""
    import fix_runner
    from ctrader_exec import CTraderExecAdapter

    ad = object.__new__(CTraderExecAdapter)
    reject = {'ord_status': '8', 'reject': 'MARKET_CLOSED'}
    assert not fix_runner._stop_ok(reject)
    # Storing it would make cancel_stop return None -> the runner refuses to close, forever.
    assert ad.cancel_stop(reject) is None
