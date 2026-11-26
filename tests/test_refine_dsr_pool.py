"""A refined child must land in its parent's trial pool.

Refinement's cost is multiplicity: every child is another draw against the same
pinned windows. The deflated-Sharpe gate is what charges for that, and it only
works if children are pooled with the strategies they were derived from.

The failure this pins is silent. _matching_trial_sharpes pools on the id prefix,
children are named "<parent>_r1", and for a legacy id (no '_auto_' segment, so
the prefix is the whole id) an unstripped child forms a pool of ONE. No prior
trials, no deflation, and the gate reports a healthy DSR for exactly the
population that added the multiplicity.
"""

import validator as V


class TestSearchSpacePrefix:
    def test_child_pools_with_its_parent_for_generated_ids(self):
        parent = 'eurusd_auto_20260716_204438_i19'
        assert V._search_space_prefix(parent + '_r1') == V._search_space_prefix(parent)

    def test_child_pools_with_its_parent_for_legacy_ids(self):
        """The case that silently broke: no '_auto_' segment, so the prefix is
        the whole id and the suffix has to be stripped explicitly."""
        parent = 'mean_reversion_eur_jpy_v18'
        assert V._search_space_prefix(parent + '_r1') == parent
        assert V._search_space_prefix(parent + '_r1') == V._search_space_prefix(parent)

    def test_generated_ids_still_collapse_to_the_instrument(self):
        """The pre-existing behaviour must survive: same instrument, one pool."""
        assert V._search_space_prefix('eurusd_auto_20260716_i19') == 'eurusd'
        assert V._search_space_prefix('eurusd_auto_20260801_i3') == 'eurusd'

    def test_different_instruments_stay_in_different_pools(self):
        """Deflation is only valid within one search space — pooling XAU trials
        into a EUR candidate's luck bar would make the correction meaningless."""
        assert V._search_space_prefix('eurusd_auto_1_i1') != V._search_space_prefix('xauusd_auto_1_i1')

    def test_only_a_refinement_suffix_is_stripped(self):
        """A version suffix is part of the strategy's identity, not lineage."""
        assert V._search_space_prefix('mean_reversion_eur_v21') == 'mean_reversion_eur_v21'
        assert V._search_space_prefix('sr_retest_eur_r2d2') == 'sr_retest_eur_r2d2'

    def test_deeper_refinement_suffixes_also_strip(self):
        """refine_depth is capped at 1 today, but the pooling must not silently
        break if that ever changes."""
        parent = 'mean_reversion_eur_jpy_v18'
        assert V._search_space_prefix(parent + '_r2') == parent
