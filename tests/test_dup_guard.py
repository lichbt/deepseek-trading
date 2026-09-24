"""The promotion guard must refuse a behavioural duplicate."""
import pipeline_utils as P


def test_dup_guard_raises_and_force_bypasses():
    orig = P.behaviour_duplicate
    P.behaviour_duplicate = lambda sid, threshold=None: ("peer", 1.0, 1.0)
    try:
        try:
            P._dup_guard('x', False)
            raise AssertionError('guard did not raise')
        except ValueError:
            pass
        assert P._dup_guard('x', True) is not None
    finally:
        P.behaviour_duplicate = orig
