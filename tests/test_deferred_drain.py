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
    def execute_order(self, signed_units, tag, stop_loss=None):
        # stop_loss rides on the ORDER under VENUE=ctrader (atomic attach) and is
        # None under VENUE=fix, where the stop is a separate order placed after.
        self.sent.append(('open', signed_units, tag, stop_loss))
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


def test_halt_drops_the_whole_queue(env, monkeypatch):
    """(1) A latched breaker drops every queued intent; nothing reaches broker."""
    monkeypatch.setattr(F, 'halt_is_active', lambda h, t: True)
    ad = FakeAdapter()
    F._write_deferred({'s1': intent(DRAIN_NOW)})
    F.deferred_drain(SLEEVES, {}, make_adapters(ad), True, now=DRAIN_NOW)
    assert len(ad.sent) == 0
    assert len(F._read_deferred()) == 0


def test_halt_check_raising_holds_the_queue(env, monkeypatch):
    """(2) An unreadable guard must block draining, never allow it — fail closed."""
    def _boom():
        raise RuntimeError('halt file unreadable')
    monkeypatch.setattr(F, '_read_halt', _boom)
    ad = FakeAdapter()
    F._write_deferred({'s1': intent(DRAIN_NOW)})
    F.deferred_drain(SLEEVES, {}, make_adapters(ad), True, now=DRAIN_NOW)
    assert len(ad.sent) == 0
    assert len(F._read_deferred()) == 1


def test_supersession_drops_stale_broker_day(env):
    """(3) An intent whose broker_day has rolled is superseded by today's pass."""
    ad = FakeAdapter()
    F._write_deferred({'s1': intent(DRAIN_NOW, broker_day='1999-01-01')})
    F.deferred_drain(SLEEVES, {}, make_adapters(ad), True, now=DRAIN_NOW)
    assert len(ad.sent) == 0
    assert len(F._read_deferred()) == 0


def test_still_shut_keeps_intent(env):
    """(4) Session closed -> intent held, zero broker calls."""
    ad = FakeAdapter(intervals=AU200)
    F._write_deferred({'s1': intent(WINTER_PASS)})
    F.deferred_drain(SLEEVES, {}, make_adapters(ad), True, now=WINTER_PASS)
    assert len(ad.sent) == 0
    assert len(F._read_deferred()) == 1


def test_happy_path_open_then_stop(env, monkeypatch):
    """(5) Session open -> open + stop attached, state recorded, queue drained."""
    monkeypatch.setattr(F, 'q2usd', lambda inst: 1.0)   # deterministic risk math
    ad = FakeAdapter()
    state = {}
    F._write_deferred({'s1': intent(DRAIN_NOW)})
    F.deferred_drain(SLEEVES, state, make_adapters(ad), True, now=DRAIN_NOW)
    assert len(ad.sent) == 2
    assert ad.sent[0][0] == 'open'
    assert ad.sent[1][0] == 'stop'
    assert len(F._read_deferred()) == 0
    assert state['s1']['pos_id'] == 'POS1'
    assert state['s1']['units'] == 2.0
    assert state['s1']['side'] == 1
    assert state['s1']['stop_ref'] is not None


def test_double_send_guard_drops_open_when_already_held(env):
    """(6) Pass opened but died before writing state -> drain must not double size."""
    ad = FakeAdapter(open_ids=('POSX',))
    state = {'s1': {'signal': 1, 'pos_id': 'POSX', 'units': 2.0, 'side': 1,
                    'stop': 8925.0, 'stop_ref': 'R'}}
    F._write_deferred({'s1': intent(DRAIN_NOW)})
    F.deferred_drain(SLEEVES, state, make_adapters(ad), True, now=DRAIN_NOW)
    assert len(ad.sent) == 0
    assert len(F._read_deferred()) == 0


def test_close_whose_position_is_already_gone_is_dropped(env):
    """(7) Close aimed at a dead id is a no-op, not a new trade in the wrong direction."""
    ad = FakeAdapter(open_ids=())                       # nothing at the broker
    state = {'s1': {'signal': 1, 'pos_id': 'GONE', 'units': 2.0, 'side': 1,
                    'stop': 8925.0, 'stop_ref': 'R'}}
    F._write_deferred({'s1': intent(DRAIN_NOW, kind='close', pos_id='GONE',
                                    prev_signal=1, signal=0)})
    F.deferred_drain(SLEEVES, state, make_adapters(ad), True, now=DRAIN_NOW)
    assert len(ad.sent) == 0
    assert len(F._read_deferred()) == 0


def test_close_aborts_when_stop_cancel_unconfirmed(env):
    """(8) A stop outliving its position fires as a naked entry — never start the
    close leg unless the cancel is confirmed. Position intact, intent kept."""
    ad = FakeAdapter(cancel_ok=False, open_ids=('P9',))
    state = {'s1': {'signal': 1, 'pos_id': 'P9', 'units': 2.0, 'side': 1,
                    'stop': 8925.0, 'stop_ref': 'R'}}
    F._write_deferred({'s1': intent(DRAIN_NOW, kind='close', pos_id='P9',
                                    stop_ref='R', side=1, held_units=2.0,
                                    prev_signal=1, signal=0)})
    F.deferred_drain(SLEEVES, state, make_adapters(ad), True, now=DRAIN_NOW)
    assert len(ad.sent) == 1
    assert all(c[0] != 'close' for c in ad.sent)
    assert state['s1']['pos_id'] == 'P9'
    assert len(F._read_deferred()) == 1


def test_disabled_is_a_no_op(env, monkeypatch):
    """(9) DEFER_SHUT_MARKET=False leaves a full queue untouched, zero calls."""
    monkeypatch.setattr(F, 'DEFER_SHUT_MARKET', False)
    ad = FakeAdapter()
    F._write_deferred({'s1': intent(DRAIN_NOW)})
    F.deferred_drain(SLEEVES, {}, make_adapters(ad), True, now=DRAIN_NOW)
    assert len(ad.sent) == 0
    assert len(F._read_deferred()) == 1
