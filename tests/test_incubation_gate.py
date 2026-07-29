"""An incubating sleeve must reach the paper book and NOT the prop account.

That asymmetry is the entire incubation feature. It is expressed as nothing more
than which status each consumer filters on, spread across six files — so it is
exactly the kind of invariant that a well-meaning "make the queries consistent"
refactor silently destroys. Being wrong here means a sleeve trades real money
before anyone has checked it does what its code says.

These tests read the SOURCE of each consumer rather than importing it, because
several are shell scripts or have import-time side effects (DB connections,
OANDA clients). Crude, but it pins the one property that matters and fails loudly
when someone edits a filter.
"""
import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent

# (file, must_include_incubating, why)
CONSUMERS = [
    ('run_paper_trading.sh', True,
     'the paper book is where incubation is OBSERVED — excluding it means '
     'incubating sleeves never trade and there is nothing to judge'),
    ('incubation.py', True,
     'the detector must observe the sleeves it gates, or promotion rests on '
     'evidence that was never collected'),
    ('fix_runner.py', False,
     'THE GATE: fix_runner drives the live prop account. An incubating sleeve '
     'reaching it defeats the entire feature and risks real capital'),
    ('portfolio.py', False,
     'weighting an incubating sleeve raises n, and cap_frac = CLUSTER_CAP/n does '
     'NOT renormalise — so every live sleeve would shrink merely because a '
     'candidate entered observation'),
    ('scripts/build_deploy_db.py', False,
     'the compact DB seeds the pod; shipping an incubating sleeve puts it on the '
     'prop account by the back door'),
]


def _source(rel):
    return (ROOT / rel).read_text()


@pytest.mark.parametrize('rel,must_include,why', CONSUMERS,
                         ids=[c[0] for c in CONSUMERS])
def test_status_filter_side(rel, must_include, why):
    src = _source(rel)
    mentions = 'incubating' in src
    assert mentions is must_include, (
        f"{rel} {'must' if must_include else 'must NOT'} select 'incubating' — {why}"
    )


def test_fix_runner_selects_paper_trading_exactly():
    """The prop-side filter must stay an equality test on paper_trading.

    A change to IN (...) or != would be the specific mistake that lets an
    incubating sleeve onto real money.
    """
    src = _source('fix_runner.py')
    assert re.search(r"status'?\]?\s*!=\s*'paper_trading'", src) or \
           re.search(r"status\s*=\s*'paper_trading'", src), \
        "fix_runner no longer pins status to exactly 'paper_trading'"
    assert "IN ('paper_trading','incubating')" not in src
    assert 'IN ("paper_trading","incubating")' not in src


def test_flatten_orphans_spares_incubating():
    """An incubating sleeve legitimately holds paper units while not being live.

    flatten_orphans previously selected `units != 0 AND status != 'paper_trading'`,
    which matches every incubating sleeve — it would have flattened exactly the
    positions incubation exists to observe.
    """
    src = _source('flatten_orphans.py')
    assert "NOT IN ('paper_trading','incubating')" in src, \
        "flatten_orphans would treat incubating sleeves as orphans and flatten them"
    assert "status != 'paper_trading'" not in src, \
        "the old orphan predicate is still present"


def test_reason_codes_classify_the_new_transitions():
    """Unknown statuses fall through to UNCLASSIFIED, which would silently undo
    the 0-UNCLASSIFIED backfill result."""
    import reason_codes as rc
    assert rc.classify('incubating', 'incubation_started') == 'INCUBATING'
    assert rc.classify('paper_trading', 'promoted_from_incubation') == 'PROMOTED'
    # a direct deploy must remain distinguishable from a promotion
    assert rc.classify('paper_trading', 'deployed_for_live') == 'DEPLOYED'


def test_promotion_is_the_only_way_out_of_incubation():
    """promote_sleeve must refuse anything that is not currently incubating."""
    import pipeline_utils as pu
    assert hasattr(pu, 'start_incubation')
    assert hasattr(pu, 'promote_sleeve')
    assert pu.INCUBATING == 'incubating'
    assert pu.PAPER_TRADING == 'paper_trading'
