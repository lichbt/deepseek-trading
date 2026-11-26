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


# 1. AU200 is OPEN at the summer pass (EU DST): 00:15 UTC == 03:15 Bucharest,
#    inside the 02:50-09:29 leg.
def test_market_open_at_summer_pass(env):
    F = env
    assert F.market_shut('AU200_AUD', FakeAdapter(AU200), SUMMER_PASS) is False


# 2. AU200 is SHUT at the winter pass (EU non-DST): 00:15 UTC == 02:15 Bucharest,
#    before the 02:50 open. This is the seasonal skew the mechanism exists for.
def test_market_shut_at_winter_pass(env):
    F = env
    assert F.market_shut('AU200_AUD', FakeAdapter(AU200), WINTER_PASS) is True


# 3. No schedule readable -> None, and the caller must proceed as before.
def test_market_shut_none_when_no_schedule(env):
    F = env
    assert F.market_shut('AU200_AUD', FakeAdapter([]), SUMMER_PASS) is None


# 4. defer_action round-trips through the queue file keyed by sleeve id, carrying
#    units/stop_mult/atr/broker_day. The stop PRICE is never stored — it is derived
#    at fill time, so a test must pin that 'stop' is absent from the record.
def test_defer_action_roundtrip(env):
    F = env
    now = SUMMER_PASS
    st = {'pos_id': None, 'stop_ref': None, 'side': None, 'units': 0.0}
    F.defer_action('s1', 'AU200_AUD', 'open', 1, 0, 2.0, 1.5, 50.0, st, now,
                   broker_day(now))
    q = F._read_deferred()
    assert 's1' in q
    rec = q['s1']
    assert rec['units'] == 2.0
    assert rec['stop_mult'] == 1.5
    assert rec['atr'] == 50.0
    assert rec['broker_day'] == broker_day(now)
    assert 'stop' not in rec


# 5. Supersession: a second defer for the SAME sleeve overwrites — never stacks.
def test_defer_action_supersedes_same_sid(env):
    F = env
    now = SUMMER_PASS
    st = {}
    F.defer_action('s1', 'AU200_AUD', 'open', 1, 0, 2.0, 1.5, 50.0, st, now,
                   broker_day(now))
    F.defer_action('s1', 'AU200_AUD', 'open', 1, 0, 3.0, 2.0, 60.0, st, now,
                   broker_day(now))
    q = F._read_deferred()
    assert len(q) == 1
    assert 's1' in q
    rec = q['s1']
    assert rec['units'] == 3.0
    assert rec['stop_mult'] == 2.0
    assert rec['atr'] == 60.0


# 6. clear_deferred drops the sleeve's pending intent from the queue.
def test_clear_deferred_removes_entry(env):
    F = env
    now = SUMMER_PASS
    F.defer_action('s1', 'AU200_AUD', 'open', 1, 0, 2.0, 1.5, 50.0, {}, now,
                   broker_day(now))
    q = F._read_deferred()
    assert 's1' in q
    q = F.clear_deferred('s1', q)
    assert 's1' not in q
