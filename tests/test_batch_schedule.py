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

    def test_exploit_instruments_gated_by_flag(self, monkeypatch):
        # A 'NORMAL' (baseline) batch must get NO exploit slots regardless of the DB.
        monkeypatch.setattr(ar, 'EXPLOIT_ENABLED', False)
        assert ar._exploit_instruments() == []
        # A 'DRIVEN' batch pulls the DD-blocked families.
        monkeypatch.setattr(ar, 'EXPLOIT_ENABLED', True)
        import meta_review
        monkeypatch.setattr(meta_review, 'dd_blocked_instruments', lambda *a, **k: ['WTICO_USD'])
        assert ar._exploit_instruments() == ['WTICO_USD']
