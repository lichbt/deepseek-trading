"""The one invariant the roll-flat policy rests on.

`fix_runner` enters and exits on a signal CHANGE, so the recorded `signal` is the
whole memory of why a sleeve is flat — and it distinguishes the two kinds of flat
that must behave in OPPOSITE ways:

  * flat because a stop fired      -> stay flat until the signal genuinely changes
  * flat because we closed on purpose -> re-establish on the very next pass

Both already existed (the guard's `flatten_all` writes FLAT(0); the stop paths
write FLAT(st['signal'])), and each is tested where it is used. Nothing pinned
them as a single contract, which is what the roll-flat policy actually depends
on: a policy close is the SECOND kind, and must never be confused with the first.
If that confusion ever happened, a stopped-out sleeve would re-enter on an
unchanged signal — the divergence from the validated return stream that commit
58c1a6f removed.
"""
import fix_runner as fr
from fix_runner import FLAT, acts_on_signal


class TestTheTwoKindsOfFlat:
    def test_deliberate_close_re_establishes_next_pass(self):
        """FLAT(0) is a PAUSE. This is the roll-flat primitive: close before the
        21:00 roll, and the next pass reads 0 -> sig as a change and reopens."""
        assert acts_on_signal(1, FLAT(0)) is True
        assert acts_on_signal(-1, FLAT(0)) is True

    def test_stopped_out_sleeve_stays_flat_on_an_unchanged_signal(self):
        """FLAT(st['signal']) is an EXIT. The negative test that matters: if this
        ever returned True, a stopped-out sleeve would re-enter against a stop the
        validation never modelled."""
        assert acts_on_signal(1, FLAT(1)) is False
        assert acts_on_signal(-1, FLAT(-1)) is False

    def test_a_genuine_flip_still_trades_after_a_stop(self):
        """Positive control — the flat-after-stop rule must not swallow a real
        signal change, or a stopped sleeve would be permanently dead."""
        assert acts_on_signal(-1, FLAT(1)) is True
        assert acts_on_signal(1, FLAT(-1)) is True

    def test_an_exit_signal_still_acts(self):
        assert acts_on_signal(0, FLAT(1)) is True

    def test_flat_and_told_to_be_flat_does_nothing(self):
        assert acts_on_signal(0, FLAT(0)) is False


class TestMutualExclusionIsStructural:
    def test_the_two_kinds_are_one_field_not_two_flags(self):
        """The mutual exclusion the policy needs is a property of the
        REPRESENTATION: one `signal` field holding either the preserved value or
        0. There is no pair of flags that could both be set, so there is no rule
        to remember and no new state to add for a policy close."""
        stopped, deliberate = FLAT(1), FLAT(0)
        assert set(stopped) == set(deliberate)
        differing = [k for k in stopped if stopped[k] != deliberate[k]]
        assert differing == ['signal']

    def test_a_corrupt_entry_raises_rather_than_entering(self):
        """Strict subscript on purpose. run_once wraps each sleeve in try/except,
        so a corrupt entry skips that sleeve; a .get() default would silently turn
        it into an ENTRY."""
        try:
            acts_on_signal(1, {'pos_id': None})
        except KeyError:
            pass
        else:
            raise AssertionError("must raise on a state entry with no 'signal'")


class TestRollFlatUsesTheExistingPrimitive:
    def test_closing_an_index_position_with_flat_zero_reopens(self, tmp_path,
                                                              monkeypatch):
        """End to end on the primitive the policy will use: flatten writes
        FLAT(0), and the next pass therefore acts on the unchanged signal."""
        monkeypatch.setattr(fr, 'STATE_FILE', str(tmp_path / 's.json'))

        class _Ad:
            def cancel_stop(self, ref, side):
                return {'ok': True}

            def close_position(self, pos_id, units, side):
                return {'ord_status': '2'}

        held = {'nas100_x': {'signal': 1, 'pos_id': 'P9', 'units': 1.0, 'side': 1,
                             'stop': 1.0, 'stop_ref': 'S9', 'inst': 'NAS100_USD'}}
        adapters = {'fix': {'NAS100_USD': _Ad()}, 'price': {},
                    'equity': lambda: 100_000.0}

        closed, failed = fr.flatten_all(held, adapters, True, 'roll')
        assert closed == ['nas100_x'] and failed == []
        assert held['nas100_x']['signal'] == 0

        # ...and the still-unchanged strategy signal now reads as a change.
        assert acts_on_signal(1, held['nas100_x']) is True
