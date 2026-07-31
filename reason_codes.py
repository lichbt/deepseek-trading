"""Pure reason-code classifier for strategy status transitions.

This module is intentionally stateless: it does no I/O, uses no database, and
produces deterministic output for the same (new_status, reason) inputs.
"""

import re


# ---------------------------------------------------------------------------
# Enum strings
# ---------------------------------------------------------------------------
INITIAL_SUBMISSION = "INITIAL_SUBMISSION"
PASS = "PASS"
DEPLOYED = "DEPLOYED"
INCUBATING = "INCUBATING"        # entered observe-only incubation (paper book only)
PROMOTED = "PROMOTED"            # incubating -> paper_trading, i.e. onto the prop account

RETIRED_DECAY = "RETIRED_DECAY"
RETIRED_LOOKAHEAD = "RETIRED_LOOKAHEAD"
RETIRED_REPLACED = "RETIRED_REPLACED"
RETIRED_MANUAL = "RETIRED_MANUAL"
RETIRED_DRAWDOWN = "RETIRED_DRAWDOWN"
RETIRED_OTHER = "RETIRED_OTHER"

SKIPPED_BULK_REJECT = "SKIPPED_BULK_REJECT"
SKIPPED_OTHER = "SKIPPED_OTHER"

# Decay verdict flips on a LIVE sleeve. These are NOT status changes — the sleeve
# stays paper_trading — but they resize it automatically (0.5x conviction AND 0.5x
# decay_kelly_scale, so 0.25x combined), which is a material change that otherwise
# leaves no trace outside one of 25 per-sleeve log files.
DECAY_DETECTED = "DECAY_DETECTED"   # -> DECAYED: auto-halved, retire candidate
DECAY_CLEARED = "DECAY_CLEARED"     # -> OK: recovered, size restored

GATE_FAIL_IS = "GATE_FAIL_IS"
GATE_FAIL_WF = "GATE_FAIL_WF"
GATE_FAIL_HOLDOUT_DECAY = "GATE_FAIL_HOLDOUT_DECAY"
GATE_FAIL_HOLDOUT_TRADES = "GATE_FAIL_HOLDOUT_TRADES"
GATE_FAIL_MIN_WINDOWS = "GATE_FAIL_MIN_WINDOWS"
GATE_FAIL_DRAWDOWN = "GATE_FAIL_DRAWDOWN"
GATE_FAIL_DIRECTIONAL_BIAS = "GATE_FAIL_DIRECTIONAL_BIAS"
GATE_FAIL_LOOKAHEAD = "GATE_FAIL_LOOKAHEAD"
GATE_FAIL_OTHER = "GATE_FAIL_OTHER"

DATA_MISSING = "DATA_MISSING"
CODE_TIMEOUT = "CODE_TIMEOUT"
CODE_ERROR = "CODE_ERROR"
NONFINITE_SCORE = "NONFINITE_SCORE"
UNCLASSIFIED = "UNCLASSIFIED"


# ---------------------------------------------------------------------------
# Regex helpers
# ---------------------------------------------------------------------------
_RE_FLAGS = re.IGNORECASE


# Match: "FAIL: IS <anything> < 0.3" (includes numeric values and -inf)
_RE_FAIL_IS = re.compile(r"^IS\s+\S+\s*<\s*[\d.]+", _RE_FLAGS)

# Match: "FAIL: WF <anything> < 0.5" or "< 0.3"
_RE_FAIL_WF = re.compile(r"^WF\s+\S+\s*<\s*[\d.]+", _RE_FLAGS)

# Match: "FAIL: In-sample GT-Score <anything> < 0.3"
_RE_FAIL_IS_GT = re.compile(r"^In-sample\s+GT-Score\s+\S+\s*<", _RE_FLAGS)

# Match: "FAIL: Walk-forward GT-Score <anything> < 0.2"
_RE_FAIL_WF_GT = re.compile(r"^Walk-forward\s+GT-Score\s+\S+\s*<", _RE_FLAGS)

# Non-finite score
_RE_FAIL_NONFINITE = re.compile(r"^IS\s+score\s+non-finite", _RE_FLAGS)

# Data missing / fetch error
_RE_FAIL_DATA_MISSING = re.compile(r"^No\s+valid\s+data", _RE_FLAGS)
_RE_FAIL_DATA_FETCH = re.compile(r"Data\s+fetch\s+error", _RE_FLAGS)

# Timeouts / code errors
_RE_FAIL_TIMEOUT_GRID = re.compile(
    r"^Strategy\s+timed\s+out\s+during\s+grid\s+search", _RE_FLAGS
)
_RE_FAIL_TIMEOUT_WF = re.compile(
    r"^Strategy\s+timed\s+out\s+during\s+walk-forward", _RE_FLAGS
)
_RE_FAIL_CODE_ERROR = re.compile(r"^Code\s+error", _RE_FLAGS)

# Window / edge-count gates
_RE_FAIL_SINGLE_REGIME = re.compile(r"^Single-regime\s+edge", _RE_FLAGS)
_RE_FAIL_SPARSE_TRADES = re.compile(r"^Sparse\s+trades", _RE_FLAGS)
_RE_FAIL_MIN_WINDOW = re.compile(r"^Min\s+window\s+GT-Score", _RE_FLAGS)

# Directional bias
_RE_FAIL_DIRECTIONAL_BIAS = re.compile(r"^directional_bias", _RE_FLAGS)

# Holdout decay (in-sample research gate)
_RE_FAIL_HO_DECAY = re.compile(r"^HO\s+decay", _RE_FLAGS)

# Drawdown gate
_RE_FAIL_MAX_DRAWDOWN = re.compile(r"^Max\s+drawdown", _RE_FLAGS)

# Look-ahead gate
_RE_FAIL_LOOKAHEAD = re.compile(r"^Look-ahead", _RE_FLAGS)

# Generic "did not pass" gates
_RE_FAIL_VALIDATION_GATES = re.compile(
    r"^Validation\s+did\s+not\s+pass\s+all\s+gates", _RE_FLAGS
)
_RE_FAIL_NO_TIMEFRAME = re.compile(r"^No\s+timeframe\s+passed\s+all\s+gates", _RE_FLAGS)

# Holdout failure
_RE_FAIL_HOLDOUT_TRADES_FEW = re.compile(r"^Too\s+few\s+holdout\s+trades", _RE_FLAGS)
_RE_FAIL_HOLDOUT_TRADES_NONE = re.compile(r"^No\s+holdout\s+trades", _RE_FLAGS)

# Pass / deployed
_RE_PASS = re.compile(r"^PASS\s+\(", _RE_FLAGS)


# ---------------------------------------------------------------------------
# Classifier
# ---------------------------------------------------------------------------
def classify(new_status: str, reason: str) -> str:
    """Return a deterministic enum string for a status-history transition.

    Parameters
    ----------
    new_status : str
        The new status value (e.g. 'research_failed', 'passed').
    reason : str
        The verbatim reason prose from status_history.

    Returns
    -------
    str
        One of the reason-code enum strings defined in this module.
    """
    ns = (new_status or "").strip().lower()
    r = reason or ""
    # A small number of research_failed rows are wrapped with a redundant
    # "fail: FAIL:" prefix. Strip it so the standard patterns apply.
    while re.match(r"^fail:", r, re.IGNORECASE):
        r = re.sub(r"^fail:\s*", "", r, flags=re.IGNORECASE).strip()
    rl = r.lower()

    # ------------------------------------------------------------------
    # Success / lifecycle states
    # ------------------------------------------------------------------
    if ns == "proposed":
        if r == "initial_submission":
            return INITIAL_SUBMISSION
        return UNCLASSIFIED

    if ns in ("passed", "passed_but_fragile"):
        # Includes plain "PASS (D)" and rare "BACKED OUT swap" that landed
        # as a passed status.
        return PASS

    if ns == INCUBATING.lower():
        return INCUBATING

    if ns == "paper_trading":
        # Promotion out of incubation is distinguishable from a direct deploy,
        # and the difference is the whole point of the gate: one was observed
        # live first, the other went straight to real capital.
        if "promoted" in rl or "incubation" in rl:
            return PROMOTED
        return DEPLOYED

    # ------------------------------------------------------------------
    # Skipped
    # ------------------------------------------------------------------
    if ns == "skipped":
        if r.startswith("Bulk-rejected"):
            return SKIPPED_BULK_REJECT
        return SKIPPED_OTHER

    # ------------------------------------------------------------------
    # Holdout failures
    # ------------------------------------------------------------------
    if ns == "holdout_failed":
        if _RE_FAIL_HOLDOUT_TRADES_FEW.match(r) or _RE_FAIL_HOLDOUT_TRADES_NONE.match(r):
            return GATE_FAIL_HOLDOUT_TRADES
        return UNCLASSIFIED

    # ------------------------------------------------------------------
    # Walk-forward failures
    # ------------------------------------------------------------------
    if ns == "walk_forward_failed":
        if _RE_FAIL_WF_GT.match(r):
            return GATE_FAIL_WF
        if _RE_FAIL_TIMEOUT_WF.match(r):
            return CODE_TIMEOUT
        return UNCLASSIFIED

    # ------------------------------------------------------------------
    # Retired - split by cause because the prose distinguishes them.
    # ------------------------------------------------------------------
    if ns == "retired":
        # Look-ahead / data leak retirements
        if re.search(r"look-ahead|publication-lag|leak", rl, re.IGNORECASE):
            return RETIRED_LOOKAHEAD
        # Drawdown-driven retirements
        if re.search(r"max-dd|maxdd|reconstructed full-history", rl, re.IGNORECASE):
            return RETIRED_DRAWDOWN
        # Decay / realistic-cost / single-regime retirements
        if re.search(r"decay|single-regime|realistic-cost", rl, re.IGNORECASE):
            return RETIRED_DECAY
        # Replacement / swap retirements
        if re.search(r"superseded|swapped|replaced", rl, re.IGNORECASE):
            return RETIRED_REPLACED
        # Manual / artifact / pulled retirements
        if re.search(r"manual|pulled|market_halted", rl, re.IGNORECASE):
            return RETIRED_MANUAL
        return RETIRED_OTHER

    # ------------------------------------------------------------------
    # Research failures - the bulk of the rows.
    # ------------------------------------------------------------------
    if ns == "research_failed":
        # Non-finite must be checked before the generic IS regex.
        if _RE_FAIL_NONFINITE.match(r):
            return NONFINITE_SCORE

        if _RE_FAIL_IS.match(r) or _RE_FAIL_IS_GT.match(r):
            return GATE_FAIL_IS

        if _RE_FAIL_WF.match(r) or _RE_FAIL_WF_GT.match(r):
            return GATE_FAIL_WF

        if _RE_FAIL_TIMEOUT_GRID.match(r):
            return CODE_TIMEOUT

        if _RE_FAIL_CODE_ERROR.match(r):
            return CODE_ERROR

        if _RE_FAIL_DATA_MISSING.match(r) or _RE_FAIL_DATA_FETCH.search(r):
            return DATA_MISSING

        if _RE_FAIL_SINGLE_REGIME.match(r) or _RE_FAIL_SPARSE_TRADES.match(r) or _RE_FAIL_MIN_WINDOW.match(r):
            return GATE_FAIL_MIN_WINDOWS

        if _RE_FAIL_VALIDATION_GATES.match(r) or _RE_FAIL_NO_TIMEFRAME.match(r):
            return GATE_FAIL_OTHER

        if _RE_FAIL_DIRECTIONAL_BIAS.match(r):
            return GATE_FAIL_DIRECTIONAL_BIAS

        if _RE_FAIL_HO_DECAY.match(r):
            return GATE_FAIL_HOLDOUT_DECAY

        if _RE_FAIL_MAX_DRAWDOWN.match(r):
            return GATE_FAIL_DRAWDOWN

        if _RE_FAIL_LOOKAHEAD.match(r):
            return GATE_FAIL_LOOKAHEAD

        return UNCLASSIFIED

    return UNCLASSIFIED
