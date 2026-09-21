"""evaluate_strategy.hold_cap_check — does a declared max-hold param actually bind?

Found 2026-08-22: 16 of the 17 deployable/deployed strategies that declare a hold
cap overshoot it, because the generated shape loops over entry indices and
slice-assigns the position array, so consecutive entries CHAIN. The parameter the
optimiser searched is not a cap, and nothing surfaced that before this check.
"""
import numpy as np
import pandas as pd
import pytest

import evaluate_strategy as E


def _sig(vals):
    return pd.Series(vals, index=pd.RangeIndex(len(vals)))


def test_returns_none_when_no_cap_param_is_declared():
    assert E.hold_cap_check(_sig([1, 1, 0, -1]), {'stop_mult': 3.0}) is None


def test_returns_none_on_an_always_flat_signal():
    # no held runs at all — nothing to compare a cap against
    assert E.hold_cap_check(_sig([0, 0, 0, 0]), {'max_hold': 5}) is None


def test_binding_cap_reports_no_overshoot():
    # longest run is 3, cap is 5
    hc = E.hold_cap_check(_sig([1, 1, 1, 0, -1, -1, 0]), {'max_hold': 5})
    assert hc['verdict'] == 'BINDS'
    assert hc['over'] == 0
    assert hc['max_run'] == 3
    assert hc['runs'] == 2


def test_non_binding_cap_is_caught_and_counted():
    # one run of 6 against a cap of 2, plus one run of 1 that is fine
    hc = E.hold_cap_check(_sig([1, 1, 1, 1, 1, 1, 0, -1, 0]), {'max_hold': 2})
    assert hc['verdict'].startswith('DOES NOT BIND')
    assert (hc['max_run'], hc['over'], hc['runs']) == (6, 1, 2)


def test_a_long_short_flip_is_two_runs_not_one():
    # the RLE must be PER DIRECTION: an always-in oscillator that flips every 2
    # bars is not one stuck 8-bar position, and must not be reported as one
    hc = E.hold_cap_check(_sig([1, 1, -1, -1, 1, 1, -1, -1]), {'max_hold': 3})
    assert hc['verdict'] == 'BINDS'
    assert hc['max_run'] == 2
    assert hc['runs'] == 4


@pytest.mark.parametrize('key', ['max_hold', 'timeout', 'hold_bars', 'max_bars'])
def test_every_alias_is_recognised(key):
    # 'timeout' was the alias that hid the defect on the DEPLOYED
    # audusd_auto_20260806_110126_i15, and 'hold_bars' on gbpusd_..._i3
    hc = E.hold_cap_check(_sig([1, 1, 1, 1, 0]), {key: 2})
    assert hc is not None and hc['param'] == key
    assert hc['verdict'].startswith('DOES NOT BIND')


def test_a_nonsense_cap_is_ignored_rather_than_crashing():
    assert E.hold_cap_check(_sig([1, 1, 0]), {'max_hold': 0}) is None
    assert E.hold_cap_check(_sig([1, 1, 0]), {'max_hold': None}) is None
    assert E.hold_cap_check(_sig([1, 1, 0]), {'max_hold': 'five'}) is None


def test_the_first_alias_present_wins_deterministically():
    # two aliases at once must not make the result order-dependent
    hc = E.hold_cap_check(_sig([1, 1, 1, 0]), {'timeout': 9, 'max_hold': 1})
    assert hc['param'] == 'max_hold'


def test_a_run_that_ends_at_the_last_bar_is_still_counted():
    # the trailing run is only recorded by the flush after the loop; a regression
    # there would silently under-report the worst overshoot
    hc = E.hold_cap_check(_sig([0, 1, 1, 1, 1]), {'max_hold': 2})
    assert hc['max_run'] == 4 and hc['over'] == 1


def test_accepts_a_plain_numpy_signal():
    hc = E.hold_cap_check(np.array([1, 1, 1, 0]), {'max_hold': 1})
    assert hc['max_run'] == 3


class TestReturnColumnsAreLabelledAsReturns:
    """12mo and ytd are RETURNS; maxDD is the only drawdown in the row.

    They sit side by side in _fmt and are all commonly negative, so a negative
    12mo reads as a drawdown figure unless the label says otherwise. Asked
    2026-08-22 by a reader of the output, which is the evidence that it does.
    """

    def _m(self):
        idx = pd.date_range('2024-01-01', periods=600, freq='D')
        net = pd.Series(0.001, index=idx)
        return E.metrics(pd.Series(1, index=idx), net)

    def test_ytd_is_derived_from_the_data_not_hardcoded(self):
        # was pinned to '2026-01-01', which silently becomes "since 2 years ago"
        # in 2027 under a column still labelled for one year
        m = self._m()
        assert m['ytd_year'] == 2025          # last year present in the fixture
        assert m['rytd'] > 0

    def test_ytd_covers_only_the_final_year(self):
        m = self._m()
        full = m['tot']
        assert m['rytd'] < full, 'ytd must be a slice, not the whole history'

    def test_the_row_labels_both_return_columns_as_ret(self):
        row = E._fmt('CAND', self._m())
        assert '12mo ret' in row
        assert 'ytd ret' in row
        assert 'maxDD' in row

    def test_a_negative_year_is_still_a_return_not_a_drawdown(self):
        # a strategy that only loses: its ytd RETURN and its maxDD are different
        # numbers, and conflating them was the actual reading error
        idx = pd.date_range('2025-01-01', periods=200, freq='D')
        net = pd.Series(-0.001, index=idx)
        m = E.metrics(pd.Series(1, index=idx), net)
        assert m['rytd'] < 0 and m['maxdd'] < 0
        assert m['rytd'] != m['maxdd']
