import auto_research as ar
import meta_review


def test_every_family_has_constraint():
    for f in meta_review._MECH_FAMILIES:
        assert f in ar._MECH_CONSTRAINTS, f'missing constraint for family: {f}'


def test_constraint_round_trips_to_own_family():
    for f in meta_review._MECH_FAMILIES:
        got = meta_review._mechanism_of(ar._MECH_CONSTRAINTS[f])
        assert got == f, f'family {f!r} classified as {got!r}'


def test_constraints_are_real_briefs():
    for f, text in ar._MECH_CONSTRAINTS.items():
        assert len(text) >= 150, f'constraint for {f!r} too short ({len(text)} chars)'


def test_mech_constraint_for_lookup():
    assert ar._mech_constraint_for('nope') is None
    assert ar._mech_constraint_for('event')