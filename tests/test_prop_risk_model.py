"""Unit tests for prop_risk_model.

The load-bearing one is test_halt_decision_parity_with_fix_runner: the model
carries its OWN copy of the breaker so it can stay env-free and dependency-free,
and this is what stops that copy from drifting away from the code that actually
trades the account.
"""
import random
from dataclasses import replace

import pytest

import prop_risk_model as M


def _state(equity=100_000.0, start=100_000.0, day_base=100_000.0, **kw):
    return M.AccountState(equity=equity, initial_balance=start,
                          step_start_balance=start, day_base=day_base, **kw)


# ---------------------------------------------------------------------------
# Parity with the live breaker
# ---------------------------------------------------------------------------

def test_halt_decision_parity_with_fix_runner():
    """A second implementation is only safe if something proves it is the same one."""
    fix_runner = pytest.importorskip("fix_runner")
    rng = random.Random(20260807)
    for _ in range(3000):
        start = rng.choice([10_000.0, 100_000.0, 2_354.0])
        anchor = start * rng.uniform(0.85, 1.30)
        equity = anchor * rng.uniform(0.85, 1.15)
        low = equity * rng.uniform(0.95, 1.0) if rng.random() < 0.5 else None
        cfg = M.RiskConfig(daily_limit=rng.choice([0.03, 0.05]),
                           total_limit=rng.choice([0.06, 0.10]),
                           halt_fraction=rng.choice([0.70, 0.80, 0.90, 1.0]))
        mine = M.halt_decision(equity, anchor, start, cfg, day_low=low)
        theirs = fix_runner.halt_decision(
            equity, anchor, start,
            daily_limit=cfg.daily_limit, total_limit=cfg.total_limit,
            fraction=cfg.halt_fraction, day_low=low)
        assert mine == theirs, (equity, anchor, start, low, cfg)


def test_halt_decision_fires_exactly_at_the_threshold():
    """0.03 * 0.80 has no exact binary form; the EPS is what makes this pass."""
    cfg = M.RiskConfig(halt_fraction=0.80)
    exactly = 100_000.0 * (1 - 0.024)
    assert M.halt_decision(exactly, 100_000.0, 100_000.0, cfg) == "daily"


def test_halt_decision_prefers_total_over_daily():
    cfg = M.RiskConfig()
    # Down 9% on the day AND 9% overall: total is the more serious verdict.
    assert M.halt_decision(91_000.0, 100_000.0, 100_000.0, cfg) == "total"


def test_halt_decision_unknown_is_not_a_breach():
    cfg = M.RiskConfig()
    assert M.halt_decision(0, 100_000.0, 100_000.0, cfg) is None
    assert M.halt_decision(95_000.0, None, 100_000.0, cfg) is None


def test_halt_decision_uses_the_day_low_not_the_tick():
    """A dip that recovered between samples still breaches — the rule says 'at any point'."""
    cfg = M.RiskConfig()
    assert M.halt_decision(100_000.0, 100_000.0, 100_000.0, cfg) is None
    assert M.halt_decision(100_000.0, 100_000.0, 100_000.0, cfg, day_low=97_500.0) == "daily"


# ---------------------------------------------------------------------------
# The sizing expression
# ---------------------------------------------------------------------------

def test_all_components_off_reproduces_the_live_expression():
    """This is what makes the harness's baseline-reproduction checkpoint mean anything."""
    cfg = M.RiskConfig().sizing_only()
    st = _state()
    rng = random.Random(7)
    for _ in range(500):
        ws, corr, kelly, decay = (rng.uniform(0.1, 3.0), rng.choice([0.5, 1.0]),
                                  rng.choice([0.5, 1.0, 2.0]), rng.choice([0.5, 1.0]))
        expected = min(cfg.base_risk * ws * corr * kelly * decay, cfg.max_risk)
        assert M.size_fraction(st, cfg, ws, corr, kelly, decay) == pytest.approx(expected)


def test_reducers_still_bite_a_trade_pinned_at_maxrisk():
    """The ordering bug this guards against would make the throttle a no-op
    exactly on the trades that most need it."""
    cfg = M.RiskConfig(throttle_enabled=True, throttle_start=0.0, throttle_floor=0.25)
    # ws large enough that the raw fraction is far above max_risk.
    pinned = _state()
    assert M.size_fraction(pinned, cfg.sizing_only(), weight_scale=20.0) == cfg.max_risk
    # Now spend the whole budget: the pinned trade must shrink, not stay at the cap.
    spent = _state(equity=97_600.0, day_base=100_000.0, day_low=97_600.0)
    assert M.daily_budget_used(spent, cfg) == pytest.approx(0.80)
    assert M.size_fraction(spent, cfg, weight_scale=20.0) == pytest.approx(
        cfg.max_risk * cfg.throttle_floor)


def test_ramp_is_neutral_at_a_fresh_step_and_bounded_by_maxrisk():
    cfg = M.RiskConfig(ramp_enabled=True)
    fresh = _state()
    assert M.cushion(fresh, cfg) == pytest.approx(0.10)
    assert M.m_ramp(fresh, cfg) == pytest.approx(1.0)
    # Banked cushion accelerates...
    rich = _state(equity=110_000.0)
    assert M.m_ramp(rich, cfg) > 1.0
    # ...but the per-trade ceiling still bounds it.
    assert M.size_fraction(rich, cfg, weight_scale=20.0) == cfg.max_risk


def test_endgame_cuts_only_inside_the_band():
    cfg = M.RiskConfig(endgame_enabled=True, endgame_band=0.015, endgame_scale=0.5)
    assert M.m_endgame(_state(equity=104_000.0), cfg) == 1.0      # 6.0% to go
    assert M.m_endgame(_state(equity=109_000.0), cfg) == 0.5      # 1.0% to go
    assert M.m_endgame(_state(equity=111_000.0), cfg) == 0.5      # past target


# ---------------------------------------------------------------------------
# Firm-rule geometry
# ---------------------------------------------------------------------------

def test_cushion_grows_with_profit_but_the_daily_base_does_not_accumulate():
    """Why M3 exists at all: only the static floor banks progress."""
    cfg = M.RiskConfig()
    assert M.cushion(_state(), cfg) == pytest.approx(0.10)
    assert M.cushion(_state(equity=110_000.0), cfg) > 0.15
    # The daily allowance is measured against a base that rescales every day,
    # so a profitable account has exactly as much daily room as a fresh one.
    rich = _state(equity=110_000.0, day_base=110_000.0)
    assert M.daily_floor(rich, cfg) / rich.equity == pytest.approx(0.97)


def test_daily_budget_uses_the_low_not_the_current_equity():
    cfg = M.RiskConfig()
    recovered = _state(equity=100_000.0, day_base=100_000.0, day_low=98_500.0)
    assert M.daily_budget_used(recovered, cfg) == pytest.approx(0.50)


def test_step_two_rebases_the_static_floor():
    cfg = M.RiskConfig(floor_rebases_each_step=True)
    st = _state(equity=110_000.0)
    assert M.total_floor(st, cfg) == pytest.approx(90_000.0)
    nxt = M.begin_step(st, cfg)
    assert nxt.step == 2
    assert M.total_floor(nxt, cfg) == pytest.approx(99_000.0)
    assert M.step_target(nxt, cfg) == pytest.approx(5_500.0)   # 5% of 110k
    assert nxt.best_day_profit == 0.0


def test_pinned_floor_variant_does_not_rebase():
    cfg = M.RiskConfig(floor_rebases_each_step=False)
    nxt = M.begin_step(_state(equity=110_000.0), cfg)
    assert M.total_floor(nxt, cfg) == pytest.approx(90_000.0)


def test_consistency_is_a_raised_bar_not_a_failure():
    """Treating it as pass/fail produced a bogus 76% pass figure on 2026-08-06."""
    cfg = M.RiskConfig()
    # Target met, but one day carried 80% of the profit -> not yet approved.
    st = _state(equity=110_000.0)
    st.best_day_profit = 8_000.0
    assert st.step_profit >= M.step_target(st, cfg)
    assert not M.step_passed(st, cfg)          # delayed, NOT disqualified
    # Keep trading; profit catches up and the same best day now clears the bar.
    st.equity = 116_000.0
    assert M.step_passed(st, cfg)


def test_consistency_ratio_is_infinite_only_while_profit_is_absent():
    cfg = M.RiskConfig()
    st = _state()
    st.best_day_profit = 500.0
    assert M.consistency_ratio(st, cfg) == float("inf")
    assert M.consistency_ratio(_state(), cfg) == 0.0


# ---------------------------------------------------------------------------
# The budget gate
# ---------------------------------------------------------------------------

def test_budget_gate_is_off_by_default():
    assert M.admit_open(0.02, _state(), M.RiskConfig()) is True


def test_budget_gate_refuses_a_position_that_will_not_fit():
    cfg = M.RiskConfig(budget_gate_enabled=True)
    fresh = _state()
    # Halt line is 2.40% down; a fresh day can still afford a 2% stop-out.
    assert M.daily_budget_remaining(fresh, cfg) == pytest.approx(2_400.0)
    assert M.admit_open(0.02, fresh, cfg) is True
    assert M.admit_open(0.03, fresh, cfg) is False
    # Half the budget already spent: the same 2% position no longer fits.
    spent = _state(equity=98_800.0, day_base=100_000.0, day_low=98_800.0)
    assert M.daily_budget_remaining(spent, cfg) == pytest.approx(1_200.0)
    assert M.admit_open(0.02, spent, cfg) is False


def test_budget_remaining_floors_at_zero_past_the_halt_line():
    cfg = M.RiskConfig(budget_gate_enabled=True)
    blown = _state(equity=97_000.0, day_base=100_000.0, day_low=97_000.0)
    assert M.daily_budget_remaining(blown, cfg) == 0.0
    assert M.admit_open(0.001, blown, cfg) is False
