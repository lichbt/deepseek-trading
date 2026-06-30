"""
strategy_honesty.py

Two layers that sit on top of an LLM strategy-generation loop:

  1. Trials accounting + Deflated Sharpe Ratio (DSR)
     -- a DESCRIPTIVE deflation: where a winner's full-sample Sharpe sits vs the
        search's expected-best. NOTE: valid as a multiple-testing verdict ONLY if
        you select on Sharpe; this pipeline selects on GT-score, so it's read
        descriptively, not as a gate (see deflated_sharpe_ratio docstring).

  2. Failure taxonomy
     -- structured validation output with a fixed failure vocabulary,
        and a feedback formatter that goes back into the generation prompt
        so the search is directed, not a random walk.

Deps: numpy, scipy. SQLite is stdlib.
Math: Bailey & Lopez de Prado, "The Deflated Sharpe Ratio" (2014).
"""

from __future__ import annotations
import sqlite3, time, json
from collections import Counter
from dataclasses import dataclass, field, asdict
from typing import Optional

import numpy as np
from scipy.stats import norm, skew, kurtosis

EULER_MASCHERONI = 0.5772156649015329


# ---------------------------------------------------------------------------
# 1. TRIALS ACCOUNTING + DEFLATED SHARPE
# ---------------------------------------------------------------------------

def probabilistic_sharpe_ratio(returns, sr_benchmark: float = 0.0) -> float:
    """P(true SR > sr_benchmark) given the observed return series.

    returns        : per-period returns (one frequency throughout, e.g. daily)
    sr_benchmark   : threshold Sharpe in the SAME per-period units as `returns`

    Corrects for sample length AND non-normality (skew/fat tails), which is
    where naive Sharpe lies to you on strategies with rare blow-ups.
    """
    r = np.asarray(returns, dtype=float)
    n = len(r)
    if n < 8 or r.std(ddof=1) == 0:
        return 0.0
    sr = r.mean() / r.std(ddof=1)              # per-period observed Sharpe
    g3 = skew(r)                                # skewness
    g4 = kurtosis(r, fisher=False)             # kurtosis, normal == 3
    denom = np.sqrt(1.0 - g3 * sr + ((g4 - 1.0) / 4.0) * sr ** 2)
    return float(norm.cdf((sr - sr_benchmark) * np.sqrt(n - 1) / denom))


def expected_max_sharpe(sr_variance: float, n_trials: int) -> float:
    """Expected MAXIMUM Sharpe you'd see from n_trials junk strategies whose
    true Sharpe is zero but whose estimates scatter with variance sr_variance.

    This is the bar a real edge has to clear. The more strategies you try,
    the higher this bar climbs -- that's the multiple-comparisons tax made
    explicit.
    """
    if n_trials < 2:
        return 0.0
    z1 = norm.ppf(1.0 - 1.0 / n_trials)
    z2 = norm.ppf(1.0 - 1.0 / (n_trials * np.e))
    return float(np.sqrt(sr_variance) * ((1.0 - EULER_MASCHERONI) * z1
                                         + EULER_MASCHERONI * z2))


def deflated_sharpe_ratio(returns, all_trial_sharpes) -> float:
    """DSR in [0,1]. PSR benchmarked against the expected-max-Sharpe of the
    whole search, not against zero.

    returns            : candidate's per-period returns
    all_trial_sharpes  : per-period Sharpe of EVERY strategy tried this search
                         (winners and losers -- the losers define the variance)

    DESCRIPTIVE ONLY in this pipeline. The DSR/PSR machinery is derived for a
    candidate SELECTED BY MAXIMIZING SHARPE over N trials. This pipeline selects
    on GT-score (WF/HO), so the deflated quantity (Sharpe) is NOT the selection
    axis: a GT-selected winner is usually not the Sharpe-argmax, so its DSR runs
    systematically low. Read it as "where this strategy's full-sample Sharpe sits
    vs the search's expected-best Sharpe" -- informative, NOT a "this is overfit /
    just luck" verdict. The locked holdout is the real overfit control. To make
    this a valid multiple-testing GATE, deflate the GT-score (the selected stat),
    not the Sharpe.
    """
    s = np.asarray(all_trial_sharpes, dtype=float)
    if len(s) < 2:
        return probabilistic_sharpe_ratio(returns, 0.0)
    sr_star = expected_max_sharpe(np.var(s, ddof=1), len(s))
    return probabilistic_sharpe_ratio(returns, sr_benchmark=sr_star)


# --- SQLite trial log (fits your existing dedup/time-series pattern) --------

_SCHEMA = """CREATE TABLE IF NOT EXISTS trials(
    hash       TEXT PRIMARY KEY,
    ts         REAL,
    sharpe     REAL,      -- per-period, same units you feed DSR
    passed_wf  INTEGER,
    passed_ho  INTEGER,
    failure    TEXT,
    meta       TEXT
)"""


def record_trial(db, strat_hash, sharpe, passed_wf, passed_ho,
                 failure=None, meta=None):
    con = sqlite3.connect(db)
    con.execute(_SCHEMA)
    con.execute(
        "INSERT OR IGNORE INTO trials VALUES (?,?,?,?,?,?,?)",
        (strat_hash, time.time(), float(sharpe), int(passed_wf),
         int(passed_ho), failure, json.dumps(meta or {})),
    )
    con.commit(); con.close()


def trial_sharpes(db) -> list[float]:
    con = sqlite3.connect(db); con.execute(_SCHEMA)
    rows = [r[0] for r in con.execute("SELECT sharpe FROM trials")]
    con.close()
    return rows


def trials_per_ho_pass(db) -> Optional[float]:
    """The number you're probably not tracking. 1-in-40 means your HO is
    effectively training data; deflate accordingly."""
    con = sqlite3.connect(db); con.execute(_SCHEMA)
    total = con.execute("SELECT COUNT(*) FROM trials").fetchone()[0]
    passes = con.execute("SELECT COUNT(*) FROM trials WHERE passed_ho=1").fetchone()[0]
    con.close()
    return None if passes == 0 else total / passes


# ---------------------------------------------------------------------------
# 2. FAILURE TAXONOMY
# ---------------------------------------------------------------------------

# Fixed vocabulary -> aggregatable across the search. Don't free-text these.
FAILURE_TAGS = {
    "regime_fragile":   "wins in only one regime across folds",
    "high_vol_breakdown": "fails specifically in high-vol folds",
    "dd_breach":        "max drawdown over limit",
    "turnover_excess":  "too much trading -- cost-fragile",
    "insufficient_folds": "passed < required number of WF folds",
    "ho_decay":         "passed WF, failed HO (the overfit signature)",
    "low_sample":       "too few trades to be significant",
}


@dataclass
class FoldResult:
    fold_id: int
    sharpe: float
    max_dd: float
    turnover: float
    regime: str          # trending | ranging | high_vol | low_vol
    passed: bool


@dataclass
class ValidationResult:
    strat_hash: str
    folds: list[FoldResult] = field(default_factory=list)
    wf_folds_passed: int = 0
    wf_passed: bool = False
    ho_sharpe: Optional[float] = None
    ho_passed: Optional[bool] = None
    failure_tag: Optional[str] = None      # primary reason, from FAILURE_TAGS
    failure_detail: Optional[str] = None   # one human line of specifics

    def to_row(self):
        return dict(strat_hash=self.strat_hash,
                    wf_folds_passed=self.wf_folds_passed,
                    wf_passed=self.wf_passed, ho_passed=self.ho_passed,
                    failure_tag=self.failure_tag,
                    failure_detail=self.failure_detail)


def classify_failure(folds, wf_min_folds, dd_limit,
                     ho_passed=None) -> tuple[Optional[str], Optional[str]]:
    """Assign ONE primary failure tag. Order = priority. Returns (tag, detail).
    None means it passed everything."""
    passed = [f for f in folds if f.passed]

    # WF passed but HO didn't -> the signature you most want to catch
    if ho_passed is False and len(passed) >= wf_min_folds:
        return "ho_decay", "clears WF folds, decays out-of-sample on HO"

    worst_dd = max((f.max_dd for f in folds), default=0.0)
    if worst_dd > dd_limit:
        return "dd_breach", f"max DD {worst_dd:.1%} > limit {dd_limit:.1%}"

    win_regimes = {f.regime for f in passed}
    if passed and len(win_regimes) == 1:
        return "regime_fragile", f"only wins in {win_regimes.pop()} regime"

    hv = [f for f in folds if f.regime == "high_vol"]
    if hv and not any(f.passed for f in hv):
        return "high_vol_breakdown", "all high-vol folds failed"

    if len(passed) < wf_min_folds:
        return "insufficient_folds", f"{len(passed)}/{len(folds)} folds, need {wf_min_folds}"

    return None, None


def failure_feedback(recent: list[ValidationResult], last_n: int = 12) -> str:
    """Format the last N failures into a prompt block for the generator.
    This is what turns the loop from random-walk into directed search."""
    window = [r for r in recent if r.failure_tag][-last_n:]
    if not window:
        return "No recent failures to learn from."
    tags = Counter(r.failure_tag for r in window)
    lines = ["Recent failure pattern (last %d rejected):" % len(window)]
    for tag, n in tags.most_common():
        lines.append(f"  - {tag} ({n}x): {FAILURE_TAGS.get(tag, '')}")
    dominant = tags.most_common(1)[0][0]
    hint = {
        "regime_fragile":     "Bias toward theses with a mechanism that holds across regimes, not one tuned to a single market state.",
        "high_vol_breakdown": "Add a volatility-aware sizing or filter; current theses break when vol spikes.",
        "dd_breach":          "Tighten risk control / position sizing; edges exist but drawdown disqualifies them.",
        "ho_decay":           "STOP widening parameters. WF passes are overfitting. Simplify the thesis -- fewer knobs.",
        "turnover_excess":    "Lower trade frequency; current edges die after costs.",
        "insufficient_folds": "Theses are too fragile across time. Aim for a more general mechanism.",
    }.get(dominant, "")
    if hint:
        lines.append(f"\nDirective: {hint}")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# USAGE (meta-review checkpoint)
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    # --- self-test with synthetic data ---
    rng = np.random.default_rng(0)

    # a search of 60 junk strategies + 1 with a small real edge
    junk_sharpes = list(rng.normal(0, 0.06, 60))      # per-period, ~zero edge
    edge_returns = rng.normal(0.0008, 0.011, 750)     # ~3yr daily, small edge

    dsr = deflated_sharpe_ratio(edge_returns, junk_sharpes + [edge_returns.mean()/edge_returns.std()])
    psr0 = probabilistic_sharpe_ratio(edge_returns, 0.0)
    print(f"PSR vs zero      : {psr0:.3f}   (looks great in isolation)")
    print(f"DSR vs 61 trials : {dsr:.3f}   (promote only if > 0.95)")

    folds = [
        FoldResult(0, 1.2, 0.09, 0.4, "trending", True),
        FoldResult(1, 0.9, 0.11, 0.5, "ranging",  True),
        FoldResult(2, -0.3, 0.22, 0.6, "high_vol", False),
        FoldResult(3, 1.1, 0.08, 0.4, "trending", True),
        FoldResult(4, 0.2, 0.14, 0.5, "low_vol",  False),
    ]
    tag, detail = classify_failure(folds, wf_min_folds=4, dd_limit=0.20)
    print(f"\nFailure tag      : {tag} -- {detail}")

    sample = [ValidationResult("h%d" % i, failure_tag=t)
              for i, t in enumerate(["high_vol_breakdown"]*5 + ["ho_decay"]*3 + ["regime_fragile"]*2)]
    print("\n" + failure_feedback(sample))
