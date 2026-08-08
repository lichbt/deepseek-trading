"""Pure safety partition over the existing failure taxonomy, for refinement.

WHY THIS EXISTS. Refeeding a failed strategy to the generator is only safe when
the reason we hand back is a COVERAGE fact ("it did not trade") rather than a
PERFORMANCE verdict ("it traded and scored badly"). validator.py's windows are
pinned (DEV 2015-01-01..2019-12-31, HOLDOUT 2024-01-01..LOCKED_HOLDOUT_START,
see validator.py:173-175) and do NOT advance with a candidate's creation date, so
a refined child is re-scored on exactly the bars its parent failed. Feeding back
a performance verdict therefore hill-climbs a fixed test set — the multiple-
comparisons problem, wearing prose as a disguise. A coverage fact carries no
information about how well anything performed, so it cannot.

This module invents NO taxonomy. reason_codes.py already classifies validator
failure prose into GATE_FAIL_*/CODE_*/DATA_* codes, and pipeline_utils.
gt_score_zero_reason already splits the exact-zero sentinel into its underlying
guards. This is a partition over those two, plus the scrubber that keeps a
numeric payload from riding along inside a code's detail suffix.

Stateless and I/O-free, exactly like reason_codes.py: same inputs always give
the same answer, so the eligibility rule can be tested without a database.

THE EXACT-ZERO TRAP (2026-08-03, binding). "GT-Score 0.0000" is a SENTINEL, not
a computed score: compute_gt_score returns a hard 0.0 on three early-return
guards before reaching its arithmetic. 125 of 156 walk_forward_failed rows carry
that string and are indistinguishable in the database. Classifying them from
stored prose would mark genuine verdicts as mechanical — precisely the leak this
module exists to prevent. So classify() REFUSES to resolve an exact zero without
a zero_reason argument obtained by re-running the strategy; it returns
NEEDS_RERUN instead of guessing. Do not "helpfully" default that branch.
"""

import os
import re
from typing import Optional

import reason_codes as rc
from pipeline_utils import (
    GT_ZERO_TOO_SHORT,
    GT_ZERO_FEW_ACTIVE,
    GT_ZERO_NO_VOL,
    GT_ZERO_CLAMPED,
    GT_ZERO_EXACT,
)


# ---------------------------------------------------------------------------
# Partition codes
# ---------------------------------------------------------------------------
MECHANICAL = "MECHANICAL"      # a coverage bug on the dev/WF side — safe to feed back
# A coverage bug found on the HOLDOUT side: the strategy traded, but produced too
# few DISTINCT entries for its out-of-sample result to be statistically reliable.
# Kept separate from MECHANICAL rather than merged, for two reasons. The brief
# handed to the generator has to differ — these strategies DID trade, and telling
# one it produced no trades sends it loosening the wrong thing. And the fact,
# though it carries no P&L, was observed on the holdout, so the two cohorts'
# survival rates are worth comparing later; if the holdout-derived children
# survive at a suspiciously higher rate, that is a leak becoming visible.
MECHANICAL_HOLDOUT = "MECHANICAL_HOLDOUT"
# NEAR-MISS partitions. These ARE performance failures, and admitting them is a
# deliberate, user-owned exception to the rule above — see the NEAR-MISS note at
# the bottom of this docstring block. What makes it defensible is that the
# MAGNITUDE never leaves this module: the selector reads a score to FIND these
# parents, and refine_select's brief then describes only the failure MODE, with
# no number the model could optimise toward.
NEAR_MISS_HO = "NEAR_MISS_HO"   # passed WF, scored out-of-sample, decayed by a margin
NEAR_MISS_DD = "NEAR_MISS_DD"   # real edge, drawdown slightly over the risk limit
VERDICT = "VERDICT"            # a performance judgment — never feed back
INTEGRITY = "INTEGRITY"        # look-ahead / directional bias — never refine at all
INFRA = "INFRA"                # data or harness failure — re-run, do not refine
NEEDS_RERUN = "NEEDS_RERUN"    # undecidable from stored prose; needs a returns series
UNKNOWN = "UNKNOWN"            # unclassified — treated as VERDICT by is_refinable


# Codes from reason_codes that describe a strategy which did not produce enough
# trades to be measured. None of these reveal how profitable anything was.
_MECHANICAL_CODES = frozenset({
    rc.GATE_FAIL_MIN_WINDOWS,   # sparse trades / single regime / min-window shortfall
})

# Holdout-side coverage. MEASURED 2026-08-08: all 241 holdout_failed rows are
# these — 231 "too few holdout trades", 10 "no holdout trades", and ZERO that
# failed on a holdout score. The stored holdout_gt_score is a hardcoded 0.0
# returned at validator.py:653 BEFORE evaluate_on_data runs, so it is a
# placeholder and never a measurement: reading it as "decayed to zero" is wrong.
_MECHANICAL_HOLDOUT_CODES = frozenset({
    rc.GATE_FAIL_HOLDOUT_TRADES,
})

# Codes that ARE a performance measure, however they are worded.
_VERDICT_CODES = frozenset({
    rc.GATE_FAIL_IS,
    rc.GATE_FAIL_WF,
    rc.GATE_FAIL_HOLDOUT_DECAY,
    rc.GATE_FAIL_DRAWDOWN,
    rc.GATE_FAIL_OTHER,
})

# Refining these would be an attempt to sneak a tainted strategy past the gate
# that caught it. A look-ahead leak is not a coverage bug to be repaired.
_INTEGRITY_CODES = frozenset({
    rc.GATE_FAIL_LOOKAHEAD,
    rc.GATE_FAIL_DIRECTIONAL_BIAS,
})

# Harness failures. The fix is to re-run, not to redesign the strategy — and a
# timeout in particular says nothing about the idea.
_INFRA_CODES = frozenset({
    rc.DATA_MISSING,
    rc.CODE_TIMEOUT,
    rc.CODE_ERROR,
    rc.NONFINITE_SCORE,
})


# ---------------------------------------------------------------------------
# gt_score_zero_reason payloads
# ---------------------------------------------------------------------------
# pipeline_utils returns these as bare strings or as "CODE:detail". Imported by
# VALUE rather than by name so a rename upstream fails loudly in the tests
# rather than silently reclassifying a whole bucket.
_ZERO_MECHANICAL = frozenset({
    GT_ZERO_TOO_SHORT,    # len(returns) < 2 — nothing to measure
    GT_ZERO_FEW_ACTIVE,   # len(active) < 20 — did not trade enough
    GT_ZERO_NO_VOL,       # annual_vol < 1e-6 — flat equity, i.e. never entered
})

# raw < 0 floored to zero, and a genuine computed zero, are both real scores.
_ZERO_VERDICT = frozenset({
    GT_ZERO_CLAMPED,
    GT_ZERO_EXACT,
})

# How close a near-miss must have come to count as one. Env-overridable because
# these are policy, not measurement: they decide how much of a performance
# failure is worth another attempt.
#
# Measured 2026-08-08 over 540 HO-decay and 626 drawdown rejections:
#   HO >= 0.80 of required -> 42 parents      DD <= 35% -> 64 parents
#   HO >= 0.70             -> 58             DD <= 40% -> 136
# 332 of the 540 HO rows show 0.0000, i.e. never scored — those are coverage
# failures wearing a decay label and the ratio band excludes them.
NEAR_MISS_HO_MIN_RATIO = float(os.environ.get('REFINE_NEAR_MISS_HO_RATIO', '0.80'))
NEAR_MISS_DD_MAX_PCT = float(os.environ.get('REFINE_NEAR_MISS_DD_PCT', '35.0'))

# "FAIL: HO decay 0.6742 < 1.0866"  — achieved, then required.
_RE_HO_DECAY = re.compile(r"HO\s+decay\s+([\d.]+)\s*<\s*([\d.]+)", re.IGNORECASE)
# "FAIL: Max drawdown 38.7% > 30% (full reconstructed equity)"
_RE_MAX_DD = re.compile(r"Max\s+drawdown\s+([\d.]+)%\s*>\s*([\d.]+)%", re.IGNORECASE)


def _near_miss(text: str):
    """NEAR_MISS_* if this failure came close enough to be worth another attempt.

    Deterministic and prose-only — no re-run needed, because unlike the
    exact-zero sentinel these reasons carry their own numbers.
    """
    m = _RE_HO_DECAY.search(text)
    if m:
        got, need = float(m.group(1)), float(m.group(2))
        # got == 0 means the holdout was never scored, not that it decayed.
        if need > 0 and got > 0 and (got / need) >= NEAR_MISS_HO_MIN_RATIO:
            return NEAR_MISS_HO
        return None
    m = _RE_MAX_DD.search(text)
    if m:
        dd = float(m.group(1))
        if dd <= NEAR_MISS_DD_MAX_PCT:
            return NEAR_MISS_DD
        return None
    return None


# "GT-Score 0.0000" in any of its stored spellings.
_RE_EXACT_ZERO = re.compile(r"\b0\.0{3,}\s*<", re.IGNORECASE)

# A detail suffix that carries a performance magnitude rather than a count.
# few_active:12 is a trade count (safe); clamped:-0.1234 is a score (not safe).
_RE_SIGNED_DECIMAL = re.compile(r"-?\d+\.\d+")


def _zero_bucket(zero_reason: str) -> str:
    """Partition a pipeline_utils.gt_score_zero_reason payload."""
    head = str(zero_reason).split(':', 1)[0].strip().lower()
    if head in _ZERO_MECHANICAL:
        return MECHANICAL
    if head in _ZERO_VERDICT:
        return VERDICT
    return UNKNOWN


def classify(new_status: str, reason: str, zero_reason: Optional[str] = None) -> str:
    """Partition one validation failure into a refinement-safety class.

    `zero_reason` is a pipeline_utils.gt_score_zero_reason payload, available
    only by re-running the strategy. It is REQUIRED to resolve the exact-zero
    sentinel; without it that branch returns NEEDS_RERUN rather than guessing.
    """
    text = (reason or '').strip()

    # Integrity first, for the same reason reason_codes checks look-ahead first:
    # under-reporting a tainted strategy is far worse than over-reporting one.
    code = rc.classify(new_status, text)
    if code in _INTEGRITY_CODES:
        return INTEGRITY

    # The exact-zero sentinel outranks the prose code. reason_codes maps
    # "Walk-forward GT-Score 0.0000 < 0.2" to GATE_FAIL_WF, which LOOKS like a
    # verdict, but the zero may be any of three coverage guards. Resolve it from
    # the re-run if we have one; refuse to decide if we do not.
    if _RE_EXACT_ZERO.search(text):
        if zero_reason is None:
            return NEEDS_RERUN
        return _zero_bucket(zero_reason)

    # Near-miss before the verdict codes: GATE_FAIL_HOLDOUT_DECAY and
    # GATE_FAIL_DRAWDOWN both live in _VERDICT_CODES, and a near-miss is a
    # deliberate carve-out from them rather than a different code.
    nm = _near_miss(text)
    if nm:
        return nm

    if code in _MECHANICAL_CODES:
        return MECHANICAL
    if code in _MECHANICAL_HOLDOUT_CODES:
        return MECHANICAL_HOLDOUT
    if code in _VERDICT_CODES:
        return VERDICT
    if code in _INFRA_CODES:
        return INFRA
    return UNKNOWN


REFINABLE = frozenset({MECHANICAL, MECHANICAL_HOLDOUT,
                       NEAR_MISS_HO, NEAR_MISS_DD})


def detail(zero_reason) -> Optional[str]:
    """The bare gt_score_zero_reason CODE, without its numeric payload.

    "few_active_bars:12" -> "few_active_bars". The count is dropped because this
    value is persisted and read back by code that must not start treating a
    magnitude as meaningful; the code alone says which defect occurred, which is
    all any consumer needs.
    """
    if not zero_reason:
        return None
    return str(zero_reason).split(':', 1)[0].strip().lower() or None


def is_refinable(partition: str) -> bool:
    """Only the coverage partitions are eligible. Everything else — including
    UNKNOWN and NEEDS_RERUN — is not, so the failure mode is a missed refinement
    rather than a leaked one."""
    return partition in REFINABLE


def scrub(detail: Optional[str]) -> Optional[str]:
    """Strip any signed decimal from a code detail before it can reach a prompt.

    few_active:12 survives (a trade count is coverage information). A payload
    like clamped:-0.1234 would carry a score, so the number is dropped rather
    than the whole string, keeping the code readable in logs.
    """
    if not detail:
        return detail
    return _RE_SIGNED_DECIMAL.sub('<redacted>', str(detail))
