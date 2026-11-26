# Task: pytest suite — deferred_drain SAFETY CLAUSES (part 2 of 2)

Repo: /Users/lich/deepseek-oanda-trading   Run from repo root. Use ./venv/bin/python

Write ONE new file `tests/test_deferred_drain.py`. Modify NOTHING else.
Do NOT edit `fix_runner.py`. If a test fails, REPORT it — never "fix" the source.

A sibling task owns the queue tests. Stay out of `tests/test_deferred_queue.py`.

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

## Function under test

`fix_runner.deferred_drain(sleeves, state, adapters, live, now=None)`

It executes intents the daily pass could not send because the market was shut.
Call it as `F.deferred_drain(SLEEVES, state, make_adapters(ad), True, now=DRAIN_NOW)`.

EVERY test must assert BOTH:
  - the number of broker calls: `len(ad.sent)`
  - the resulting queue length: `len(F._read_deferred())`

Seed the queue with `F._write_deferred({'s1': intent(DRAIN_NOW, **overrides)})`.

## Write exactly these 9 tests

1. **Halt drops the queue.** `halt_is_active` -> True. Assert queue empty AND
   `ad.sent == []`. Nothing may be sent while the breaker is latched.
2. **Halt check raising HOLDS the queue.** Make `_read_halt` raise. Assert the
   queue still has 1 entry AND zero calls. This is fail-closed behaviour and is
   the most important test in this file — draining is the action that needs the
   guard's permission, so an unreadable guard must block it, not allow it.
3. **Supersession.** Intent with `broker_day='1999-01-01'` -> dropped, zero calls.
4. **Still shut.** Adapter with schedule `AU200` and `now=WINTER_PASS` -> intent
   kept (queue length 1), zero calls.
5. **Happy path.** Session open -> EXACTLY 2 calls (`open` then `stop`), queue
   empty, and `state['s1']` has `pos_id == 'POS1'`, `units == 2.0`, `side == 1`,
   and a non-None `stop_ref`.
6. **Double-send guard.** `state['s1']` holds `pos_id='POSX'` AND `'POSX'` is in
   `open_pos_ids()`, intent kind `'open'` -> intent dropped, ZERO calls. This
   prevents doubling size when the pass opened but died before writing state.
7. **Close whose position is already gone.** Intent
   `kind='close', pos_id='GONE', prev_signal=1, signal=0` with `open_pos_ids()`
   empty -> dropped, zero calls.
8. **Stop cancel unconfirmed aborts.** `FakeAdapter(..., cancel_ok=False)`, intent
   `kind='close', pos_id='P9', stop_ref='R', side=1, held_units=2.0` with
   `open_pos_ids=('P9',)`. Assert: exactly 1 call (the cancel), NO `close` call in
   `ad.sent`, `state['s1']['pos_id']` still `'P9'`, intent still queued. A stop
   outliving its position fires as a naked entry, so a close must never start
   unless the cancel is confirmed.
9. **Disabled is a no-op.** `DEFER_SHUT_MARKET = False` with a full queue ->
   zero calls and the queue untouched.

## Evidence required — paste the REAL output of both

```
./venv/bin/python -m pytest tests/test_deferred_drain.py -q
./venv/bin/python -m pytest -q
```

Then write a 12-line summary to `.scratch/defer/n3b-drain.md`.

End your answer with exactly one line:
VERDICT: PASS or VERDICT: FAIL — <reason>

PASS only if BOTH commands exited 0.
