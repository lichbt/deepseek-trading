"""The total-loss rule is UNDERWATER drawdown, not a static line.

The5ers max loss trails the high-water mark: equity <= 0.9 * running peak is a
DQ, and the peak carries across steps. The old default (`--total-mode static`,
one fixed floor 10% below the initial balance) let a path give back its whole
step-1 gain and still pass, which is the bug this pins down.

Deterministic: a stub rng feeds fixed indices so the "path" is exactly
+10% then -10%.
"""
import os
import sys
from types import SimpleNamespace

import numpy as np
import pytest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "scripts"))

import prop_guarded_mc as pg  # noqa: E402


class _Rng:
    """Replays `idx` forever; every guard draw misses."""

    def __init__(self, idx):
        self.idx = list(idx)
        self.calls = 0

    def integers(self, lo, hi):
        v = self.idx[self.calls % len(self.idx)]
        self.calls += 1
        return v

    def random(self):
        return 0.5


def _args(**kw):
    a = SimpleNamespace(
        mode="none", halt=-0.024, slip=0.004, post_halt_flat=0, p_miss=0.10,
        daily=-0.99, total=-0.10, total_mode="underwater",
        consistency=0.50, max_days=6, block=1,
    )
    a.__dict__.update(kw)
    return a


# +10% on day 1, -10% on day 2: equity 0.99 against a peak of 1.10.
RETURNS = np.array([0.10, -0.10, 0.0])


def test_underwater_breaches_on_profit_giveback():
    acct = pg._Acct()
    out, days = pg.run_step(0.50, RETURNS, _Rng([0, 1]), _args(), acct=acct)
    assert (out, days) == ("total", 2)
    assert acct.max_dd == pytest.approx(-0.10, abs=1e-9)


def test_static_mode_lets_the_same_path_live():
    """The old model's blind spot — 10% off the peak, 1% below the initial."""
    acct = pg._Acct()
    out, _ = pg.run_step(0.50, RETURNS, _Rng([0, 1]), _args(total_mode="static"),
                         total=-0.10, acct=acct)
    assert out == "timeout"


def test_peak_carries_across_steps():
    """Step 2 inherits step 1's high-water mark rather than starting fresh."""
    acct = pg._Acct()
    pg.run_step(0.10, np.array([0.10, 0.0]), _Rng([0]), _args(max_days=4), acct=acct)
    assert acct.peak > 1.05, "step 1 should have set a high-water mark above start"
    out, _ = pg.run_step(0.50, np.array([0.10, -0.12, 0.0]), _Rng([1]), _args(),
                         acct=acct)
    assert out == "total"


def test_exact_ten_percent_giveback_breaches_despite_float():
    """`1.1 * 0.9` is 0.9900000000000001 in binary, so a strict `<=` let an
    exact -10.00% day through. Same eps the live guard uses."""
    acct = pg._Acct()
    assert pg.run_step(0.50, RETURNS, _Rng([0, 1]), _args(), acct=acct)[0] == "total"


def test_inside_the_limit_still_survives():
    """-9.9% off a 1.1 peak is 0.9911, i.e. clearly inside — eps must not eat
    the margin and start failing paths that did not breach."""
    acct = pg._Acct()
    out, _ = pg.run_step(0.50, np.array([0.10, -0.099, 0.0]), _Rng([0, 1]),
                         _args(max_days=2), acct=acct)
    assert out == "timeout"
