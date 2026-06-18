"""Tests for the DRIVEN/NORMAL A/B batch-split ratio (_driven_for_batch).

The split is deterministic (Bresenham / largest-remainder) so the exact ratio is
hit over each cycle while the two arms stay temporally interleaved — no long
single-arm blocks that would confound the IS comparison with regime drift.
"""
import auto_research


def _seq(ratio, n):
    return [auto_research._driven_for_batch(i, ratio) for i in range(n)]


def test_even_split_alternates():
    # 0.5 must reproduce the original n%2 behaviour: N,D,N,D,...
    assert _seq(0.5, 6) == [False, True, False, True, False, True]


def test_70_30_seven_driven_per_ten():
    s = _seq(0.7, 10)
    assert sum(s) == 7              # 7 DRIVEN
    assert s.count(False) == 3      # 3 NORMAL
    # well-spread, not blocked: longest consecutive same-arm run stays small
    longest = best = 1
    for a, b in zip(s, s[1:]):
        best = best + 1 if a == b else 1
        longest = max(longest, best)
    assert longest <= 3


def test_ratio_one_all_driven():
    assert all(_seq(1.0, 8))


def test_ratio_zero_all_normal():
    assert not any(_seq(0.0, 8))


def test_ratio_clamped_out_of_range():
    assert all(_seq(5.0, 5))        # >1 clamps to 1.0
    assert not any(_seq(-2.0, 5))   # <0 clamps to 0.0


def test_long_run_converges_to_ratio():
    for r in (0.3, 0.5, 0.7, 0.8):
        s = _seq(r, 2000)
        assert abs(sum(s) / 2000 - r) < 0.005
