"""Prop-challenge risk model — sizing policy under a 3% daily / 10% static wall.

WHAT THIS REPLACES. The live sizing decision is one expression, in two places
that agree by convention rather than by construction:

    fix_runner.size_units:            min(RISK * ws * corr * kelly * decay, MAXRISK)
    oanda_book_simulator.risk_units:  min(risk * weight_scale * corr_scale * kelly * decay, max_risk)

That expression is PER-TRADE and STATELESS. It cannot see today's loss, the
cushion above the static floor, how close the step target is, or the consistency
bar. This module adds exactly that account-level layer and leaves the per-sleeve
terms (ws / corr / kelly / decay) untouched — they are a redistribution of a
cap-bound pie and are not this module's business.

WHAT IT DELIBERATELY DOES NOT DO. It does not gate on aggregate OPEN RISK.
Measured 2026-08-05: corr(open_risk, same-day return) = +0.106, the eight
highest-open-risk days were net +1.48% and contained the best day in the sample,
and the worst day (-1.507%, 2024-06-06) carried only 1.153% open risk — the gate
would not have fired. Open risk measures participation, not danger. M2 below
gates on REALISED intraday loss instead, which is the risk rather than a proxy
for it.

WHY THE OBJECTIVE IS DAYS, NOT SURVIVAL. Measured 2026-08-05: this book times
out (0.30%) rather than breaching (~0.00%), and cutting BASE_RISK 0.005 ->
0.00375 moved median days to funded 479 -> 645. Total DD has never been binding
(maxDD -4.26% against a 10% floor). So every component here is judged on days to
funded at matched DQ probability, and a component that does not buy days is
shipped off by default.

PURE BY CONSTRUCTION: no clock, no I/O, no broker, no env reads. Every input is
an argument and every knob lives in RiskConfig, so a sweep is a dataclasses
.replace() and a unit test needs no fixtures.
"""
from dataclasses import dataclass, field, replace
from typing import Optional, Tuple

__all__ = [
    "RiskConfig", "AccountState", "size_fraction", "admit_open",
    "halt_decision", "total_floor", "daily_floor", "cushion",
    "daily_budget_used", "step_target", "step_passed", "begin_step",
    "consistency_ratio", "MULTIPLIERS",
]

_EPS = 1e-12


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class RiskConfig:
    """Every knob, with the live 2026-08-07 pod config as the default.

    Defaults are chosen so that RiskConfig() with all components disabled
    reproduces the CURRENT live sizing exactly — that is what makes the
    harness's baseline-reproduction checkpoint meaningful.
    """

    # -- M1: the operating point (scalar x guard, swept jointly) --------------
    base_risk: float = 0.005        # pod BASE_RISK
    max_risk: float = 0.02          # pod FIX_MAXRISK, per-trade ceiling
    halt_fraction: float = 0.80     # pod PROP_HALT_FRACTION
    guard_lag_pp: float = 0.004     # modelled slippage past the halt line

    # -- firm rules (The5ers 100k two-step) -----------------------------------
    daily_limit: float = 0.03
    total_limit: float = 0.10
    step_targets: Tuple[float, ...] = (0.10, 0.05)
    # Confirmed with the user 2026-08-07: the static max loss RE-BASES to the
    # step's starting balance, which is what scripts/prop_twostep_mc.py already
    # assumes. False models a floor pinned to the original deposit.
    floor_rebases_each_step: bool = True
    consistency_cap: float = 0.50

    # -- M2: realised-loss throttle (the ONLY new account-level control) ------
    # Two independently switchable legs, so the sweep can attribute each:
    #   throttle  — shrink new positions as today's loss eats the daily budget
    #   budget_gate — refuse an open whose stop-risk does not FIT in what is left
    throttle_enabled: bool = False
    throttle_start: float = 0.40    # budget fraction consumed before shrinking
    throttle_floor: float = 0.25    # size multiplier once the halt line is reached
    budget_gate_enabled: bool = False
    budget_gate_headroom: float = 1.0   # required multiple of the position's stop-risk

    # -- M3: cushion ramp above the static floor ------------------------------
    ramp_enabled: bool = False
    ramp_ref_cushion: float = 0.10  # cushion at which the multiplier is exactly 1.0
    ramp_min: float = 1.0
    ramp_max: float = 2.0

    # -- M4: endgame de-risking near a step target ----------------------------
    endgame_enabled: bool = False
    endgame_band: float = 0.015     # within this much of the target, in step-start terms
    endgame_scale: float = 0.50

    # -- M5: consistency governor ---------------------------------------------
    consistency_enabled: bool = False
    consistency_start: float = 0.80  # fraction of the cap at which shrinking begins
    consistency_floor: float = 0.50

    def sizing_only(self) -> "RiskConfig":
        """The narrow variant: current sizing, every account component off."""
        return replace(
            self, throttle_enabled=False, budget_gate_enabled=False,
            ramp_enabled=False, endgame_enabled=False, consistency_enabled=False,
        )


@dataclass
class AccountState:
    """Mutable account view the model reads. The caller owns advancing it.

    `day_base` is the daily BASE: max(balance, equity) snapshotted at midnight
    server time, NOT the equity at whatever moment we happened to sample. That
    is the firm's rule (confirmed 2026-08-07) and equity-only anchoring is too
    permissive whenever a day opens in floating loss.

    `day_low` is the lowest equity seen today. The daily rule breaches "at any
    point during the day", so every daily quantity here is measured on
    min(equity, day_low) rather than on the current tick.
    """

    equity: float
    initial_balance: float
    step_start_balance: float
    day_base: float
    day_low: Optional[float] = None
    step: int = 1
    best_day_profit: float = 0.0    # running max, in currency, within the step
    peak_equity: float = 0.0

    def __post_init__(self):
        if self.day_low is None:
            self.day_low = self.equity
        if not self.peak_equity:
            self.peak_equity = self.equity

    @property
    def low(self) -> float:
        """The equity the daily rule actually judges."""
        return min(float(self.equity), float(self.day_low))

    @property
    def step_profit(self) -> float:
        return self.equity - self.step_start_balance


# ---------------------------------------------------------------------------
# Firm-rule geometry
# ---------------------------------------------------------------------------

def total_floor(state: AccountState, cfg: RiskConfig) -> float:
    """Equity level at which the STATIC max loss is breached."""
    anchor = state.step_start_balance if cfg.floor_rebases_each_step else state.initial_balance
    return anchor * (1.0 - cfg.total_limit)


def daily_floor(state: AccountState, cfg: RiskConfig) -> float:
    """Equity level at which the DAILY loss is breached."""
    return state.day_base * (1.0 - cfg.daily_limit)


def cushion(state: AccountState, cfg: RiskConfig) -> float:
    """Room above the static floor, as a fraction of current equity.

    This is the quantity that GROWS with profit — the daily limit is measured
    against a base that rescales every day and therefore offers no such
    accumulation. It is the whole basis of M3.
    """
    eq = max(float(state.equity), _EPS)
    return max(0.0, (eq - total_floor(state, cfg)) / eq)


def daily_budget_used(state: AccountState, cfg: RiskConfig) -> float:
    """Fraction of the day's loss allowance already spent, in [0, 1].

    Denominated in the FULL limit, not the halt line, so that a config's
    halt_fraction can move without silently re-scaling what "half the budget"
    means across the sweep.
    """
    base = max(float(state.day_base), _EPS)
    lost = (base - state.low) / base
    return min(1.0, max(0.0, lost / max(cfg.daily_limit, _EPS)))


def daily_budget_remaining(state: AccountState, cfg: RiskConfig) -> float:
    """Currency still losable today before the GUARD halts (not before the wall).

    Measured to the halt line rather than the limit, because reaching the halt
    line ends the day's trading just as surely as breaching does — and costs a
    flatten plus re-entries on top.
    """
    base = max(float(state.day_base), _EPS)
    halt_level = base * (1.0 - cfg.daily_limit * cfg.halt_fraction)
    return max(0.0, state.low - halt_level)


def consistency_ratio(state: AccountState, cfg: RiskConfig) -> float:
    """best_day / (cap * profit). <= 1.0 means the consistency bar is met.

    The5ers' rule is (best day profit / total profit) * 100 <= cap. It is a
    RAISED BAR, not a failure mode: missing it delays approval until profit
    catches up, it does not disqualify. Returns inf while profit is non-positive
    — the bar is unmeetable there, and unmeetable is not the same as failed.
    """
    profit = state.step_profit
    if profit <= _EPS:
        return float("inf") if state.best_day_profit > 0 else 0.0
    return state.best_day_profit / max(cfg.consistency_cap * profit, _EPS)


def step_target(state: AccountState, cfg: RiskConfig) -> float:
    """Profit required to clear the current step, in currency."""
    idx = min(max(state.step, 1), len(cfg.step_targets)) - 1
    return state.step_start_balance * cfg.step_targets[idx]


def step_passed(state: AccountState, cfg: RiskConfig) -> bool:
    """Has the current step been approved?

    Consistency is modelled as a raised bar: the step clears when profit reaches
    max(target, best_day / cap). Treating it as pass/fail produced a bogus "76%
    pass" for the live config (measured 2026-08-06).
    """
    required = step_target(state, cfg)
    if state.best_day_profit > 0:
        required = max(required, state.best_day_profit / max(cfg.consistency_cap, _EPS))
    return state.step_profit >= required


def begin_step(state: AccountState, cfg: RiskConfig) -> AccountState:
    """Advance to the next step, re-basing what the firm re-bases.

    best_day_profit resets because the consistency ratio is evaluated per step
    against that step's accumulated profit.
    """
    return AccountState(
        equity=state.equity,
        initial_balance=state.initial_balance,
        step_start_balance=state.equity,
        day_base=state.equity,
        day_low=state.equity,
        step=state.step + 1,
        best_day_profit=0.0,
        peak_equity=state.equity,
    )


def halt_decision(equity, day_anchor, start_equity, cfg: RiskConfig,
                  day_low=None) -> Optional[str]:
    """-> None | 'daily' | 'total'. Mirrors fix_runner.halt_decision.

    Deliberately a SECOND implementation rather than an import: this module
    stays dependency-free and env-free, while tests/test_prop_risk_model.py
    asserts parity against fix_runner over a randomised grid, so the two cannot
    drift silently.

    Total is checked FIRST and is the more serious verdict — the daily anchor
    resets every session, the static total never does.

    EPS because the thresholds are products of decimals with no exact binary
    form: 0.03 * 0.80 is -0.024000000000000004, so an equity exactly 2.40% down
    compared as strictly-greater and did NOT halt.
    """
    if not equity or not day_anchor or not start_equity:
        return None                      # unknown != safe, but unknown != breach
    low = equity if not day_low else min(float(day_low), float(equity))
    eps = 1e-9
    if (low - start_equity) / start_equity <= -abs(cfg.total_limit) * cfg.halt_fraction + eps:
        return "total"
    if (low - day_anchor) / day_anchor <= -abs(cfg.daily_limit) * cfg.halt_fraction + eps:
        return "daily"
    return None


# ---------------------------------------------------------------------------
# The account-level multipliers
# ---------------------------------------------------------------------------

def _lerp_down(x: float, lo: float, hi: float, top: float, bottom: float) -> float:
    """Linear taper from `top` at x<=lo to `bottom` at x>=hi."""
    if hi <= lo:
        return bottom if x >= hi else top
    t = min(1.0, max(0.0, (x - lo) / (hi - lo)))
    return top + (bottom - top) * t


def m_throttle(state: AccountState, cfg: RiskConfig) -> float:
    """M2 — shrink as today's realised loss consumes the daily budget.

    Reaches its floor at the HALT LINE, not at the wall: past the halt line the
    guard flattens anyway, so tapering to the wall would be tapering across a
    region the book never occupies.
    """
    if not cfg.throttle_enabled:
        return 1.0
    return _lerp_down(daily_budget_used(state, cfg),
                      cfg.throttle_start, cfg.halt_fraction,
                      1.0, cfg.throttle_floor)


def m_ramp(state: AccountState, cfg: RiskConfig) -> float:
    """M3 — scale with the cushion above the static floor.

    Neutral at ramp_ref_cushion (0.10 = a fresh step), so a step opens at
    exactly the configured base risk and only accelerates once real cushion has
    been banked.
    """
    if not cfg.ramp_enabled:
        return 1.0
    raw = cushion(state, cfg) / max(cfg.ramp_ref_cushion, _EPS)
    return min(cfg.ramp_max, max(cfg.ramp_min, raw))


def m_endgame(state: AccountState, cfg: RiskConfig) -> float:
    """M4 — cut size inside the last stretch to a step target.

    A DQ here costs the entire run to date, so this trades days for tail.
    """
    if not cfg.endgame_enabled:
        return 1.0
    remaining = step_target(state, cfg) - state.step_profit
    band = cfg.endgame_band * max(state.step_start_balance, _EPS)
    if remaining <= 0:
        return cfg.endgame_scale
    return cfg.endgame_scale if remaining <= band else 1.0


def m_consistency(state: AccountState, cfg: RiskConfig) -> float:
    """M5 — shrink as the best-day ratio approaches the cap.

    A record day pushes the required total profit out again, so once the ratio
    is high the cheapest route to approval is more SMALL days. Expected to be
    minor at a 50% cap; included because it is nearly free, not because it is
    promising.
    """
    if not cfg.consistency_enabled:
        return 1.0
    ratio = consistency_ratio(state, cfg)
    if ratio == float("inf"):
        return cfg.consistency_floor
    return _lerp_down(ratio, cfg.consistency_start, 1.0,
                      1.0, cfg.consistency_floor)


MULTIPLIERS = {
    "throttle": m_throttle,
    "ramp": m_ramp,
    "endgame": m_endgame,
    "consistency": m_consistency,
}


# ---------------------------------------------------------------------------
# The sizing decision
# ---------------------------------------------------------------------------

def size_fraction(state: AccountState, cfg: RiskConfig,
                  weight_scale: float = 1.0, corr_scale: float = 1.0,
                  kelly: float = 1.0, decay: float = 1.0) -> float:
    """Risk fraction for ONE new position — the replacement expression.

    ORDER MATTERS, and getting it wrong makes a reducer a no-op. Increases (the
    cushion ramp) are applied BEFORE the per-trade ceiling so that MAXRISK still
    bounds them; reductions are applied AFTER, because a trade already pinned at
    MAXRISK must still shrink when today's budget is spent. Folding everything
    into one min() would let the ceiling swallow the throttle exactly on the
    days it exists for.
    """
    up = m_ramp(state, cfg)
    down = m_throttle(state, cfg) * m_endgame(state, cfg) * m_consistency(state, cfg)
    capped = min(cfg.base_risk * weight_scale * corr_scale * kelly * decay * up,
                 cfg.max_risk)
    return max(0.0, capped * down)


def admit_open(stop_risk_fraction: float, state: AccountState,
               cfg: RiskConfig) -> bool:
    """M2's hard leg — does this position's loss-to-stop FIT in what is left today?

    Distinct from the throttle, and switchable separately, because they answer
    different questions: the throttle shrinks everything as the budget drains,
    this one refuses a single position that would carry the book past the halt
    line on its own if it stopped out.

    NOTE this is not the rejected aggregate open-risk gate. That one asked "is a
    lot of risk open right now?" — a question whose answer is uncorrelated with
    the day's outcome. This asks "can we still afford to lose this?", which is
    conditioned on realised loss.
    """
    if not cfg.budget_gate_enabled:
        return True
    if stop_risk_fraction <= 0:
        return True
    need = stop_risk_fraction * max(state.equity, 0.0) * cfg.budget_gate_headroom
    return need <= daily_budget_remaining(state, cfg)
