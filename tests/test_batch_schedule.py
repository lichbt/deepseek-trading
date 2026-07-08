"""Tests for _build_batch_schedule — the explore/exploit balance of batch gen.

Data-driven 'exploit' slots steer toward demonstrated-edge-but-DD-blocked
families, but they must stay a BOUNDED minority on top of the random rotation,
never displacing the wild (pure-exploration) slots — so data-driven focus can't
collapse diversity.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
import auto_research as ar

INSTS = ['EUR_USD', 'XAU_USD', 'GBP_JPY', 'SPX500_USD', 'NAS100_USD', 'XCU_USD']
POOL = ['WTICO_USD', 'ETH_USD', 'XAG_USD']


def _exploit(sch):
    return [s for s in sch if 'DATA-DRIVEN' in s[1]]


def _wild(sch):
    return [s for s in sch if s[2]]


def _event(sch):
    return [s for s in sch if 'days_to_event' in s[1] or 'event_window' in s[1]]


def _calendar(sch):
    return [s for s in sch if s[1] == ar._CALENDAR_CONSTRAINT]


class TestBatchSchedule:
    def test_exploit_bounded_and_never_wild(self):
        sch = ar._build_batch_schedule(INSTS, 31, 0, POOL)
        ex = _exploit(sch)
        assert 0 < len(ex) <= 31 // ar.EXPLOIT_SLOT_EVERY + 1
        assert all(not s[2] for s in ex)        # an exploit slot is never a wild slot
        assert len(ex) / 31 < 0.15              # exploration stays the backbone

    def test_wild_floor_preserved(self):
        sch = ar._build_batch_schedule(INSTS, 31, 0, POOL)
        w = _wild(sch)
        assert [s[3] for s in w] == [8, 16, 24]          # every 8th, untouched
        assert all('WILD' in s[1] for s in w)            # pure exploration, not exploit

    def test_exploit_uses_pool_and_risk_instruction(self):
        for s in _exploit(ar._build_batch_schedule(INSTS, 31, 0, POOL)):
            assert s[0] in POOL                           # targets a DD-blocked family
            assert 'drawdown control' in s[1].lower()     # carries the risk-control steer
            assert s[5] == 'D'                            # exploit designs on daily

    def test_empty_pool_is_pure_rotation(self):
        sch = ar._build_batch_schedule(INSTS, 31, 0, exploit_pool=[])
        assert _exploit(sch) == []                        # fail-soft: no exploit slots
        assert len(_wild(sch)) == 3                       # rotation backbone intact

    def test_none_pool_is_failsoft(self):
        sch = ar._build_batch_schedule(INSTS, 20, 0, exploit_pool=None)
        assert _exploit(sch) == [] and len(sch) == 20

    def test_schedule_length_matches_iterations(self):
        assert len(ar._build_batch_schedule(INSTS, 20, 0, POOL)) == 20

    def test_cadence_keeps_exploration_dominant(self):
        assert ar.EXPLOIT_SLOT_EVERY >= 10                # exploit is a small minority

    def test_event_slot_has_measured_presence(self):
        # Event was 1-of-N creative constraints the model ignored ~90% of the time.
        # As a dedicated forced slot it must get a real, bounded share (~5%).
        sch = ar._build_batch_schedule(INSTS, 2000, 0, POOL)
        ev = _event(sch)
        assert 0.03 < len(ev) / len(sch) < 0.08          # a real ~5% presence, not zero, not dominant

    def test_event_slots_daily_pinned(self):
        # days_to_event / event_window are day-resolution — meaningless on weekly bars.
        sch = ar._build_batch_schedule(INSTS, 2000, 0, POOL)
        assert all(s[5] == 'D' for s in _event(sch))     # every event slot forced to daily

    def test_event_never_collides_with_calendar(self):
        sch = ar._build_batch_schedule(INSTS, 2000, 0, POOL)
        ev_i = {s[3] for s in _event(sch)}
        cal_i = {s[3] for s in _calendar(sch)}
        assert ev_i and cal_i                            # both families present
        assert ev_i.isdisjoint(cal_i)                    # offset moduli never overlap

    def test_event_constraint_self_enforces(self):
        # The forced constraint must name the exact injected columns and threaten discard,
        # so the model actually references them instead of falling back to price-only.
        c = ar._EVENT_CONSTRAINT
        assert 'days_to_event' in c and 'event_window' in c and 'days_since_event' in c
        assert 'DISCARD' in c.upper()

    def test_event_removed_from_creative_rotation(self):
        # Single source of truth: event lives only in the forced slot now.
        assert not any('days_to_event' in c or 'event_window' in c
                       for c in ar._CREATIVE_CONSTRAINTS)

    def test_exploit_instruments_gated_by_flag(self, monkeypatch):
        # A 'NORMAL' (baseline) batch must get NO exploit slots regardless of the DB.
        monkeypatch.setattr(ar, 'EXPLOIT_ENABLED', False)
        assert ar._exploit_instruments() == []
        # A 'DRIVEN' batch pulls the DD-blocked families.
        monkeypatch.setattr(ar, 'EXPLOIT_ENABLED', True)
        import meta_review
        monkeypatch.setattr(meta_review, 'dd_blocked_instruments', lambda *a, **k: ['WTICO_USD'])
        assert ar._exploit_instruments() == ['WTICO_USD']
