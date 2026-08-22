# Task: pytest suite — session gate + deferred QUEUE (part 1 of 2)

Repo: /Users/lich/deepseek-oanda-trading   Run from repo root. Use ./venv/bin/python

Write ONE new file `tests/test_deferred_queue.py`. Modify NOTHING else.
Do NOT edit `fix_runner.py`. If a test fails, REPORT it — never "fix" the source.

A sibling task owns the drain tests. Stay out of `tests/test_deferred_drain.py`.

## Start from this harness — paste it verbatim at the top of your file

It is already verified working. Use it as-is; do not redesign it.

# ── paste this harness in; it is already verified working ──────────────────
import os, tempfile, pytest
from datetime import datetime, timezone
import fix_runner as F
import prop_guard as PG

# AU200_AUD real schedule: Mon-Fri 02:50-09:29 and 10:10-23:59 Europe/Bucharest,
# as seconds from Sunday 00:00. Weekday index 1..5 == Mon..Fri.
AU200 = ([(86400*w + 2*3600+50*60, 86400*w + 9*3600+29*60) for w in range(1, 6)] +
         [(86400*w + 10*3600+10*60, 86400*w + 23*3600+59*60) for w in range(1, 6)])
NAS100 = [(86400*w + 5*60, 86400*w + 23*3600+55*60) for w in range(1, 6)]
ALWAYS_OPEN = [(86400*w, 86400*w + 86399) for w in range(0, 7)]

SUMMER_PASS = datetime(2026, 8, 19, 0, 15, tzinfo=timezone.utc)   # AU200 OPEN
WINTER_PASS = datetime(2026, 12, 16, 0, 15, tzinfo=timezone.utc)  # AU200 SHUT
DRAIN_NOW   = datetime(2026, 12, 16, 3, 0, tzinfo=timezone.utc)   # after it opens

def broker_day(dt):
    return PG.broker_now(dt).strftime('%Y-%m-%d')

class FakeAdapter:
    """Records every broker call in .sent so a test can assert ZERO calls."""
    def __init__(self, intervals=ALWAYS_OPEN, open_ids=(), sent=None,
                 fill=True, cancel_ok=True, close_ok=True):
        self.intervals, self.ids = intervals, set(open_ids)
        self.sent = sent if sent is not None else []
        self.fill, self.cancel_ok, self.close_ok = fill, cancel_ok, close_ok
    def session_intervals(self):  return self.intervals
    def open_pos_ids(self):       return self.ids
    def execute_order(self, signed_units, tag):
        self.sent.append(('open', signed_units, tag))
        return 'POS1' if self.fill else None
    def place_stop(self, pos_id, units, side, px):
        self.sent.append(('stop', pos_id, px))
        # MUST be this exact shape: fix_runner._stop_ok() accepts ONLY a dict
        # with ord_status == '0'. A bare string reads as failure and retries.
        return {'ord_status': '0', 'ref': str(pos_id)}
    def cancel_stop(self, ref, side):
        self.sent.append(('cancel', ref))
        return True if self.cancel_ok else None
    def close_position(self, pos_id, units, side):
        self.sent.append(('close', pos_id, units))
        return {'ord_status': '0'} if self.close_ok else None

class FakePrice:
    def get_current_price(self): return 9000.0

def make_adapters(ad, inst='AU200_AUD', equity=100000.0):
    return {'fix': {inst: ad}, 'price': {inst: FakePrice()},
            'equity': (lambda: equity)}

SLEEVES = [{'sid': 's1', 'inst': 'AU200_AUD', 'params': {'stop_mult': 1.5}}]

def intent(now, day=None, **over):
    e = {'kind': 'open', 'inst': 'AU200_AUD', 'signal': 1, 'prev_signal': 0,
         'units': 2.0, 'stop_mult': 1.5, 'atr': 50.0, 'pos_id': None,
         'stop_ref': None, 'side': None, 'held_units': None,
         'broker_day': day or broker_day(now), 'created': now.isoformat()}
    e.update(over)
    return e

@pytest.fixture
def env(tmp_path, monkeypatch):
    """Isolates every module global the mechanism touches, and RESTORES them."""
    monkeypatch.setattr(F, 'DEFER_FILE', str(tmp_path / 'deferred_actions.json'))
    monkeypatch.setattr(F, 'STATE_FILE', str(tmp_path / 'state.json'))
    monkeypatch.setattr(F, 'DEFER_SHUT_MARKET', True)
    monkeypatch.setattr(F, 'GUARD_ENABLED', True)
    monkeypatch.setattr(F, '_read_halt', lambda: {})
    monkeypatch.setattr(F, 'halt_is_active', lambda h, t: False)
    F._SESSION_CACHE.clear()      # caches per PROCESS — leaks between tests
    yield F
    F._SESSION_CACHE.clear()
# ───────────────────────────────────────────────────────────────────────────

## Functions under test (all in `fix_runner`)

- `market_shut(inst, adapter, now)` -> True / False / None. None means the schedule
  was unreadable and the caller must proceed exactly as before.
- `session_end(now, intervals, tzname='Europe/Bucharest')` -> UTC datetime, or None
  when shut.
- `_read_deferred()` / `_write_deferred(q)` / `clear_deferred(sid, q=None)`
- `defer_action(sid, inst, kind, sig, prev_sig, units, stop_mult, atr, st, now, broker_day)`
- `FLAT` is `lambda sig=0: {'signal':sig,'pos_id':None,'units':0.0,'side':0,'stop':None,'stop_ref':None}`

## Write exactly these 6 tests

1. `market_shut('AU200_AUD', FakeAdapter(AU200), SUMMER_PASS)` is **False** —
   AU200 is open at the pass in EU summer.
2. `market_shut('AU200_AUD', FakeAdapter(AU200), WINTER_PASS)` is **True** — shut
   at the pass in EU winter. This is the seasonal bug the mechanism exists for.
3. `market_shut` returns **None** when `session_intervals()` returns `[]`.
4. `defer_action(...)` then `_read_deferred()` round-trips: the record is keyed by
   sleeve id and carries `units`, `stop_mult`, `atr`, `broker_day`. Assert
   `'stop' not in record` — the stop price is deliberately derived at fill time,
   never carried, and a test must pin that.
5. Supersession: calling `defer_action` twice for the SAME sid leaves exactly ONE
   entry, holding the second call's values — it must not stack.
6. `clear_deferred(sid, q)` removes the entry.

Remember `F._SESSION_CACHE.clear()` between any two tests that use different
schedules for the same instrument — the `env` fixture already does this, so take
the fixture in every test that touches `market_shut`.

## Evidence required — paste the REAL output of both

```
./venv/bin/python -m pytest tests/test_deferred_queue.py -q
./venv/bin/python -m pytest -q
```

Then write a 10-line summary to `.scratch/defer/n3a-queue.md`.

End your answer with exactly one line:
VERDICT: PASS or VERDICT: FAIL — <reason>

PASS only if BOTH commands exited 0.
