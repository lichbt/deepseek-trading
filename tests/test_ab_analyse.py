"""The pre-registration has to bind the analysis, not decorate it.

An A/B is only as honest as the things that were fixed BEFORE the data existed. Two
of those are easy to edit afterwards without noticing:

  * the sample size and the decision rule, which are coupled — n=147/arm is valid only
    because the challenger must WIN to be adopted (it is ~1.8x slower, so a tie is a
    loss). Soften the rule to "adopt on any improvement" and the effect worth detecting
    drops to +50%, which needs 510/arm. Editing one without the other silently converts
    an adequately powered test into an underpowered one.
  * the endpoint's treatment of missing values. Dropping a NULL walk-forward score, or
    an -inf in-sample score, scores an arm only on the subset where it already worked.

So the script recomputes n from the pre-registered effect and refuses to run if it
disagrees, and these tests are the proof that the refusal is real.
"""
import json

import pytest

from scripts import ab_analyse as A


PREREG = """# heading

```json
{
  "chain": "thesis",
  "control": "c-model",
  "challenger": "x-model",
  "primary": {"metric": "wf_nonzero_rate", "column": "walk_forward_gt_score",
              "null_is_failure": true},
  "baseline_rate": 0.129,
  "relative_effect": 1.00,
  "alpha": 0.05,
  "power": 0.80,
  "n_per_arm": 147,
  "decision_rule": "asymmetric_challenger_must_win"
}
```
"""


def _write(tmp_path, text, name='p.md'):
    p = tmp_path / name
    p.write_text(text)
    return p


class TestTheSampleSizeIsDerived:
    def test_it_reproduces_the_pre_registered_147(self):
        assert A.required_n(0.129, 1.00, 0.05, 0.80) == 147

    def test_softening_the_effect_to_50_percent_needs_510(self):
        """The number the map quotes as the cost of a softer rule. If this ever
        stops matching, the pre-registration's justification is stale."""
        assert A.required_n(0.129, 0.50, 0.05, 0.80) == 510

    def test_a_smaller_effect_always_needs_more_samples(self):
        ns = [A.required_n(0.129, e, 0.05, 0.80) for e in (1.5, 1.0, 0.75, 0.5)]
        assert ns == sorted(ns)


class TestTheCouplingCannotBeEdgedApart:
    def test_editing_n_alone_aborts(self, tmp_path):
        spec = json.loads(PREREG.split('```json')[1].split('```')[0])
        spec['n_per_arm'] = 60
        with pytest.raises(SystemExit) as e:
            A.check_power_coupling(spec)
        assert 'edited without the other' in str(e.value)

    def test_softening_the_rule_alone_aborts(self, tmp_path):
        spec = json.loads(PREREG.split('```json')[1].split('```')[0])
        spec['decision_rule'] = 'adopt_on_any_improvement'
        with pytest.raises(SystemExit) as e:
            A.check_power_coupling(spec)
        assert 'asymmetric rule' in str(e.value)

    def test_the_shipped_pre_registration_is_self_consistent(self):
        """The real file, not a fixture — this is the one that will be used."""
        spec = A.load_prereg(A.PREREG)
        A.check_power_coupling(spec)          # must not raise


class TestItRefusesAnUndefinedAnalysis:
    def test_a_missing_pre_registration_aborts(self, tmp_path):
        with pytest.raises(SystemExit) as e:
            A.load_prereg(tmp_path / 'nope.md')
        assert 'no pre-registration' in str(e.value)

    def test_unparseable_json_aborts(self, tmp_path):
        p = _write(tmp_path, "```json\n{not json}\n```\n")
        with pytest.raises(SystemExit) as e:
            A.load_prereg(p)
        assert 'unparseable' in str(e.value)

    def test_two_json_blocks_abort(self, tmp_path):
        p = _write(tmp_path, PREREG + "\n```json\n{}\n```\n")
        with pytest.raises(SystemExit) as e:
            A.load_prereg(p)
        assert 'exactly one' in str(e.value)

    def test_a_missing_required_key_aborts(self, tmp_path):
        p = _write(tmp_path, PREREG.replace('"alpha": 0.05,', ''))
        with pytest.raises(SystemExit) as e:
            A.load_prereg(p)
        assert 'missing' in str(e.value)


class TestMissingValuesCountAsFailures:
    def test_a_null_walk_forward_is_a_failure_not_a_dropped_row(self):
        rows = [{'wf': 0.3}, {'wf': None}, {'wf': 0.0}]
        assert A.nonzero(rows, 'wf', null_is_failure=True) == 1

    def test_negative_infinity_is_a_failure_when_configured(self):
        """is_gt_score stores -inf for a candidate that blew up. Counting it as
        'non-zero' would score a catastrophe as a success."""
        rows = [{'is': 0.4}, {'is': -1e308}, {'is': 0.0}]
        assert A.nonzero(rows, 'is', neg_inf_is_failure=True) == 1
        assert A.nonzero(rows, 'is', neg_inf_is_failure=False) == 2


class TestTheTestItself:
    def test_identical_arms_are_not_significant(self):
        _, _, p = A.two_proportion(20, 100, 20, 100)
        assert p > 0.9

    def test_a_doubling_at_the_planned_n_is_significant(self):
        """Sanity on the power calculation: the effect the study is sized for
        should land under alpha at the sample size it prescribes."""
        _, _, p = A.two_proportion(19, 147, 38, 147)     # 12.9% vs 25.8%
        assert p < 0.05

    def test_an_empty_arm_does_not_crash(self):
        assert A.two_proportion(0, 0, 5, 10)[2] != A.two_proportion(0, 0, 5, 10)[2]
